#!/usr/bin/env python3
"""
Tennis World Booking Scheduler — Competitive Sniper
====================================================
Runs as a long-lived daemon. Wakes up at the right time of day, logs in,
then snipes each slot THE SECOND it opens.

The booking window works like this:
    A slot 5 days from now becomes bookable at the same wall-clock time today.
    A 7:00 PM slot 5 days from now becomes bookable at exactly 7:00 PM today.
    A 11:00 AM Saturday slot becomes bookable at 11:00 AM the Monday before.

Day-of-week schedule:
    Wed–Sun   → evening session  (7:00 → 9:00 PM)   (booking weekday slots)
    Mon–Tue   → morning session  (10 / 11 / 12 PM)  (booking weekend slots)

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
    enabled_clubs,
)

import settings as _S

# ─────────────────────────────────────────────────────────────────────────────
# Scheduler configuration
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (hour_24, minute, display_string).
# Built from settings.EVENING_TIMES / MORNING_TIMES so the user edits one
# file and both the sniper schedule and the fallback list update together.
def _to_schedule_entry(t):
    parsed = datetime.strptime(t, "%I:%M %p")
    return (parsed.hour, parsed.minute, t)


EVENING_SCHEDULE = sorted(
    [_to_schedule_entry(t) for t in _S.EVENING_TIMES],
    key=lambda x: (x[0], x[1]),
)
MORNING_SCHEDULE = sorted(
    [_to_schedule_entry(t) for t in _S.MORNING_TIMES],
    key=lambda x: (x[0], x[1]),
)

# Fallback orderings (priority, not chronological).
FALLBACK_EVENING = list(_S.EVENING_TIMES)
FALLBACK_MORNING = list(_S.MORNING_TIMES)


def schedule_for_today(now=None):
    """
    Return (schedule, fallback_times, label) for today.

    Mon (0) or Tue (1) → MORNING_SCHEDULE  (booking Sat/Sun respectively)
    Anything else      → EVENING_SCHEDULE  (booking the next weekday slot)
    """
    now = now or datetime.now()
    if now.weekday() in (0, 1):
        return MORNING_SCHEDULE, FALLBACK_MORNING, "morning"
    return EVENING_SCHEDULE, FALLBACK_EVENING, "evening"


# Timing knobs come from settings.py.
SNIPE_RETRIES = _S.SNIPE_RETRIES
RETRY_GAP_S = _S.RETRY_GAP_S
PRE_LOGIN_MINUTES = _S.PRE_LOGIN_MINUTES
PRE_REFRESH_S = _S.PRE_REFRESH_S

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

def snipe_one_slot(page, primary_time, target_date, fallback_times, booked_clubs):
    """
    Attempt to book *primary_time* (just opened), then try fallback times.
    Cycles through all configured zones for each non-booked club.

    Returns a list of {club, zone, time} dicts — one per booking made
    (could be 0, 1, or 2 with two venues).  Skips clubs already in
    *booked_clubs*.
    """
    times_to_try = [primary_time] + [
        t for t in fallback_times if t != primary_time
    ]
    bookings = []

    for club in enabled_clubs():
        if club["name"] in booked_clubs:
            continue

        booked_here = False
        zones = club.get(
            "zones",
            [{"zoneTypeId": club.get("zoneTypeId", 32), "label": ""}],
        )

        for zone in zones:
            if booked_here:
                break
            zone_id = zone["zoneTypeId"]
            zone_label = zone.get("label", "")

            open_booking_page(page, club["clubId"], zone_id, target_date, zone_label)

            for t in times_to_try:
                if not click_book_now(page, t):
                    continue

                result = handle_booking_flow(page)

                if result == "success":
                    bookings.append({
                        "club": club["name"],
                        "zone": zone_label,
                        "time": t,
                    })
                    booked_here = True
                    booked_clubs.add(club["name"])
                    break
                if result == "too_soon":
                    log.info(f"      {t} → too soon, trying next time")
                    continue
                # error → try next time
                continue

    return bookings


def run_session(page, target_date, schedule, fallback_times, headless):
    """
    Run one session window.  For each (hour, minute, display) in *schedule*:
      1. Wait until the exact opening second.
      2. Rapid-fire snipe attempts.
      3. Continue until every enabled venue has a booking on target_date.
    """
    target_clubs = {c["name"] for c in enabled_clubs()}
    booked_clubs: set[str] = set()
    all_bookings: list[dict] = []

    for hour, minute, display in schedule:
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
            new = snipe_one_slot(
                page, display, target_date, fallback_times, booked_clubs
            )
            for b in new:
                log.info(
                    f"  BOOKED! {b['time']} at {b['club']} "
                    f"[{b['zone']}] for {target_date}"
                )
                all_bookings.append(b)
                mark_booked_today(b["time"], b["club"], target_date)
                try:
                    from notify import notify_booking_success
                    notify_booking_success(
                        b["time"], b["club"], b["zone"], target_date
                    )
                except Exception as exc:
                    log.warning(f"  Telegram notify failed: {exc}")

            if booked_clubs >= target_clubs:
                log.info("All enabled venues booked — done for today.")
                try:
                    page.screenshot(path="booking_success.png")
                except Exception:
                    pass
                return True

            if not new and attempt < SNIPE_RETRIES:
                log.info(f"    Attempt {attempt}/{SNIPE_RETRIES} — retrying in {RETRY_GAP_S}s")
                time.sleep(RETRY_GAP_S)

        log.info(
            f"  Slot {display} done. Booked so far: "
            f"{sorted(booked_clubs) or 'none'}. "
            f"Still trying: {sorted(target_clubs - booked_clubs) or 'none'}"
        )

    return bool(all_bookings)


# ─────────────────────────────────────────────────────────────────────────────
# Main daemon loop
# ─────────────────────────────────────────────────────────────────────────────

def _next_session_pre_login(now):
    """Return the next datetime we should wake up to pre-login for a session."""
    # If today's session hasn't started, pre-login PRE_LOGIN_MINUTES before
    # the first slot.  Otherwise sleep to tomorrow's pre-login time.
    schedule, _, _ = schedule_for_today(now)
    first_h, first_m, _ = schedule[0]
    today_first = now.replace(
        hour=first_h, minute=first_m, second=0, microsecond=0
    )
    pre_login = today_first - timedelta(minutes=PRE_LOGIN_MINUTES)
    if now < pre_login:
        return pre_login

    # Today's session is past — find tomorrow's.
    tomorrow_noon = (now + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    sched_tomorrow, _, _ = schedule_for_today(tomorrow_noon)
    h, m, _ = sched_tomorrow[0]
    first_tomorrow = tomorrow_noon.replace(hour=h, minute=m)
    return first_tomorrow - timedelta(minutes=PRE_LOGIN_MINUTES)


def run_scheduler(headless=True, test_now=False):
    log.info("=" * 60)
    log.info("  TENNIS WORLD SCHEDULER — SNIPER MODE")
    log.info(f"  Clubs: {', '.join(c['name'] for c in CLUBS)}")
    log.info(f"  Advance days: {ADVANCE_DAYS}")
    log.info(f"  Pre-login: {PRE_LOGIN_MINUTES} min before first slot")
    log.info("=" * 60)

    while True:
        now = datetime.now()
        schedule, fallback_times, session_label = schedule_for_today(now)
        log.info(
            f"Today is {now.strftime('%A')} — using {session_label} session"
        )

        # ── Already booked today? Sleep until next session's pre-login. ─
        if already_booked_today():
            wake = _next_session_pre_login(now)
            wait = (wake - now).total_seconds()
            log.info(
                f"Already booked today. Sleeping {wait / 3600:.1f}h until "
                f"{wake.strftime('%a %H:%M')}"
            )
            time.sleep(max(wait, 60))
            continue

        # ── Determine when to wake up ───────────────────────────────────
        if test_now:
            log.info("--test-now: skipping wait, running immediately")
        else:
            first_slot_h, first_slot_m = schedule[0][0], schedule[0][1]
            secs_to_first = _secs_until(first_slot_h, first_slot_m)
            login_lead = PRE_LOGIN_MINUTES * 60

            # Window is long past (>3h ago) → sleep to next session
            if secs_to_first < -(60 * 60 * 3):
                wake = _next_session_pre_login(now)
                wait = (wake - now).total_seconds()
                log.info(
                    f"Session window passed. Sleeping {wait / 3600:.1f}h "
                    f"until {wake.strftime('%a %H:%M')}"
                )
                time.sleep(max(wait, 60))
                continue

            if secs_to_first > login_lead:
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

                # Run the session window
                booked = run_session(
                    page, target_date, schedule, fallback_times, headless
                )

                if booked:
                    log.info("Booking secured for today!")
                else:
                    log.info("No booking made this session.")
                    try:
                        from notify import notify_session_no_booking
                        notify_session_no_booking(session_label, target_date)
                    except Exception as exc:
                        log.warning(f"Telegram notify failed: {exc}")

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
