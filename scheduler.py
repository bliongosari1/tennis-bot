#!/usr/bin/env python3
"""
Tennis World Booking Scheduler — Competitive Sniper
====================================================
Runs as a long-lived daemon. Every day at 5:57 PM it wakes up,
logs in, and then snipes each slot THE SECOND it opens.

The booking window works like this:
    A 6:00 PM slot 5 days from now becomes bookable at exactly 6:00 PM today.
    A 6:30 PM slot 5 days from now becomes bookable at exactly 6:30 PM today.
    ...
    A 8:00 PM slot 5 days from now becomes bookable at exactly 8:00 PM today.

So this scheduler fires at each half-hour from 6:00–9:00 PM,
pre-logged-in and on the booking page, to grab the slot instantly.

Usage:
    python scheduler.py                  # run as daemon (headless)
    python scheduler.py --visible        # watch the browser
    python scheduler.py --test-now       # skip waiting, try immediately
"""

import json
import sys
import time
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

# Import core booking functions from book.py
from book import (
    EMAIL,
    PASSWORD,
    BASE_URL,
    LOGIN_URL,
    CLUBS,
    ADVANCE_DAYS,
    do_login,
    open_booking_page,
    click_book_now,
    handle_booking_flow,
    _dismiss_modal,
    get_target_date,
)

# ─────────────────────────────────────────────────────────────────────────────
# Scheduler configuration
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (hour_24, minute, display_string)
# These are the times at which a slot 5 days out becomes bookable.
SNIPE_SCHEDULE = [
    (18, 0,  "06:00 PM"),
    (18, 30, "06:30 PM"),
    (19, 0,  "07:00 PM"),
    (19, 30, "07:30 PM"),
    (20, 0,  "08:00 PM"),   # ← preferred
    (20, 30, "08:30 PM"),
    (21, 0,  "09:00 PM"),
]

# After sniping the newly-opened slot, also try these (earlier slots that
# may still be free).  Ordered by preference.
FALLBACK_TIMES = [
    "08:00 PM", "08:30 PM", "07:30 PM", "07:00 PM",
    "09:00 PM", "06:30 PM", "06:00 PM",
]

# How many rapid-fire retries per slot opening (every RETRY_GAP_S seconds)
SNIPE_RETRIES = 8
RETRY_GAP_S = 3

# Log in this many minutes before the first slot of the evening
PRE_LOGIN_MINUTES = 3

# How many seconds before the exact slot time to refresh the booking page
PRE_REFRESH_S = 10

STATE_FILE = Path("scheduler_state.json")

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("tw-scheduler")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
_fh = logging.FileHandler("scheduler.log")
_fh.setFormatter(_fmt)
log.addHandler(_sh)
log.addHandler(_fh)

# ─────────────────────────────────────────────────────────────────────────────
# State persistence  (tracks whether we already booked today)
# ─────────────────────────────────────────────────────────────────────────────

def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def already_booked_today():
    return _load_state().get("last_booked_date") == datetime.now().strftime("%Y-%m-%d")


def mark_booked_today(slot_time, club_name, target_date):
    state = _load_state()
    state["last_booked_date"] = datetime.now().strftime("%Y-%m-%d")
    state["last_booking"] = {
        "slot": slot_time,
        "club": club_name,
        "for_date": target_date,
        "booked_at": datetime.now().isoformat(),
    }
    _save_state(state)


# ─────────────────────────────────────────────────────────────────────────────
# Timing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _secs_until(hour, minute):
    """Seconds from now until today at hour:minute:00."""
    target = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (target - datetime.now()).total_seconds()


def _sleep_precise(seconds):
    """Sleep, but check the clock to avoid drift."""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 1.0))   # wake every 1 s to stay precise


# ─────────────────────────────────────────────────────────────────────────────
# Core snipe logic
# ─────────────────────────────────────────────────────────────────────────────

def snipe_one_slot(page, primary_time, target_date):
    """
    Attempt to book *primary_time* (just opened), then try fallback times.
    Cycles through all configured zones for each club.
    Returns (booked: bool, slot_str, club_name).
    """
    # Build ordered list: the newly-opened time first, then preference order
    times_to_try = [primary_time] + [t for t in FALLBACK_TIMES if t != primary_time]

    for club in CLUBS:
        zones = club.get("zones", [{"zoneTypeId": club.get("zoneTypeId", 32), "label": ""}])

        for zone in zones:
            zone_id = zone["zoneTypeId"]
            zone_label = zone.get("label", "")

            open_booking_page(page, club["clubId"], zone_id, target_date, zone_label)

            for t in times_to_try:
                if not click_book_now(page, t):
                    continue

                result = handle_booking_flow(page)

                if result == "success":
                    tag = f" [{zone_label}]" if zone_label else ""
                    return True, f"{t}{tag}", club["name"]
                if result == "too_soon":
                    log.info(f"      {t} → too soon, trying next time")
                    continue
                # error → try next time
                continue

    return False, None, None


