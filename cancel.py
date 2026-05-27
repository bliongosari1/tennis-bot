#!/usr/bin/env python3
"""
Tennis World Booking Cancellation
=================================
Cancels a specific booking on the My Bookings page.

Reminder: Tennis World charges if you cancel less than 6 hours before
the booking. This script does NOT enforce that — it'll happily cancel
anything you ask it to.

Usage:
    python cancel.py 2026-05-25 20:30          # cancel that exact slot
    python cancel.py 2026-05-25 8:30PM         # also accepted
    python cancel.py --next                    # cancel the soonest booking
    python cancel.py --list                    # just list bookings, don't cancel
    python cancel.py 2026-05-25 20:30 --dry-run  # find it, click Cancel, but
                                                  # press Back instead of Confirm
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta
from typing import Optional

from playwright.sync_api import sync_playwright

from book import BASE_URL, do_login, _click_element, _normalise_time
from daily_summary import fetch_bookings


# Map every common spelling to weekday number (Mon=0 … Sun=6).
_DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def resolve_day_name(day_token: str, today=None) -> str:
    """
    'mon' → next upcoming Monday as YYYY-MM-DD.  If today IS that weekday,
    returns today (so /cancel_sun_0930 on Sunday cancels today's booking).
    """
    today = today or datetime.now().date()
    key = day_token.strip().lower()
    if key not in _DAY_NAMES:
        raise ValueError(f"Not a weekday name: {day_token!r}")
    target_dow = _DAY_NAMES[key]
    delta = (target_dow - today.weekday()) % 7
    return (today + timedelta(days=delta)).strftime("%Y-%m-%d")


log = logging.getLogger("tw-cancel")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
log.addHandler(_h)


# ─── Time / date parsing helpers ─────────────────────────────────────────────

# Re-use the canonical "HH:MM AM/PM" formatter from book.py.
_parse_time = _normalise_time


def _ymd_to_dmy(date_str: str) -> str:
    """2026-05-25 → 25/05/2026 (the format the site uses)."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%d/%m/%Y")


def resolve_date(token: str) -> str:
    """
    Accept any of:
        '2026-05-25'         → '2026-05-25'
        '20260525'           → '2026-05-25'
        'today' / 'tomorrow'
        'mon' / 'monday' / 'fri' / etc → next upcoming match
    Returns YYYY-MM-DD.
    """
    t = token.strip().lower()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        return t
    if re.match(r"^\d{8}$", t):
        return f"{t[:4]}-{t[4:6]}-{t[6:8]}"
    if t == "today":
        return datetime.now().strftime("%Y-%m-%d")
    if t == "tomorrow":
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    if t in _DAY_NAMES:
        return resolve_day_name(t)
    raise ValueError(f"Cannot parse date: {token!r}")


# ─── Page interaction ────────────────────────────────────────────────────────

# JS to find the Cancel-booking link belonging to a booking row whose text
# contains the given date AND time strings, and mark it with data-twcancel.
JS_MARK_CANCEL = """
([targetDate, targetTime]) => {
    document.querySelectorAll('[data-twcancel]')
        .forEach(e => e.removeAttribute('data-twcancel'));

    // All elements whose own text contains both date and time.
    const candidates = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
        const el = walker.currentNode;
        if (!el.innerText) continue;
        if (el.innerText.indexOf(targetDate) !== -1
            && el.innerText.indexOf(targetTime) !== -1
            && el.innerText.indexOf('Cancel booking') !== -1) {
            const r = el.getBoundingClientRect();
            if (r.height > 0 && r.height < 400) {
                candidates.push({el, area: r.width * r.height});
            }
        }
    }
    // Pick the smallest matching container — that's the single booking row.
    candidates.sort((a, b) => a.area - b.area);
    if (candidates.length === 0) return false;
    const row = candidates[0].el;
    const cancelLink = Array.from(row.querySelectorAll('a, button, span, div'))
        .find(e =>
            (e.innerText || '').trim() === 'Cancel booking'
            && e.offsetParent !== null
        );
    if (!cancelLink) return false;
    cancelLink.setAttribute('data-twcancel', '1');
    return true;
}
"""


