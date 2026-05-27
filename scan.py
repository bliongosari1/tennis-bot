#!/usr/bin/env python3
"""
Opportunistic slot scanner
==========================
Walks every (date in lookahead window) × (enabled venue) × (zone) and
captures which preferred time slots show a "Book now" button.

Use cases:
    • Cron every 15 min to catch slots reopened by other people's
      cancellations.
    • On-demand via Telegram /scan.

By default the scanner only NOTIFIES (no booking).  If
settings.SCAN_AUTO_BOOK is True it will attempt to book each opportunity
in priority order (subject to the per-venue/day cap).

Usage:
    python scan.py                  # print + send Telegram message
    python scan.py --quiet          # only send Telegram if anything found
    python scan.py --no-telegram    # print only
    python scan.py --visible        # show browser
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

import settings as S
from book import (
    BASE_URL, CLUBS, do_login, open_booking_page, enabled_clubs,
    preferred_times_for,
)
from daily_summary import fetch_bookings


log = logging.getLogger("tw-scan")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
log.addHandler(_h)


def _visible_times(page, preferred_times: list[str]) -> list[str]:
    """
    Return the subset of preferred_times for which a 'Book now' button is
    currently visible on the page (i.e. the slot is bookable right now).

    Uses the SAME spatial-matching logic as book.JS_FIND_AND_MARK_SLOT —
    a time only counts as available if there is a 'Book now' button
    directly below it within the same cell card.
    """
    return page.evaluate(
        """(times) => {
            // Gather visible time elements whose direct text is a target time.
            const timeBoxes = [];
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT
            );
            while (walker.nextNode()) {
                const el = walker.currentNode;
                if (el.offsetParent === null) continue;
                const ownText = Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent.trim())
                    .join(' ').trim();
                if (!times.includes(ownText)) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    timeBoxes.push({label: ownText, r});
                }
            }

            // Gather visible Book now buttons.
            const bookBoxes = [];
            document.querySelectorAll('button, a, [role="button"], div, span')
                .forEach(el => {
                    if (el.offsetParent === null) return;
                    const own = Array.from(el.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent.trim())
                        .join(' ').trim();
                    if (own !== 'Book now') return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 30 || r.height < 12) return;
                    bookBoxes.push(r);
                });

            // A time is bookable iff some Book now button is directly below
            // it within the same cell card (MAX_DY px, horizontally aligned).
            const MAX_DY = 160;
            const found = new Set();
            for (const {label, r: tr} of timeBoxes) {
                if (found.has(label)) continue;
                for (const br of bookBoxes) {
                    const dy = br.top - tr.bottom;
                    if (dy < -10 || dy > MAX_DY) continue;
                    const overlap =
                        Math.min(tr.right, br.right) - Math.max(tr.left, br.left);
                    if (overlap < tr.width * 0.3) continue;
                    found.add(label);
                    break;
                }
            }
            return Array.from(found);
        }""",
        preferred_times,
    )


def find_opportunities(headless: bool = True) -> list[dict]:
    """
    Return a list of {date, time, club, zone, club_id, zone_id} dicts for
    every preferred slot that's currently free across the lookahead window.
    """
    out = []

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
            return []

        today = datetime.now().date()
        dates = [
            (today + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(S.SCAN_LOOKAHEAD_DAYS)
        ]

        for date_str in dates:
            preferred = preferred_times_for(date_str)
            for club in enabled_clubs():
                for zone in club["zones"]:
                    try:
                        open_booking_page(
                            page, club["clubId"], zone["zoneTypeId"],
                            date_str, zone.get("label", ""),
                        )
                    except Exception as exc:
                        log.warning(
                            f"  skip {club['name']}/{zone['label']}/{date_str}: {exc}"
                        )
                        continue
                    free = _visible_times(page, preferred)
                    for tm in free:
                        out.append({
                            "date":     date_str,
                            "time":     tm,
                            "club":     club["name"],
                            "club_id":  club["clubId"],
                            "zone":     zone.get("label", ""),
                            "zone_id":  zone["zoneTypeId"],
                        })

        browser.close()

    # Sort by date then time priority within the day.
    def _sort_key(o):
        prefs = preferred_times_for(o["date"])
        try:
            tprio = prefs.index(o["time"])
        except ValueError:
            tprio = 999
        return (o["date"], tprio)

    out.sort(key=_sort_key)
    return out


def _booked_clubs_by_date(bookings: list) -> dict[str, set[str]]:
    """{ '2026-05-25': {'Melbourne Park'} } — what's already booked."""
    by_date: dict[str, set[str]] = {}
    for b in bookings:
        club_short = b["club"].replace("Tennis World ", "").replace(" Park", "")
        # Normalise to our internal naming
        if "Melbourne" in b["club"]:
            club = "Melbourne Park"
        elif "Albert" in b["club"]:
            club = "Albert Reserve"
        else:
            club = b["club"]
        by_date.setdefault(b["date"], set()).add(club)
    return by_date