def run_evening_session(page, target_date, headless):
    """
    Covers one evening window (6–9 PM).  For each half-hour slot:
      1.  Wait until the exact opening second.
      2.  Rapid-fire snipe attempts.
      3.  If booked → stop.
    """
    for hour, minute, display in SNIPE_SCHEDULE:
        if already_booked_today():
            log.info("Already booked today — stopping early.")
            return True

        secs = _secs_until(hour, minute)

        # Skip slots that opened more than 60 s ago
        if secs < -60:
            continue

        # Wait until ~PRE_REFRESH_S before the slot opens, then refresh
        if secs > PRE_REFRESH_S:
            log.info(f"  Waiting {secs:.0f}s until {display} ...")
            _sleep_precise(secs - PRE_REFRESH_S)

            # Refresh booking page right before the slot opens
            log.info(f"  Refreshing page ({PRE_REFRESH_S}s before {display}) ...")
            page.reload(wait_until="networkidle", timeout=15_000)
            page.wait_for_timeout(1000)

        # Final wait for the exact second
        remaining = _secs_until(hour, minute)
        if remaining > 0:
            _sleep_precise(remaining)

        # ── SNIPE! ──────────────────────────────────────────────────────
        log.info(f"  >>> SNIPING {display} <<<")

        for attempt in range(1, SNIPE_RETRIES + 1):
            booked, slot_str, club_name = snipe_one_slot(page, display, target_date)

            if booked:
                log.info(f"  BOOKED! {slot_str} at {club_name} for {target_date}")
                mark_booked_today(slot_str, club_name, target_date)
                try:
                    page.screenshot(path="booking_success.png")
                except Exception:
                    pass
                return True

            if attempt < SNIPE_RETRIES:
                log.info(f"    Attempt {attempt}/{SNIPE_RETRIES} — retrying in {RETRY_GAP_S}s")
                time.sleep(RETRY_GAP_S)

        log.info(f"  Could not book at {display}, moving to next slot ...")

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Main daemon loop
# ─────────────────────────────────────────────────────────────────────────────

def run_scheduler(headless=True, test_now=False):
    log.info("=" * 60)
    log.info("  TENNIS WORLD SCHEDULER — SNIPER MODE")
    log.info(f"  Clubs: {', '.join(c['name'] for c in CLUBS)}")
    log.info(f"  Advance days: {ADVANCE_DAYS}")
    log.info(f"  Pre-login: {PRE_LOGIN_MINUTES} min before first slot")
    log.info("=" * 60)

    while True:
        now = datetime.now()

        # ── Already booked today? Sleep until tomorrow. ─────────────────
        if already_booked_today():
            tomorrow = (now + timedelta(days=1)).replace(
                hour=18 - PRE_LOGIN_MINUTES // 60,
                minute=60 - PRE_LOGIN_MINUTES % 60 if PRE_LOGIN_MINUTES % 60 else 0,
                second=0,
            )
            # Simpler: just sleep until tomorrow 5:57 PM
            tomorrow = (now + timedelta(days=1)).replace(
                hour=17, minute=57, second=0, microsecond=0
            )
            wait = (tomorrow - now).total_seconds()
            log.info(f"Already booked today. Sleeping {wait / 3600:.1f}h until tomorrow 5:57 PM")
            time.sleep(max(wait, 60))
            continue

        # ── Determine when to wake up ───────────────────────────────────
        if test_now:
            # Skip all waiting — useful for testing
            log.info("--test-now: skipping wait, running immediately")
        else:
            first_slot_h, first_slot_m = SNIPE_SCHEDULE[0][0], SNIPE_SCHEDULE[0][1]
            secs_to_first = _secs_until(first_slot_h, first_slot_m)
            login_lead = PRE_LOGIN_MINUTES * 60

            if secs_to_first < -(60 * 60 * 3):
                # Evening window is long past → sleep until tomorrow
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=17, minute=57, second=0, microsecond=0
                )
                wait = (tomorrow - now).total_seconds()
                log.info(f"Evening window passed. Sleeping {wait / 3600:.1f}h until tomorrow")
                time.sleep(max(wait, 60))
                continue

            if secs_to_first > login_lead:
                # Plenty of time — sleep until PRE_LOGIN_MINUTES before first slot
                sleep_for = secs_to_first - login_lead
                wake_time = now + timedelta(seconds=sleep_for)
                log.info(
                    f"Sleeping {sleep_for / 60:.0f} min until "
                    f"{wake_time.strftime('%H:%M:%S')} (pre-login) ..."
                )
                time.sleep(sleep_for)

        # ── Launch browser & log in ─────────────────────────────────────
        target_date = get_target_date()
        log.info(f"Target date: {target_date}")
        log.info("Launching browser ...")

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=headless)
                ctx = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                )
                page = ctx.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined})"
                )

                if not do_login(page):
                    log.error("Login failed — will retry in 60 s")
                    browser.close()
                    time.sleep(60)
                    continue

                # Pre-navigate to booking page for the first club/zone
                first_club = CLUBS[0]
                first_zone = first_club.get("zones", [{"zoneTypeId": 32, "label": ""}])[0]
                open_booking_page(
                    page,
                    first_club["clubId"],
                    first_zone["zoneTypeId"],
                    target_date,
                    first_zone.get("label", ""),
                )

                # Run the evening session
                booked = run_evening_session(page, target_date, headless)

                if booked:
                    log.info("Booking secured for today!")
                else:
                    log.info("No booking made this evening.")

                browser.close()

        except Exception as exc:
            log.error(f"Session error: {exc}", exc_info=True)
            time.sleep(60)

        # If --test-now, exit after one run
        if test_now:
            break


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Tennis World Scheduler — snipes slots at exact opening times"
    )
    ap.add_argument(
        "--visible", action="store_true",
        help="Show browser window",
    )
    ap.add_argument(
        "--test-now", action="store_true",
        help="Run one attempt immediately (don't wait for schedule)",
    )
    args = ap.parse_args()
    run_scheduler(headless=not args.visible, test_now=args.test_now)


if __name__ == "__main__":
    main()
