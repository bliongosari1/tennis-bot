#!/usr/bin/env python3
"""
Daily Tennis Summary
====================
Logs into Tennis World, scrapes "My Bookings", and pings Telegram with
any bookings happening today.

Run this once a day in the morning (locally via cron or on GitHub Actions).

Usage:
    python daily_summary.py                  # today's bookings only
    python daily_summary.py --days 7         # all upcoming bookings in next N days
    python daily_summary.py --always-send    # also send "no bookings today" message
    python daily_summary.py --visible        # show browser for debugging
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

from book import BASE_URL, do_login
from notify import notify_daily_summary, send as telegram_send


log = logging.getLogger("tw-daily")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
log.addHandler(_h)


# A single booking block in the "Reserved classes" feed looks like:
#
#     08:30 PM - 09:30 PM
#     MONDAY
#     25/05/2026
#     Hardcourt 1
#      Tennis World Albert Reserve
#     Cancel booking         ← only present for upcoming bookings
#
BOOKING_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)\s*\n"
    r"\s*([A-Z]+DAY)\s*\n"
    r"\s*(\d{1,2}/\d{1,2}/\d{4})\s*\n"
    r"\s*([^\n]+)\s*\n"
    r"\s*(Tennis World[^\n]+)",
    re.IGNORECASE,
)


def fetch_bookings(headless=True):
    """Return a list of booking dicts {date, time, time_end, court, club, day}."""
    bookings = []
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
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        if not do_login(page):
            log.error("Login failed")
            browser.close()
            return []

        page.goto(
            f"{BASE_URL}/#/MyCalendar",
            wait_until="networkidle",
            timeout=20_000,
        )
        page.wait_for_timeout(2500)

        body = page.evaluate("() => document.body.innerText")
        log.info(f"Fetched MyCalendar page ({len(body)} chars)")

        # The page lists future + past bookings.  We only want upcoming ones —
        # before the "Show past bookings" divider.
        if "Show past bookings" in body:
            body = body.split("Show past bookings")[0]

        for m in BOOKING_RE.finditer(body):
            time_start, time_end, day, date_str, court, club = m.groups()
            try:
                d = datetime.strptime(date_str.strip(), "%d/%m/%Y").date()
            except ValueError:
                continue
            bookings.append({
                "date": d.strftime("%Y-%m-%d"),
                "date_obj": d,
                "day": day.strip().title(),
                "time": time_start.strip(),
                "time_end": time_end.strip(),
                "court": court.strip(),
                "club": club.strip(),
            })

        browser.close()

    return bookings


def main():
    ap = argparse.ArgumentParser(description="Daily tennis booking summary → Telegram")
    ap.add_argument(
        "--days", type=int, default=4,
        help="How many days ahead to include (default 4 = today + next 3).",
    )
    ap.add_argument(
        "--silent-when-empty", action="store_true",
        help="Don't send anything if there are no bookings in the range.",
    )
    ap.add_argument("--visible", action="store_true")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print the summary instead of sending to Telegram.",
    )
    args = ap.parse_args()

    bookings = fetch_bookings(headless=not args.visible)
    log.info(f"Found {len(bookings)} upcoming booking(s) total")

    today = datetime.now().date()
    cutoff = today + timedelta(days=args.days)
    todays = [
        b for b in bookings if today <= b["date_obj"] < cutoff
    ]

    log.info(
        f"{len(todays)} booking(s) between {today} and {cutoff - timedelta(days=1)}"
    )
    for b in todays:
        log.info(f"  • {b['date']} {b['time']} – {b['club']} ({b['court']})")

    if args.dry_run:
        return 0

    if not todays and args.silent_when_empty:
        log.info("No bookings in range and --silent-when-empty set; staying silent.")
        return 0

    payload = [
        {
            "date": b["date"],
            "time": f"{b['time']}–{b['time_end']}",
            "club": b["club"],
            "zone": b["court"],
        }
        for b in todays
    ]
    notify_daily_summary(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