def filter_fresh(opps: list[dict], bookings: list[dict]) -> list[dict]:
    """Drop opps for (date, venue) combos where you already have a booking."""
    booked = _booked_clubs_by_date(bookings)
    return [o for o in opps if o["club"] not in booked.get(o["date"], set())]


def format_message(fresh: list[dict], header: str = "🎾 <b>Free slots available</b>") -> str | None:
    """
    Build the Telegram message body for a list of opportunities.
    Returns None if there are no opportunities.
    """
    if not fresh:
        return None
    from notify import _DAY_ABBREV
    lines = [header]
    by_date: dict[str, list[dict]] = {}
    for o in fresh:
        by_date.setdefault(o["date"], []).append(o)
    for date_str in sorted(by_date.keys()):
        d = datetime.strptime(date_str, "%Y-%m-%d")
        day_abbrev = _DAY_ABBREV[d.weekday()]
        lines.append("")
        lines.append(f"<b>{d.strftime('%a %d %b')}</b>")
        # Group by (club, time) — same time/club can appear under multiple zones.
        seen = set()
        for o in by_date[date_str]:
            key = (o["club"], o["time"])
            if key in seen:
                continue
            seen.add(key)
            hhmm = datetime.strptime(o["time"], "%I:%M %p").strftime("%H%M")
            snipe = f"/snipe_{day_abbrev}_{hhmm}"
            lines.append(
                f"• <b>{o['time']}</b> · {o['club']}\n"
                f"   Book: {snipe}"
            )

    lines.append("")
    lines.append(
        "<i>Tap any /snipe_… command to grab that slot. "
        "Member 5-day rule may still block — bot will report back.</i>"
    )
    return "\n".join(lines)


def scan_and_message(headless: bool = True) -> tuple[list[dict], str | None]:
    """
    Run the full scan flow (login → scan → dedupe vs bookings) and return
    (fresh_opps, message_or_None).
    """
    opps = find_opportunities(headless=headless)
    log.info(f"Found {len(opps)} opportunity slot(s)")
    bookings = fetch_bookings(headless=headless)
    fresh = filter_fresh(opps, bookings)
    log.info(f"After dedupe vs existing bookings: {len(fresh)} new opportunities")
    return fresh, format_message(fresh)


def main():
    ap = argparse.ArgumentParser(description="Scan for opportunistic free slots")
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--no-telegram", action="store_true",
                    help="print to stdout only, don't send to Telegram")
    ap.add_argument("--quiet", action="store_true",
                    help="don't send to Telegram if nothing was found")
    args = ap.parse_args()

    fresh, message = scan_and_message(headless=not args.visible)
    for o in fresh:
        log.info(f"  {o['date']} {o['time']} · {o['club']} · {o['zone']}")

    if args.no_telegram:
        if message:
            print(message)
        return 0

    from notify import send
    if message:
        send(message)
    elif not args.quiet:
        send("🎾 Scan finished — no new free slots in your preferred times.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
