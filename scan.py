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


def _visible_times(page, preferred_times: list[str], target_date: str) -> list[str]:
    """
    Return the subset of preferred_times that are bookable on *target_date*
    (YYYY-MM-DD).  The page shows a 7-day grid so we filter to the column
    whose header matches target_date.
    """
    from book import _target_day_dm
    day_dm = _target_day_dm(target_date)

    return page.evaluate(
        """([times, targetDayDM]) => {
            function normaliseDM(s) {
                const m = s.match(/(\\d{1,2})\\/(\\d{1,2})/);
                if (!m) return null;
                return `${parseInt(m[1],10)}/${parseInt(m[2],10)}`;
            }
            const wantDM = normaliseDM(targetDayDM);

            // Find the column for our target day, if any.
            let column = null;
            document.querySelectorAll('div, span, th, td').forEach(el => {
                if (el.offsetParent === null) return;
                const txt = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                if (!/^(SUN|MON|TUE|WED|THU|FRI|SAT)[A-Z]*\\s*\\d{1,2}\\/\\d{1,2}$/i
                      .test(txt)) return;
                if (normaliseDM(txt) !== wantDM) return;
                const r = el.getBoundingClientRect();
                if (r.height < 10 || r.height > 100 || r.width < 50) return;
                column = {left: r.left, right: r.right};
            });

            const inColumn = (r) => {
                if (!column) return true;
                const xMid = (r.left + r.right) / 2;
                return xMid >= column.left - 10 && xMid <= column.right + 10;
            };

            // Gather visible time elements in the target column.
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
                if (r.width > 0 && r.height > 0 && inColumn(r)) {
                    timeBoxes.push({label: ownText, r});
                }
            }

            // Gather Book now buttons in the target column.
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
                    if (!inColumn(r)) return;
                    bookBoxes.push(r);
                });

            // Pair: Book button directly below the time within ~160 px,
            // overlap ≥ 30% of the narrower of the two (button is often
            // narrower than the big time text but still inside the cell).
            const MAX_DY = 160;
            const found = new Set();
            for (const {label, r: tr} of timeBoxes) {
                if (found.has(label)) continue;
                for (const br of bookBoxes) {
                    const dy = br.top - tr.bottom;
                    if (dy < -10 || dy > MAX_DY) continue;
                    const overlap =
                        Math.min(tr.right, br.right) - Math.max(tr.left, br.left);
                    const minWidth = Math.min(tr.width, br.width);
                    if (overlap < minWidth * 0.3) continue;
                    found.add(label);
                    break;
                }
            }
            return Array.from(found);
        }""",
        [preferred_times, day_dm],
    )


def find_opportunities(
    headless: bool = True,
    on_progress=None,
    page=None,
) -> list[dict]:
    """
    Return a list of {date, time, club, zone, club_id, zone_id} dicts for
    every preferred slot that's currently free across the lookahead window.

    on_progress(date_str, slots_so_far) — optional callback fired once per
    date scanned, useful for streaming progress to Telegram.
    page — already-logged-in Playwright page; pass to reuse a session and
    skip a full re-login (saves ~10 s when called from scan_and_message).
    """
    out = []

    def _run(p):
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
                        # Short wait — we only read DOM, not click.
                        open_booking_page(
                            p, club["clubId"], zone["zoneTypeId"],
                            date_str, zone.get("label", ""),
                            wait_ms=1200,
                        )
                    except Exception as exc:
                        log.warning(
                            f"  skip {club['name']}/{zone['label']}/{date_str}: {exc}"
                        )
                        continue
                    free = _visible_times(p, preferred, date_str)
                    for tm in free:
                        out.append({
                            "date":     date_str,
                            "time":     tm,
                            "club":     club["name"],
                            "club_id":  club["clubId"],
                            "zone":     zone.get("label", ""),
                            "zone_id":  zone["zoneTypeId"],
                        })
            if on_progress:
                try:
                    on_progress(date_str, list(out))
                except Exception as exc:
                    log.warning(f"on_progress raised: {exc}")

    if page is not None:
        _run(page)
    else:
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
            p = ctx.new_page()
            p.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            if not do_login(p):
                browser.close()
                return []
            _run(p)
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


def scan_and_message(
    headless: bool = True,
    on_progress=None,
) -> tuple[list[dict], str | None]:
    """
    Run the full scan flow in ONE browser session (single login):
      1. Log in.
      2. Scan all (date × venue × zone) for free preferred slots.
      3. Fetch My Bookings to dedupe vs existing.
      4. Return (fresh_opps, message_or_None).

    on_progress(date_str, slots_so_far) — fired once per date scanned.
    """
    out: list[dict] = []
    bookings: list[dict] = []

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
            return [], None

        # Scan using the already-logged-in page.
        out = find_opportunities(
            headless=headless, on_progress=on_progress, page=page,
        )

        # Fetch My Bookings in the same session to dedupe.
        try:
            page.goto(
                f"{BASE_URL}/#/MyCalendar",
                wait_until="networkidle",
                timeout=20_000,
            )
            page.wait_for_timeout(1500)
            body = page.evaluate("() => document.body.innerText")
            if "Show past bookings" in body:
                body = body.split("Show past bookings")[0]
            from daily_summary import BOOKING_RE
            for m in BOOKING_RE.finditer(body):
                time_start, time_end, day, date_str, court, club = m.groups()
                try:
                    d = datetime.strptime(date_str.strip(), "%d/%m/%Y").date()
                except ValueError:
                    continue
                bookings.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "club": club.strip(),
                })
        except Exception as exc:
            log.warning(f"could not fetch bookings for dedupe: {exc}")

        browser.close()

    log.info(f"Found {len(out)} opportunity slot(s)")
    fresh = filter_fresh(out, bookings)
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
