#!/usr/bin/env python3
"""
Tennis World Auto-Booker
========================
Automatically books tennis courts at Tennis World (Melbourne Park first,
Albert Reserve fallback).  Books 5 days in advance.

Weekday targets (Mon–Fri):  7:00 PM → 9:00 PM   (preferring 8 PM)
Weekend targets (Sat–Sun):  10 / 11 / 12 PM     (preferring 11 AM)

Usage:
    python book.py                      # single attempt
    python book.py --loop               # retry every 30 min until booked
    python book.py --loop --interval 15 # retry every 15 min
    python book.py --visible            # show browser for debugging
    python book.py --date 2026-02-13    # override target date
"""

import os
import re
import sys
import time
import argparse
import logging
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

EMAIL = os.getenv("TW_EMAIL")
PASSWORD = os.getenv("TW_PASSWORD")

if not EMAIL or not PASSWORD:
    print("ERROR: Set TW_EMAIL and TW_PASSWORD in your .env file")
    sys.exit(1)

BASE_URL = "https://tennisworld.perfectgym.com.au/ClientPortal2"
LOGIN_URL = f"{BASE_URL}/#/Login"

# Venues and court types to try, in priority order.
# Melbourne Park is preferred — Albert Reserve is the fallback.
#
# IDs discovered by visiting each booking URL while logged in:
#   https://tennisworld.perfectgym.com.au/ClientPortal2/#/FacilityBooking?clubId=X&zoneTypeId=Y
CLUBS = [
    # ── Melbourne Park (preferred) ──────────────────────────────────────
    {
        "name": "Melbourne Park",
        "clubId": 2,
        "zones": [
            {"zoneTypeId": 1,  "label": "NTC Outdoor Courts"},
            {"zoneTypeId": 27, "label": "Western Courts (Outdoor Full Court)"},
            {"zoneTypeId": 28, "label": "Eastern Courts (Full Court)"},
            {"zoneTypeId": 31, "label": "NTC Eastern Courts"},
            # Excluded (paid): 26 Western Show Court, 30 NTC Indoor Courts
        ],
    },
    # ── Albert Reserve (fallback) ───────────────────────────────────────
    {
        "name": "Albert Reserve",
        "clubId": 3,
        "zones": [
            {"zoneTypeId": 32, "label": "Full court Albert Reserve"},
        ],
    },
]

# Time / window prefs come from settings.py so the user has one place to edit.
import settings as _S

EVENING_TIMES = _S.EVENING_TIMES
MORNING_TIMES = _S.MORNING_TIMES
ADVANCE_DAYS = _S.ADVANCE_DAYS


def preferred_times_for(target_date):
    """
    Return the time-slot priority list for a given target booking date.

    Saturday/Sunday targets → morning slots (10/11/12).
    Weekday targets         → evening slots (7–9 PM).
    """
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    weekday = target_date.weekday()  # Mon=0 … Sun=6
    if weekday in (5, 6):
        return list(MORNING_TIMES)
    return list(EVENING_TIMES)


# Backwards-compat — old code expects PREFERRED_TIMES at module scope.
PREFERRED_TIMES = EVENING_TIMES


def enabled_clubs():
    """Return CLUBS filtered to those enabled in settings.py, in settings order."""
    enabled = _S.enabled_venue_names()
    by_name = {c["name"]: c for c in CLUBS}
    ordered = []
    for v in _S.VENUES:
        if v.get("enabled", True) and v["name"] in by_name:
            ordered.append(by_name[v["name"]])
    return ordered or CLUBS