def cancel_booking(
    date_str: str,
    time_str: str,
    headless: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Cancel one booking.  Returns a dict {ok, message}.
    """
    site_date = _ymd_to_dmy(date_str)
    site_time = _parse_time(time_str)

    log.info(f"Targeting booking on {site_date} at {site_time}")

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
            browser.close()
            return {"ok": False, "message": "Login failed"}

        page.goto(
            f"{BASE_URL}/#/MyCalendar",
            wait_until="networkidle",
            timeout=20_000,
        )
        page.wait_for_timeout(2500)

        # Truncate body to the future-bookings section.
        body = page.evaluate("() => document.body.innerText")
        future_section = body.split("Show past bookings")[0]
        if site_date not in future_section or site_time not in future_section:
            browser.close()
            return {
                "ok": False,
                "message": (
                    f"No upcoming booking matches {site_date} at {site_time}. "
                    f"It may already have been cancelled or be in the past."
                ),
            }

        # Mark and click the right Cancel link.
        found = page.evaluate(JS_MARK_CANCEL, [site_date, site_time])
        if not found:
            page.screenshot(path="cancel_no_match.png")
            browser.close()
            return {
                "ok": False,
                "message": "Could not locate the Cancel link in the DOM",
            }

        page.locator('[data-twcancel="1"]').first.click()
        page.wait_for_timeout(2500)
        page.screenshot(path="cancel_modal.png")

        modal_text = page.evaluate("() => document.body.innerText")
        if "Booking cancellation" not in modal_text:
            browser.close()
            return {
                "ok": False,
                "message": "Cancellation modal did not appear",
            }

        if dry_run:
            if not _click_element(page, "Back"):
                browser.close()
                return {
                    "ok": False,
                    "message": "DRY RUN: modal opened but Back button not clickable",
                }
            page.wait_for_timeout(1500)
            browser.close()
            return {
                "ok": True,
                "message": (
                    f"DRY RUN — would have cancelled "
                    f"{site_date} at {site_time}"
                ),
            }

        if not _click_element(page, "Confirm cancellation"):
            page.screenshot(path="cancel_no_confirm.png")
            browser.close()
            return {
                "ok": False,
                "message": "Confirm cancellation button not found",
            }
        page.wait_for_timeout(3000)
        page.screenshot(path="cancel_after_confirm.png")

        # Re-verify the booking is gone.
        new_body = page.evaluate("() => document.body.innerText")
        new_future = new_body.split("Show past bookings")[0]
        gone = not (site_date in new_future and site_time in new_future)

        browser.close()

    return {
        "ok": gone,
        "message": (
            f"Cancelled {site_date} at {site_time}"
            if gone
            else "Confirm clicked but booking still appears — please check"
        ),
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Cancel a Tennis World booking")
    ap.add_argument(
        "date", nargs="?",
        help="YYYY-MM-DD, 'today', 'tomorrow', or a day name like 'mon', 'fri'",
    )
    ap.add_argument(
        "time", nargs="?",
        help="HH:MM (24h), H:MMAM/PM, '7pm', '8:30am', etc.",
    )
    ap.add_argument("--next", action="store_true", help="Cancel the soonest booking")
    ap.add_argument("--list", action="store_true", help="List upcoming bookings, don't cancel")
    ap.add_argument("--dry-run", action="store_true", help="Click Cancel but press Back instead of Confirm")
    ap.add_argument("--visible", action="store_true")
    args = ap.parse_args()

    if args.list or args.next:
        bookings = fetch_bookings(headless=not args.visible)
        bookings.sort(key=lambda b: (b["date"], b["time"]))
        if not bookings:
            log.info("No upcoming bookings.")
            return 0
        log.info(f"{len(bookings)} upcoming booking(s):")
        for b in bookings:
            log.info(f"  • {b['date']} {b['time']} – {b['club']} ({b['court']})")
        if args.list:
            return 0
        target = bookings[0]
        log.info(f"Cancelling soonest: {target['date']} {target['time']}")
        result = cancel_booking(
            target["date"], target["time"],
            headless=not args.visible, dry_run=args.dry_run,
        )
    else:
        if not args.date or not args.time:
            ap.error("provide DATE and TIME, or use --next / --list")
        try:
            iso_date = resolve_date(args.date)
        except ValueError as exc:
            log.error(str(exc))
            return 2
        result = cancel_booking(
            iso_date, args.time,
            headless=not args.visible, dry_run=args.dry_run,
        )

    log.info(result["message"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
