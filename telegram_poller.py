#!/usr/bin/env python3
"""
Telegram command poller.

Polls the bot's getUpdates feed and runs any commands sent in the chat:

    /list                       → list upcoming bookings
    /summary                    → re-run daily_summary
    /cancel_YYYYMMDD_HHMM       → cancel that specific booking
    /cancel_next                → cancel the soonest upcoming booking
    /ping                       → reply 'pong'
    /help                       → show all commands

Designed to be run every ~5 minutes from a cron / GitHub Actions schedule.
Uses Telegram's own server-side offset cursor — once we acknowledge an
update with offset=last+1, it won't be replayed.

Security: only messages from TELEGRAM_CHAT_ID (your group) are honored.
Messages from anywhere else are ignored.
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import notify  # noqa: E402  (after load_dotenv so env is picked up)

log = logging.getLogger("tw-poller")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
log.addHandler(_h)


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ─── Telegram API helpers ────────────────────────────────────────────────────

def _api(method: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_updates(offset: int = 0) -> list:
    res = _api(
        "getUpdates",
        offset=offset,
        timeout=0,
        allowed_updates=json.dumps(["message"]),
    )
    if not res.get("ok"):
        log.warning(f"getUpdates failed: {res}")
        return []
    return res.get("result", [])


def reply(chat_id: int, text: str) -> bool:
    res = _api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview="true",
    )
    if not res.get("ok"):
        log.warning(f"sendMessage failed: {res}")
        return False
    return True


# ─── Command handlers ────────────────────────────────────────────────────────

def cmd_ping(chat_id: int, _args: str) -> None:
    reply(chat_id, "🎾 pong")


def cmd_help(chat_id: int, _args: str) -> None:
    reply(
        chat_id,
        "<b>Tennis Booker commands</b>\n\n"
        "<b>See</b>\n"
        "/list — upcoming bookings (with cancel buttons)\n"
        "/summary — today + next 3 days\n"
        "/scan — find free preferred slots in next 5 days\n"
        "/settings — show current config\n\n"
        "<b>Book</b>\n"
        "/snipe_today\n"
        "/snipe_tomorrow\n"
        "/snipe_fri or /snipe_friday\n"
        "/snipe_tomorrow_8:30am\n"
        "/snipe_2026-05-29_7pm\n\n"
        "<b>Cancel</b>\n"
        "/cancel_next — soonest booking\n"
        "/cancel_mon_2030 — Monday at 8:30 PM\n"
        "/cancel_fri_7pm — Friday at 7 PM\n"
        "/cancel_tue_0830 — Tuesday at 8:30 AM\n\n"
        "<b>Utility</b>\n"
        "/ping — health check\n"
        "/help — this message",
    )


def cmd_list(chat_id: int, _args: str) -> None:
    from daily_summary import fetch_bookings
    from notify import _DAY_ABBREV
    bookings = fetch_bookings(headless=True)
    if not bookings:
        reply(chat_id, "🎾 No upcoming bookings.")
        return

    bookings.sort(key=lambda b: (b["date"], b["time"]))
    lines = ["🎾 <b>Upcoming bookings</b>"]
    for b in bookings:
        d = datetime.strptime(b["date"], "%Y-%m-%d")
        when = d.strftime("%a %d %b")
        site_time = b["time"]  # "08:30 PM"
        hhmm = datetime.strptime(site_time, "%I:%M %p").strftime("%H%M")
        day = _DAY_ABBREV[d.weekday()]
        lines.append(
            f"• <b>{when} {site_time}</b> · {b['club']}\n"
            f"  {b['court']}\n"
            f"  Cancel: /cancel_{day}_{hhmm}"
        )
    reply(chat_id, "\n".join(lines))


def cmd_summary(chat_id: int, _args: str) -> None:
    # Same content as the daily summary.
    from daily_summary import fetch_bookings
    from datetime import timedelta
    bookings = fetch_bookings(headless=True)
    today = datetime.now().date()
    cutoff = today + timedelta(days=4)
    in_range = [
        b for b in bookings
        if today <= datetime.strptime(b["date"], "%Y-%m-%d").date() < cutoff
    ]
    payload = [
        {
            "date": b["date"],
            "time": f"{b['time']}–{b['time_end']}",
            "club": b["club"],
            "zone": b["court"],
        }
        for b in in_range
    ]
    notify.notify_daily_summary(payload)


def _parse_cancel_args(args: str):
    """
    Accept many forms:
        /cancel_20260525_2030      → ('2026-05-25', '20:30')
        /cancel_2026-05-25_2030    → ('2026-05-25', '20:30')
        /cancel_mon_2030           → ('mon',        '20:30')   # day-name
        /cancel_thu_7pm            → ('thu',        '7pm')
        /cancel_tomorrow_8:30am    → ('tomorrow',   '8:30am')
        /cancel_next               → ('next', None)
    Returns the raw date+time tokens — resolution happens downstream.
    """
    args = args.strip()
    if args.lower() in ("next", "soonest", "first"):
        return "next", None
    if "_" not in args:
        return None, None
    date_tok, time_tok = args.rsplit("_", 1)
    return date_tok, time_tok


def cmd_cancel(chat_id: int, args: str) -> None:
    from cancel import cancel_booking, resolve_date
    from daily_summary import fetch_bookings

    date_tok, time_tok = _parse_cancel_args(args)
    if date_tok is None:
        reply(
            chat_id,
            "⚠️ Usage:\n"
            "<code>/cancel_mon_2030</code>  (day name + time)\n"
            "<code>/cancel_fri_7pm</code>\n"
            "<code>/cancel_20260525_2030</code>  (date + 24h)\n"
            "<code>/cancel_next</code>",
        )
        return

    if date_tok == "next":
        bookings = fetch_bookings(headless=True)
        if not bookings:
            reply(chat_id, "🎾 No upcoming bookings to cancel.")
            return
        bookings.sort(key=lambda b: (b["date"], b["time"]))
        target = bookings[0]
        date_str = target["date"]
        time_str = datetime.strptime(target["time"], "%I:%M %p").strftime("%H:%M")
    else:
        try:
            date_str = resolve_date(date_tok)
        except ValueError as exc:
            reply(chat_id, f"⚠️ {exc}")
            return
        time_str = time_tok

    log.info(f"Cancelling booking {date_str} at {time_str}")
    reply(chat_id, f"⏳ Cancelling {date_str} {time_str}…")
    result = cancel_booking(date_str, time_str, headless=True, dry_run=False)
    icon = "✅" if result["ok"] else "❌"
    msg = f"{icon} {result['message']}"

    # If the cancel didn't match an existing booking, append the actual
    # bookings so the user can see what's there and pick the right command.
    if not result["ok"] and "No upcoming booking matches" in result["message"]:
        try:
            from notify import _DAY_ABBREV
            current = fetch_bookings(headless=True)
            if current:
                current.sort(key=lambda b: (b["date"], b["time"]))
                msg += "\n\n<b>Your actual bookings:</b>"
                for b in current:
                    d = datetime.strptime(b["date"], "%Y-%m-%d")
                    day = _DAY_ABBREV[d.weekday()]
                    when = d.strftime("%a %d %b")
                    hhmm = datetime.strptime(b["time"], "%I:%M %p").strftime("%H%M")
                    msg += (
                        f"\n• <b>{when} {b['time']}</b> · {b['club']}"
                        f"\n   /cancel_{day}_{hhmm}"
                    )
            else:
                msg += "\n\nYou have no upcoming bookings."
        except Exception as exc:
            log.warning(f"could not append bookings list: {exc}")

    reply(chat_id, msg)


def cmd_snipe(chat_id: int, args: str) -> None:
    """
    /snipe_today                → book today (any preferred time)
    /snipe_tomorrow             → book tomorrow
    /snipe_mon                  → book next Monday
    /snipe_2026-05-26           → book that exact date
    /snipe_tomorrow_8:30am      → book tomorrow at 8:30 AM specifically
    /snipe_fri_7pm              → book Friday at 7 PM specifically
    """
    from book import attempt_booking
    from cancel import resolve_date
    from notify import _fmt_date

    args = args.strip()
    if not args:
        reply(
            chat_id,
            "⚠️ Usage:\n"
            "<code>/snipe_today</code>\n"
            "<code>/snipe_tomorrow</code>\n"
            "<code>/snipe_fri</code>\n"
            "<code>/snipe_tomorrow_8:30am</code>\n"
            "<code>/snipe_2026-05-26_7pm</code>",
        )
        return

    # Optional time after the last underscore.
    if "_" in args:
        date_tok, time_tok = args.rsplit("_", 1)
        # If the right half isn't a time, treat the whole thing as a date.
        try:
            from book import _normalise_time
            time_norm = _normalise_time(time_tok)
        except ValueError:
            date_tok, time_norm = args, None
    else:
        date_tok, time_norm = args, None

    try:
        iso_date = resolve_date(date_tok)
    except ValueError as exc:
        reply(chat_id, f"⚠️ {exc}")
        return

    times_msg = f" @ {time_norm}" if time_norm else ""
    reply(
        chat_id,
        f"⏳ Sniping {_fmt_date(iso_date)}{times_msg}…",
    )
    times_override = [time_norm] if time_norm else None
    booked = attempt_booking(
        date_override=iso_date,
        headless=True,
        times_override=times_override,
    )
    if booked:
        reply(chat_id, f"✅ Booked something on {_fmt_date(iso_date)}.")
    else:
        reply(
            chat_id,
            f"❌ Couldn't book {_fmt_date(iso_date)}{times_msg}. "
            f"Probably no free slot or outside the 5-day window.",
        )


def cmd_scan(chat_id: int, _args: str) -> None:
    """Find free preferred slots in the next few days and report."""
    from scan import scan_and_message
    import settings as S
    reply(
        chat_id,
        f"⏳ Scanning {S.SCAN_LOOKAHEAD_DAYS} days × all venues — about "
        f"30 s per day on Fly, so ~{S.SCAN_LOOKAHEAD_DAYS * 30}s total. "
        f"You'll see a tick per day as I go.",
    )

    # Edit the same status message after each date for a clean progress feel.
    progress_seen = [0]
    def on_progress(date_str, slots_so_far):
        progress_seen[0] += 1
        n = len(slots_so_far)
        # Send a brief tick — only every other date to avoid spam.
        if progress_seen[0] == S.SCAN_LOOKAHEAD_DAYS:
            return  # final reply will cover this
        d = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b")
        reply(
            chat_id,
            f"… {progress_seen[0]}/{S.SCAN_LOOKAHEAD_DAYS} done "
            f"({d}) — {n} free slot(s) so far",
        )

    fresh, message = scan_and_message(headless=True, on_progress=on_progress)
    if message:
        reply(chat_id, message)
    else:
        reply(chat_id, "🎾 No free preferred slots in your lookahead window.")


def cmd_settings(chat_id: int, _args: str) -> None:
    import settings as S
    venues = ", ".join(
        f"{v['name']}{'' if v.get('enabled', True) else ' (off)'}"
        for v in S.VENUES
    )
    reply(
        chat_id,
        "<b>Current settings</b>\n"
        f"Venues: {venues}\n"
        f"Max bookings/venue/day: {S.MAX_BOOKINGS_PER_VENUE_PER_DAY}\n"
        f"Advance days: {S.ADVANCE_DAYS}\n"
        f"Scan lookahead: {S.SCAN_LOOKAHEAD_DAYS} days\n"
        f"Scan auto-book: {'on' if S.SCAN_AUTO_BOOK else 'off'}\n"
        f"Evening times: {', '.join(S.EVENING_TIMES)}\n"
        f"Morning times: {', '.join(S.MORNING_TIMES)}",
    )


COMMANDS = {
    "ping":     cmd_ping,
    "help":     cmd_help,
    "start":    cmd_help,
    "list":     cmd_list,
    "summary":  cmd_summary,
    "cancel":   cmd_cancel,
    "snipe":    cmd_snipe,
    "scan":     cmd_scan,
    "settings": cmd_settings,
}


# ─── Main poll loop ──────────────────────────────────────────────────────────

def _parse_command(text: str):
    """
    Extract (command_name, args) from a Telegram message.
    Handles:
        /ping
        /ping@tennismelbparkbot
        /cancel_20260525_2030
        /cancel_20260525_2030@tennismelbparkbot
        /cancel 20260525 2030
    Returns (None, None) for non-commands.
    """
    text = text.strip()
    if not text.startswith("/"):
        return None, None

    # Strip leading slash and split on first space.
    rest = text[1:]
    if " " in rest:
        head, args = rest.split(" ", 1)
    else:
        head, args = rest, ""

    # Strip @botname suffix.
    if "@" in head:
        head = head.split("@", 1)[0]

    # Underscore syntax:  /cancel_20260525_2030  →  command='cancel', args='20260525_2030'
    if "_" in head:
        cmd, arg_part = head.split("_", 1)
        if args:
            args = arg_part + " " + args
        else:
            args = arg_part
    else:
        cmd = head

    return cmd.lower(), args


def process_update(upd: dict) -> int | None:
    """Process a single update.  Returns the update_id (used as the new offset base)."""
    update_id = upd.get("update_id")
    msg = upd.get("message")
    if not msg:
        return update_id

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        log.info(f"Ignoring message from chat {chat_id} (not allowed)")
        return update_id

    text = msg.get("text", "") or ""
    cmd, args = _parse_command(text)
    if not cmd:
        return update_id

    handler = COMMANDS.get(cmd)
    if not handler:
        log.info(f"Unknown command: /{cmd}")
        reply(chat_id, f"Unknown command <code>/{cmd}</code>. Try /help.")
        return update_id

    log.info(f"Running /{cmd} {args!r} for chat {chat_id}")
    try:
        handler(chat_id, args)
    except Exception as exc:
        log.exception(f"handler /{cmd} failed: {exc}")
        try:
            reply(chat_id, f"❌ <code>/{cmd}</code> failed: {exc}")
        except Exception:
            pass
    return update_id


def main() -> int:
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set; nothing to do.")
        return 0
    if not ALLOWED_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set; all chats will be accepted (insecure).")

    updates = get_updates()
    log.info(f"Got {len(updates)} update(s)")
    last = None
    for upd in updates:
        last = process_update(upd) or last

    # Acknowledge by re-polling with offset = last + 1 — this clears
    # the server-side queue so we don't re-process.
    if last is not None:
        get_updates(offset=last + 1)
        log.info(f"Acknowledged updates up to {last}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