def _normalise_time(raw: str) -> str:
    """
    Accept human time formats and return the canonical 'HH:MM AM/PM' string
    that the booking page uses.

        '7pm'      → '07:00 PM'
        '7:30pm'   → '07:30 PM'
        '19:00'    → '07:00 PM'
        '8:30am'   → '08:30 AM'
        '08:30 AM' → '08:30 AM'
        '0830'     → '08:30 AM'      (bare HHMM)
        '2030'     → '08:30 PM'
        '830'      → '08:30 AM'
    """
    s = raw.strip().upper().replace(" ", "")

    # Bare HHMM (3 or 4 digits, no separator, no AM/PM).
    m = re.match(r"^(\d{3,4})$", s)
    if m:
        digits = m.group(1).zfill(4)
        h = int(digits[:2])
        mi = int(digits[2:])
        if 0 <= h < 24 and 0 <= mi < 60:
            ampm = "PM" if h >= 12 else "AM"
            h12 = h % 12 or 12
            return f"{h12:02d}:{mi:02d} {ampm}"

    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(AM|PM)$", s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        ampm = m.group(3)
        return f"{h:02d}:{mi:02d} {ampm}"

    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        ampm = "PM" if h >= 12 else "AM"
        h12 = h % 12 or 12
        return f"{h12:02d}:{mi:02d} {ampm}"
    raise ValueError(f"Cannot parse time: {raw!r}")

# Zone filtering — only book courts that are FREE with your membership.
# Any facility type whose name contains one of these keywords is SKIPPED.
EXCLUDED_ZONE_KEYWORDS = [
    "Show Court",     # outdoor show court — costs extra
    "Indoor",         # indoor courts — costs extra
    "Physio",         # physio room — not a court
    "Pro Players",    # pro players only
]

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("tw-booker")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
_fh = logging.FileHandler("booker.log")
_fh.setFormatter(_fmt)
log.addHandler(_sh)
log.addHandler(_fh)

# ─────────────────────────────────────────────────────────────────────────────
# JavaScript helpers (injected into the page)
# ─────────────────────────────────────────────────────────────────────────────

# Find the "Book now" button that belongs to a given time slot AND a given
# target date (DD/MM, e.g. "29/05").  The page shows a 7-day grid so we
# must filter to the target day's column first — otherwise a missing slot
# on the target day silently matches a different day's cell.
JS_FIND_AND_MARK_SLOT = """
([targetTime, targetDayDM]) => {
    document.querySelectorAll('[data-twbot]')
        .forEach(e => e.removeAttribute('data-twbot'));

    // Find the day-header element whose text matches "<DAYNAME> <D/M>".
    // We accept any leading-zero variant: "1/06" or "01/06" both match
    // "1/6" → strip leading zeros on both sides before comparing.
    function normaliseDM(s) {
        const m = s.match(/(\\d{1,2})\\/(\\d{1,2})/);
        if (!m) return null;
        return `${parseInt(m[1],10)}/${parseInt(m[2],10)}`;
    }
    const wantDM = normaliseDM(targetDayDM);
    if (!wantDM) return false;

    let column = null;   // {left, right}
    document.querySelectorAll('div, span, th, td').forEach(el => {
        if (el.offsetParent === null) return;
        const txt = (el.innerText || '').trim();
        if (!/^(SUN|MON|TUE|WED|THU|FRI|SAT)[A-Z]*\\s*\\n?\\s*\\d{1,2}\\/\\d{1,2}$/i
              .test(txt.replace(/\\s+/g, ' '))) return;
        const got = normaliseDM(txt);
        if (got !== wantDM) return;
        const r = el.getBoundingClientRect();
        if (r.height < 10 || r.height > 100 || r.width < 50) return;
        column = {left: r.left, right: r.right};
    });

    // No header found at all → fall back to no-column-filter so single-day
    // views (e.g. mobile) still work.
    const inColumn = (r) => {
        if (!column) return true;
        const xMid = (r.left + r.right) / 2;
        return xMid >= column.left - 10 && xMid <= column.right + 10;
    };

    // Visible time elements whose direct text is the target time AND
    // (when a column is known) sit inside the target day's column.
    const timeBoxes = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
        const el = walker.currentNode;
        if (el.offsetParent === null) continue;
        const ownText = Array.from(el.childNodes)
            .filter(n => n.nodeType === 3)
            .map(n => n.textContent.trim())
            .join(' ').trim();
        if (ownText === targetTime) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 && inColumn(r)) {
                timeBoxes.push({el, r});
            }
        }
    }
    if (timeBoxes.length === 0) return false;

    // Same column filter for the Book now buttons.
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
            bookBoxes.push({el, r});
        });
    if (bookBoxes.length === 0) return false;

    // Pair: Book button must sit directly below the time, in the same cell card.
    // Overlap must be at least 30% of the NARROWER element (the button is
    // often narrower than the big blue time text but still inside the cell).
    const MAX_DY = 160;
    for (const {el: timeEl, r: tr} of timeBoxes) {
        let best = null;
        let bestDy = Infinity;
        for (const {el: bookEl, r: br} of bookBoxes) {
            const dy = br.top - tr.bottom;
            if (dy < -10 || dy > MAX_DY) continue;
            const overlap = Math.min(tr.right, br.right) - Math.max(tr.left, br.left);
            const minWidth = Math.min(tr.width, br.width);
            if (overlap < minWidth * 0.3) continue;
            if (dy < bestDy) {
                bestDy = dy;
                best = bookEl;
            }
        }
        if (best) {
            best.setAttribute('data-twbot', '1');
            return true;
        }
    }
    return false;
}
"""


