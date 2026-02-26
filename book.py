#!/usr/bin/env python3
"""
Tennis World Auto-Booker
========================
Automatically books tennis courts at Tennis World (Melbourne Park / Albert Park).
Books 5 days in advance, targeting 6–9 PM slots (preferring 8 PM).

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
#
# HOW TO FIND THE IDs:
#   1. Go to tennisworld.perfectgym.com.au  →  Court Bookings
#   2. Pick the venue (e.g. Melbourne Park)
#   3. Click the "Facility Type" dropdown and select a court type
#   4. Look at the URL: ?clubId=X&zoneTypeId=Y
#   5. Add an entry below with those values
#
# Only add the FREE court types (skip Show Court, Indoor, Physio).
CLUBS = [
    # ── Albert Reserve (confirmed) ─────────────────────────────────────
    {
        "name": "Albert Reserve",
        "clubId": 3,
        "zones": [
            {"zoneTypeId": 32, "label": "Full court Albert Reserve"},
        ],
    },
    # ── Melbourne Park ─────────────────────────────────────────────────
    # TODO: Click "Change club" → Melbourne Park, then for each free
    #       Facility Type, copy clubId & zoneTypeId from the URL.
    #       Only add the FREE zones (skip Show Court, Indoor, Physio).
    # {
    #     "name": "Melbourne Park",
    #     "clubId": ??,          # ← check URL after switching club
    #     "zones": [
    #         {"zoneTypeId": ??, "label": "Western Courts (Outdoor Full Court)"},
    #         {"zoneTypeId": ??, "label": "Eastern Courts (Full Court)"},
    #         {"zoneTypeId": ??, "label": "NTC Outdoor Courts"},
    #         {"zoneTypeId": ??, "label": "NTC Eastern Courts"},
    #     ],
    # },
]

# Slot times to try, in preference order (first match wins).
# The page displays times as "HH:MM PM" with leading zeros.
PREFERRED_TIMES = [
    "08:00 PM",
    "08:30 PM",
    "07:30 PM",
    "07:00 PM",
    "09:00 PM",
    "06:30 PM",
    "06:00 PM",
]

# Booking window — slots open exactly this many days before the session date
ADVANCE_DAYS = 5

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

# Walks the DOM to find a "Book now" button that belongs to a given time slot.
# Marks it with a data attribute so Playwright can locate and click it properly.
JS_FIND_AND_MARK_SLOT = """
(targetTime) => {
    // Clear any previous markers
    document.querySelectorAll('[data-twbot]')
        .forEach(e => e.removeAttribute('data-twbot'));

    // Find leaf-level elements whose own text content matches the target time
    const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_ELEMENT
    );
    const timeHits = [];
    while (walker.nextNode()) {
        const el = walker.currentNode;
        // "own text" = concatenation of direct text-node children only
        const ownText = Array.from(el.childNodes)
            .filter(n => n.nodeType === 3)
            .map(n => n.textContent.trim())
            .join(' ')
            .trim();
        if (ownText === targetTime && el.offsetParent !== null) {
            timeHits.push(el);
        }
    }

    // For each matching time element, walk up the DOM to find
    // the nearest container that also holds a "Book now" button.
    for (const hit of timeHits) {
        let container = hit.parentElement;
        for (let depth = 0; depth < 10 && container; depth++) {
            const rect = container.getBoundingClientRect();
            if (rect.height > 500) break;   // too large — probably the whole grid

            const candidates = container.querySelectorAll(
                'button, a, [role="button"], span, div'
            );
            for (const el of candidates) {
                if (el.textContent.trim() === 'Book now'
                    && el.offsetParent !== null) {
                    el.setAttribute('data-twbot', '1');
                    return true;
                }
            }
            container = container.parentElement;
        }
    }
    return false;
}
"""

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


def open_booking_page(page, club_id, zone_type_id, date_str, label=""):
    """Navigate to the facility-booking grid for a venue, zone, and date."""
    url = (
        f"{BASE_URL}/#/FacilityBooking"
        f"?clubId={club_id}&zoneTypeId={zone_type_id}"
        f"&date={date_str}"
    )
    tag = f" [{label}]" if label else ""
    log.info(f"Opening bookings: clubId={club_id} zone={zone_type_id}{tag} on {date_str}")
    page.goto(url, wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(3000)
    log.info(f"  Page loaded (URL: {page.url})")


def _is_zone_allowed(zone_name):
    """Return True if a zone name is free with membership."""
    return not any(kw.lower() in zone_name.lower() for kw in EXCLUDED_ZONE_KEYWORDS)




def click_book_now(page, target_time):
    """
    Find a 'Book now' button for *target_time* on the grid, and click it.
    Returns True if a button was found and clicked.
    """
    found = page.evaluate(JS_FIND_AND_MARK_SLOT, target_time)
    if not found:
        return False

    btn = page.locator('[data-twbot="1"]').first
    btn.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    btn.click()
    log.info(f"    Clicked 'Book now' for {target_time}")
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

def attempt_booking(date_override=None, headless=True):
    """
    Run one complete booking attempt.
    Returns True if a court was successfully booked.
    """
    date_str = get_target_date(date_override)
    log.info("=" * 56)
    log.info(f"  BOOKING ATTEMPT  |  target date: {date_str}")
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

            # Step 2: Try each club → each zone → each time slot
            for club in CLUBS:
                if too_soon_hit:
                    break

                zones = club.get("zones", [{"zoneTypeId": club.get("zoneTypeId", 32), "label": ""}])

                for zone in zones:
                    if too_soon_hit:
                        break

                    zone_id = zone["zoneTypeId"]
                    zone_label = zone.get("label", "")

                    open_booking_page(
                        page, club["clubId"], zone_id, date_str, zone_label
                    )

                    for slot_time in PREFERRED_TIMES:
                        tag = f" [{zone_label}]" if zone_label else ""
                        log.info(f"  Trying {slot_time} at {club['name']}{tag} ...")

                        if not click_book_now(page, slot_time):
                            log.info(f"    No available slot for {slot_time}")
                            continue

                        result = handle_booking_flow(page)

                        if result == "success":
                            log.info(
                                f"  BOOKED: {slot_time} at {club['name']}{tag}"
                                f" on {date_str}"
                            )
                            page.screenshot(path="booking_success.png")
                            return True

                        if result == "too_soon":
                            log.info("  Skipping remaining (window not open yet)")
                            too_soon_hit = True
                            break

                        # result == "error" → try next time slot
                        continue

            log.info("No booking made this run.")
            return False

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
    args = parser.parse_args()

    headless = not args.visible

    if args.loop:
        log.info(f"Loop mode — retrying every {args.interval} min until booked")
        attempt_num = 0
        while True:
            attempt_num += 1
            log.info(f"--- Attempt #{attempt_num} ---")
            if attempt_booking(date_override=args.date, headless=headless):
                log.info("Booking confirmed! Exiting loop.")
                break
            log.info(f"Sleeping {args.interval} minutes before next attempt ...")
            time.sleep(args.interval * 60)
    else:
        attempt_booking(date_override=args.date, headless=headless)


if __name__ == "__main__":
    main()
