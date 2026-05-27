#!/usr/bin/env python3
"""
Telegram notification helper.

Requires two environment variables:
    TELEGRAM_BOT_TOKEN   – the token you got from @BotFather
    TELEGRAM_CHAT_ID     – your personal chat id (talk to @userinfobot)

Both are optional — if either is missing every send() call is a silent no-op,
so the rest of the bot keeps working.
"""

import logging
import os
import sys
from datetime import datetime
from typing import Optional

import urllib.request
import urllib.parse
import json

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("tw-notify")
if not log.handlers:
    log.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
    log.addHandler(h)


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _is_configured() -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    # The Telegram docs use this exact string as their placeholder example.
    if BOT_TOKEN.startswith("123456789:ABCdefGhIJKlmNoPQRstuVWXyz"):
        log.warning(
            "TELEGRAM_BOT_TOKEN looks like the placeholder example "
            "from the Telegram docs — replace it with a real token."
        )
        return False
    return True


def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message.  Returns True on success."""
    if not _is_configured():
        log.info("[telegram] not configured — skipping message")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                log.warning(f"[telegram] api returned: {body}")
                return False
            log.info("[telegram] sent")
            return True
    except Exception as exc:
        log.warning(f"[telegram] send failed: {exc}")
        return False


# ─── Pretty helpers used throughout the bot ──────────────────────────────────

def _fmt_date(date_str: str) -> str:
    """Convert 2026-05-28 → Thu 28 May."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%a %d %b")
    except Exception:
        return date_str


def notify_booking_success(
    slot_time: str,
    club_name: str,
    zone_label: str,
    target_date: str,
) -> bool:
    zone = f" — {zone_label}" if zone_label else ""
    cancel = _cancel_command({"date": target_date, "time": slot_time})
    lines = [
        "🎾 <b>Court booked!</b>",
        f"<b>{_fmt_date(target_date)}</b> at <b>{slot_time}</b>",
        f"{club_name}{zone}",
    ]
    if cancel:
        lines.append("")
        lines.append(f"Cancel: {cancel}")
        lines.append("<i>(must cancel ≥6h before to avoid charge)</i>")
    return send("\n".join(lines))


def notify_session_no_booking(session_label: str, target_date: str) -> bool:
    text = (
        "⚠️ <b>No court booked</b>\n"
        f"{session_label.title()} session on "
        f"{datetime.now().strftime('%a %d %b')} finished without a slot for "
        f"<b>{_fmt_date(target_date)}</b>."
    )
    return send(text)


_DAY_ABBREV = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _cancel_command(b: dict) -> str:
    """Build the tappable /cancel_<day>_<HHMM> command for a booking."""
    date_raw = b.get("date", "")
    time_raw = (b.get("time", "") or "").split("–")[0].strip()
    try:
        d = datetime.strptime(date_raw, "%Y-%m-%d")
        t = datetime.strptime(time_raw, "%I:%M %p")
        day = _DAY_ABBREV[d.weekday()]
        return f"/cancel_{day}_{t.strftime('%H%M')}"
    except Exception:
        return ""


def notify_daily_summary(bookings: list) -> bool:
    """
    bookings: list of dicts with keys date, time, club, zone (any optional).
    Highlights TODAY's bookings, then lists the next few days underneath.
    Each row gets a tappable /cancel_YYYYMMDD_HHMM command (cancel takes
    effect within ~5 min via the telegram poller).
    """
    today = datetime.now().date()
    today_str = today.strftime("%a %d %b")

    today_bookings = []
    upcoming_bookings = []
    for b in bookings:
        try:
            d = datetime.strptime(b.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        (today_bookings if d == today else upcoming_bookings).append(b)

    upcoming_bookings.sort(key=lambda b: (b.get("date", ""), b.get("time", "")))

    lines = [f"🎾 <b>Tennis — {today_str}</b>"]

    if today_bookings:
        lines.append("")
        lines.append("<b>Today</b>")
        for b in today_bookings:
            tm = b.get("time", "")
            club = b.get("club", "")
            zone = b.get("zone", "")
            suffix = f" — {zone}" if zone else ""
            cancel = _cancel_command(b)
            lines.append(f"🟢 <b>{tm}</b> · {club}{suffix}")
            if cancel:
                lines.append(f"   {cancel}")
    else:
        lines.append("")
        lines.append("<i>No booking today.</i>")

    if upcoming_bookings:
        lines.append("")
        lines.append("<b>Coming up</b>")
        for b in upcoming_bookings:
            date_part = _fmt_date(b.get("date", ""))
            tm = b.get("time", "")
            club = b.get("club", "")
            zone = b.get("zone", "")
            suffix = f" — {zone}" if zone else ""
            cancel = _cancel_command(b)
            lines.append(f"• {date_part} <b>{tm}</b> · {club}{suffix}")
            if cancel:
                lines.append(f"   {cancel}")

    lines.append("")
    lines.append("<i>Tap /list any time · cancel ≥6h before to avoid charge</i>")
    return send("\n".join(lines))


if __name__ == "__main__":
    # CLI smoke test:  python notify.py "hello from tennis bot"
    msg = " ".join(sys.argv[1:]) or "🎾 tennis-booker notify.py smoke test"
    ok = send(msg)
    sys.exit(0 if ok else 1)