def _target_day_dm(date_str: str) -> str:
    """2026-05-29 → '29/5' for matching the page's day-header text."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.day}/{d.month}"

# ─────────────────────────────────────────────────────────────────────────────
# Core logic
# ─────────────────────────────────────────────────────────────────────────────

def get_target_date(override=None):
    """Return the date we want to book (today + ADVANCE_DAYS)."""
    if override:
        return override
    return (datetime.now() + timedelta(days=ADVANCE_DAYS)).strftime("%Y-%m-%d")


def do_login(page):
    """Navigate to the login page and authenticate."""
    log.info("Navigating to login page ...")
    page.goto(LOGIN_URL, timeout=60_000)
    page.wait_for_timeout(5000)

    # Debug screenshot to see what loaded
    page.screenshot(path="debug_login_page.png")
    log.info(f"Login page URL: {page.url}")

    # Wait for any input to appear (the SPA may still be rendering)
    page.locator("input").first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(1000)

    # Fill email — try multiple selector strategies
    email_filled = False
    for selector in [
        'input[type="email"]',
        'input[name*="ogin"]',
        'input[name*="ail"]',
        'input[placeholder*="mail"]',
        'input[placeholder*="ogin"]',
        'input:not([type="password"])',
    ]:
        loc = page.locator(selector)
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.fill(EMAIL)
            email_filled = True
            log.info(f"  Filled email using: {selector}")
            break
    if not email_filled:
        # Last resort: fill the first visible input
        page.locator("input").first.fill(EMAIL)
        log.info("  Filled email using first input fallback")

    # Fill password
    page.locator('input[type="password"]').first.fill(PASSWORD)
    log.info("  Filled password")

    # Click submit — try many possible button selectors
    clicked = False
    for selector in [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("LOG IN")',
        'button:has-text("Sign in")',
        'button:has-text("SIGN IN")',
        'a:has-text("Log in")',
        'a:has-text("LOG IN")',
        '[type="submit"]',
    ]:
        loc = page.locator(selector)
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.click()
            clicked = True
            log.info(f"  Clicked login using: {selector}")
            break

    if not clicked:
        # Fallback: press Enter in the password field
        page.locator('input[type="password"]').first.press("Enter")
        log.info("  Pressed Enter in password field as fallback")

    page.wait_for_timeout(5000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass  # SPA may not fully settle

    page.screenshot(path="debug_after_login.png")
    log.info(f"Post-login URL: {page.url}")

    # Basic check — if URL still contains "Login", auth probably failed
    if "Login" in page.url:
        log.error("Login may have failed (still on login page)")
        return False

    log.info("Logged in successfully")
    return True


def open_booking_page(page, club_id, zone_type_id, date_str, label="", wait_ms=3000):
    """
    Navigate to the facility-booking grid for a venue, zone, and date.

    wait_ms — extra wait AFTER networkidle for the grid to render.  Bookings
    need the full 3000ms because we click immediately after; the scanner can
    use a shorter wait since it only reads the rendered DOM.
    """
    url = (
        f"{BASE_URL}/#/FacilityBooking"
        f"?clubId={club_id}&zoneTypeId={zone_type_id}"
        f"&date={date_str}"
    )
    tag = f" [{label}]" if label else ""
    log.info(f"Opening bookings: clubId={club_id} zone={zone_type_id}{tag} on {date_str}")
    page.goto(url, wait_until="networkidle", timeout=30_000)
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)
    log.info(f"  Page loaded (URL: {page.url})")


def _is_zone_allowed(zone_name):
    """Return True if a zone name is free with membership."""
    return not any(kw.lower() in zone_name.lower() for kw in EXCLUDED_ZONE_KEYWORDS)




def click_book_now(page, target_time, target_date=None):
    """
    Find a 'Book now' button for *target_time* on *target_date* (YYYY-MM-DD)
    and click it.  The date filter is essential — the page shows a 7-day
    grid, so without it the spatial matcher could pick a Book button in
    a different day's column.

    Returns True if a button was found and clicked.
    """
    day_dm = _target_day_dm(target_date) if target_date else ""
    found = page.evaluate(JS_FIND_AND_MARK_SLOT, [target_time, day_dm])
    if not found:
        return False

    btn = page.locator('[data-twbot="1"]').first
    btn.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    btn.click()
    log.info(f"    Clicked 'Book now' for {target_time} on {target_date}")
    return True


def handle_booking_flow(page):
    """
    After clicking 'Book now', a TWO-STEP modal flow appears:

    Step 1 — Court/Time/Duration selection → click "Next"
    Step 2 — Product selection → click "Book"

    Returns:  "success"  |  "too_soon"  |  "error"
    """
    page.wait_for_timeout(3000)

    # ═══════════════════════════════════════════════════════════════════
    # STEP 1: Court / Start-Time / Duration  →  click "Next"
    # ═══════════════════════════════════════════════════════════════════
    page.screenshot(path="debug_step1_modal.png")

    # The modal pre-fills court, start time, and duration from the slot
    # we clicked. Just verify it loaded and click "Next".
    next_clicked = _click_element(page, "Next")
    if not next_clicked:
        log.warning("    Could not find 'Next' button in step-1 modal")
        page.screenshot(path="debug_no_next_btn.png")
        _dismiss_modal(page)
        return "error"

    log.info("    Clicked 'Next' → moving to product selection")
    page.wait_for_timeout(3000)

    # ═══════════════════════════════════════════════════════════════════
    # STEP 2: Product selection  →  click "Book"
    # ═══════════════════════════════════════════════════════════════════
    page.screenshot(path="debug_step2_modal.png")

    # ── Check for "Too soon" error ──────────────────────────────────
    page_text = page.evaluate("() => document.body.innerText")
    if "Too soon" in page_text or "too soon" in page_text.lower():
        log.warning("    Too soon — booking window not open yet for this date")
        _dismiss_modal(page)
        return "too_soon"

    # ── Select the membership product card ──────────────────────────
    for product_text in ["TWAR Member Booking", "Member Booking", "Member"]:
        product = page.locator(f"text='{product_text}'")
        if product.count() > 0 and product.first.is_visible():
            product.first.click()
            log.info(f"    Selected product: {product_text}")
            page.wait_for_timeout(800)
            break

    # ── Click the "Book" confirmation button ────────────────────────
    book_clicked = _click_element(page, "Book")
    if not book_clicked:
        log.warning("    Could not find 'Book' button in step-2 modal")
        page.screenshot(path="debug_no_book_btn.png")
        _dismiss_modal(page)
        return "error"

    log.info("    Clicked 'Book' → confirming ...")
    page.wait_for_timeout(4000)
    page.screenshot(path="debug_after_book.png")

    # ── Post-click validation ───────────────────────────────────────
    page_text = page.evaluate("() => document.body.innerText")
    if "Too soon" in page_text or "too soon" in page_text.lower():
        log.warning("    Booking rejected (too soon)")
        _dismiss_modal(page)
        return "too_soon"

    # Log confirmation details if the success dialog is showing
    if "all set" in page_text.lower() or "See you" in page_text:
        # Extract confirmation details from the dialog
        date_match = re.search(
            r'(\w+day),?\s*(\d{1,2}/\d{1,2}/\d{4})\s*(\d{1,2}:\d{2}\s*[AP]M)',
            page_text,
        )
        if date_match:
            log.info(
                f"    Confirmation: {date_match.group(1)} "
                f"{date_match.group(2)} {date_match.group(3)}"
            )

    log.info("    Booking confirmed!")
    return "success"


def _click_element(page, label):
    """
    Find and click an element whose visible text is exactly *label*.
    Tries role-based, text-based, and JS-based strategies.
    Returns True if clicked.
    """
    # Strategy 1: Playwright role
    btn = page.get_by_role("button", name=label, exact=True)
    if btn.count() > 0 and btn.last.is_visible():
        btn.last.click()
        return True

    # Strategy 2: Playwright link role
    link = page.get_by_role("link", name=label, exact=True)
    if link.count() > 0 and link.last.is_visible():
        link.last.click()
        return True

    # Strategy 3: JS — mark element with exact text, then click via Playwright
    found = page.evaluate("""(label) => {
        document.querySelectorAll('[data-tw-click]')
            .forEach(e => e.removeAttribute('data-tw-click'));
        const els = document.querySelectorAll('button, a, [role="button"], div, span');
        // Collect all matches, pick the last visible one (topmost layer)
        let target = null;
        for (const el of els) {
            const txt = el.textContent.trim();
            if (txt === label && el.offsetParent !== null) {
                const r = el.getBoundingClientRect();
                if (r.width > 15 && r.height > 15) {
                    target = el;
                }
            }
        }
        if (target) {
            target.setAttribute('data-tw-click', '1');
            return true;
        }
        return false;
    }""", label)
    if found:
        page.locator('[data-tw-click="1"]').first.click()
        return True

    # Strategy 4: text selector with has-text (partial match as last resort)
    for sel in [
        f'button:has-text("{label}")',
        f'a:has-text("{label}")',
        f'[role="button"]:has-text("{label}")',
    ]:
        loc = page.locator(sel)
        if loc.count() > 0 and loc.last.is_visible():
            loc.last.click()
            return True

    return False


def _dismiss_modal(page):
    """Try multiple strategies to close the booking modal."""
    for selector in [
        "button:has-text('Back')",
        "button:has-text('Close')",
        '[aria-label="Close"]',
        "button.close",
        '[class*="close"]',
    ]:
        loc = page.locator(selector)
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.click()
            page.wait_for_timeout(800)
            return
    # Fallback: press Escape
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)


# ─────────────────────────────────────────────────────────────────────────────
# Main booking attempt
# ─────────────────────────────────────────────────────────────────────────────

def attempt_booking(
    date_override=None,
    headless=True,
    times_override=None,
    skip_clubs=None,
):
    """
    Run one complete booking attempt.

    Args:
        date_override:    explicit YYYY-MM-DD target (else today + ADVANCE_DAYS)
        headless:         hide the browser window
        times_override:   list of "HH:MM AM/PM" strings overriding the
                          weekday-aware default list
        skip_clubs:       set of club names already booked for this date —
                          skip them so we move on to the next venue

    Returns True if at least one booking was made this attempt.
    """
    date_str = get_target_date(date_override)
    skip_clubs = set(skip_clubs or ())
    log.info("=" * 56)
    log.info(f"  BOOKING ATTEMPT  |  target date: {date_str}")
    if skip_clubs:
        log.info(f"  Skipping already-booked clubs: {sorted(skip_clubs)}")
    log.info("=" * 56)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Reduce automation fingerprint
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        try:
            # Step 1: Log in
            if not do_login(page):
                return False

            too_soon_hit = False
            slot_priorities = times_override or preferred_times_for(date_str)
            log.info(
                f"Time priorities for {date_str}: "
                f"{', '.join(slot_priorities)}"
            )
            booked_any = False
            booked_clubs = set(skip_clubs)

            # Step 2: Try each club → each zone → each time slot
            for club in enabled_clubs():
                if club["name"] in booked_clubs:
                    log.info(f"  Skipping {club['name']} (already booked)")
                    continue
                if too_soon_hit:
                    break

                zones = club.get(
                    "zones",
                    [{"zoneTypeId": club.get("zoneTypeId", 32), "label": ""}],
                )

                for zone in zones:
                    if too_soon_hit:
                        break

                    zone_id = zone["zoneTypeId"]
                    zone_label = zone.get("label", "")

                    open_booking_page(
                        page, club["clubId"], zone_id, date_str, zone_label
                    )

                    for slot_time in slot_priorities:
                        tag = f" [{zone_label}]" if zone_label else ""
                        log.info(f"  Trying {slot_time} at {club['name']}{tag} ...")

                        if not click_book_now(page, slot_time, date_str):
                            log.info(f"    No available slot for {slot_time}")
                            continue

                        result = handle_booking_flow(page)

                        if result == "success":
                            log.info(
                                f"  BOOKED: {slot_time} at {club['name']}{tag}"
                                f" on {date_str}"
                            )
                            page.screenshot(path="booking_success.png")
                            try:
                                from notify import notify_booking_success
                                notify_booking_success(
                                    slot_time, club["name"], zone_label, date_str
                                )
                            except Exception as exc:
                                log.warning(f"  Telegram notify failed: {exc}")
                            booked_clubs.add(club["name"])
                            booked_any = True
                            # Move on to the next venue — we don't double-book
                            # the same venue on the same day.
                            break

                        if result == "too_soon":
                            log.info("  Skipping remaining (window not open yet)")
                            too_soon_hit = True
                            break

                        # result == "error" → try next time slot
                        continue

                    # If the inner loop broke out (success or too_soon),
                    # we also break out of the zone loop.
                    if club["name"] in booked_clubs or too_soon_hit:
                        break

            if not booked_any:
                log.info("No booking made this run.")
            return booked_any

        except Exception as exc:
            log.error(f"Unhandled error: {exc}", exc_info=True)
            try:
                page.screenshot(path="debug_error.png")
            except Exception:
                pass
            return False

        finally:
            browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Tennis World Auto-Booker — books courts 5 days ahead"
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Keep retrying every --interval minutes until a booking is made",
    )
    parser.add_argument(
        "--interval", type=int, default=30,
        help="Minutes between retries in loop mode (default: 30)",
    )
    parser.add_argument(
        "--visible", action="store_true",
        help="Show browser window (useful for debugging)",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Override target date (YYYY-MM-DD) instead of auto-calculating",
    )
    parser.add_argument(
        "--time", type=str, action="append", default=None,
        help=(
            "Override time priorities. May be repeated to give an ordered list. "
            "Accepts '7pm', '7:30pm', '07:00 PM', '19:00', '08:30 AM', etc. "
            "Example: --time 8:30am --time 7am"
        ),
    )
    args = parser.parse_args()

    headless = not args.visible
    times_override = (
        [_normalise_time(t) for t in args.time] if args.time else None
    )

    if args.loop:
        log.info(f"Loop mode — retrying every {args.interval} min until booked")
        attempt_num = 0
        while True:
            attempt_num += 1
            log.info(f"--- Attempt #{attempt_num} ---")
            if attempt_booking(
                date_override=args.date,
                headless=headless,
                times_override=times_override,
            ):
                log.info("Booking confirmed! Exiting loop.")
                break
            log.info(f"Sleeping {args.interval} minutes before next attempt ...")
            time.sleep(args.interval * 60)
    else:
        attempt_booking(
            date_override=args.date,
            headless=headless,
            times_override=times_override,
        )


if __name__ == "__main__":
    main()
