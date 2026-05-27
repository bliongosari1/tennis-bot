#!/usr/bin/env python3
"""
Tennis Booker — Fly.io entrypoint.

Single long-running process that does everything in one place:

    1. Telegram long-polling (~1s response time for any command).
    2. APScheduler cron jobs (sniper, scanner, daily summary).
    3. Threaded command execution so the long-poll is never blocked.

Replaces the old Vercel + GitHub Actions split (which broke when the
account's GH billing got disabled).

ENV VARS (set with `fly secrets set`):
    TW_EMAIL, TW_PASSWORD
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TZ=Australia/Melbourne  (set in fly.toml)
"""

import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────
log = logging.getLogger("tw-fly")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s | %(name)-15s | %(levelname)-7s | %(message)s"))
log.addHandler(_h)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

import telegram_poller  # noqa: E402

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Serial worker — running two Playwright browsers in parallel on a 1 GB VM
# causes CPU contention, JS evals time out, and click_book_now misses slots
# it would otherwise see.  Commands queue and run one at a time.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tw-cmd")


# ─── Telegram long-polling ─────────────────────────────────────────────────

def _run_command(command: str, args: str, chat_id: int):
    """Dispatch a command in the worker thread."""
    handler = telegram_poller.COMMANDS.get(command)
    if not handler:
        try:
            telegram_poller.reply(
                chat_id, f"Unknown command <code>/{command}</code>. Try /help."
            )
        except Exception:
            pass
        return
    log.info(f"Running /{command} {args!r} for chat {chat_id}")
    try:
        handler(chat_id, args)
    except Exception as exc:
        log.exception(f"/{command} failed: {exc}")
        try:
            telegram_poller.reply(chat_id, f"❌ <code>/{command}</code> failed: {exc}")
        except Exception:
            pass


def telegram_long_poll():
    """Long-poll Telegram and dispatch commands."""
    log.info("Starting Telegram long-poll")
    offset = 0
    while True:
        try:
            # 30-second long poll — Telegram returns as soon as a message
            # arrives, so response time is ~50ms for new updates.
            import json, urllib.parse, urllib.request
            params = urllib.parse.urlencode({
                "offset": offset,
                "timeout": 30,
                "allowed_updates": json.dumps(["message"]),
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                data=params,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning(f"getUpdates failed: {exc}; sleeping 5s")
            time.sleep(5)
            continue

        if not body.get("ok"):
            log.warning(f"getUpdates returned not ok: {body}")
            time.sleep(5)
            continue

        for upd in body.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
                log.info(f"Ignoring chat {chat_id}")
                continue
            text = msg.get("text", "") or ""
            cmd, args = telegram_poller._parse_command(text)
            if not cmd:
                continue
            executor.submit(_run_command, cmd, args, chat_id)


# ─── Scheduled jobs ─────────────────────────────────────────────────────────

def _job_sniper():
    """Run the sniper for today's session."""
    log.info("[cron] sniper starting")
    try:
        from scheduler import run_scheduler
        run_scheduler(headless=True, test_now=True)
    except Exception as exc:
        log.exception(f"[cron] sniper failed: {exc}")
        try:
            from notify import send
            send(f"❌ Sniper crashed: {exc}")
        except Exception:
            pass


def _job_daily_summary():
    log.info("[cron] daily summary starting")
    try:
        from daily_summary import fetch_bookings
        from notify import notify_daily_summary
        from datetime import timedelta

        bookings = fetch_bookings(headless=True)
        today = datetime.now().date()
        cutoff = today + timedelta(days=4)
        payload = []
        for b in bookings:
            try:
                d = datetime.strptime(b["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if today <= d < cutoff:
                payload.append({
                    "date": b["date"],
                    "time": f"{b['time']}–{b['time_end']}",
                    "club": b["club"],
                    "zone": b["court"],
                })
        notify_daily_summary(payload)
    except Exception as exc:
        log.exception(f"[cron] daily summary failed: {exc}")


def _job_scanner():
    log.info("[cron] scanner starting")
    try:
        from scan import scan_and_message
        from notify import send
        fresh, message = scan_and_message(headless=True)
        if message:
            send(message)
        # quiet when no slots — no message
    except Exception as exc:
        log.exception(f"[cron] scanner failed: {exc}")


def setup_cron(scheduler: BackgroundScheduler):
    tz = "Australia/Melbourne"
    # Evening sniper Wed–Sun 6:30 PM Melbourne (booking weekday slots 5 days out)
    scheduler.add_job(
        _job_sniper, CronTrigger(day_of_week="wed,thu,fri,sat,sun",
                                 hour=18, minute=30, timezone=tz),
        id="evening_sniper", replace_existing=True,
    )
    # Morning sniper Mon/Tue 9:30 AM Melbourne (booking weekend slots 5 days out)
    scheduler.add_job(
        _job_sniper, CronTrigger(day_of_week="mon,tue",
                                 hour=9, minute=30, timezone=tz),
        id="morning_sniper", replace_existing=True,
    )
    # Daily summary at 8 AM Melbourne
    scheduler.add_job(
        _job_daily_summary, CronTrigger(hour=8, minute=0, timezone=tz),
        id="daily_summary", replace_existing=True,
    )
    # Scanner every 15 min during waking hours (7 AM – 11 PM Melbourne)
    scheduler.add_job(
        _job_scanner,
        CronTrigger(minute="*/15", hour="7-22", timezone=tz),
        id="scanner", replace_existing=True,
    )
    log.info(f"Cron jobs scheduled: {[j.id for j in scheduler.get_jobs()]}")


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set")
        return 1
    if not ALLOWED_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set — accepting all chats")

    log.info("=" * 60)
    log.info("  TENNIS BOOKER — Fly.io single-process mode")
    log.info(f"  bot: {BOT_TOKEN[:10]}…  allowed_chat: {ALLOWED_CHAT_ID}")
    log.info("=" * 60)

    # Start cron in background thread.
    sched = BackgroundScheduler(timezone="Australia/Melbourne")
    setup_cron(sched)
    sched.start()

    # Send a startup ping so we know the process is alive after a restart.
    try:
        from notify import send
        send(
            "🚀 Tennis Booker online "
            f"({datetime.now().strftime('%a %d %b %H:%M')} Melb)"
        )
    except Exception as exc:
        log.warning(f"startup ping failed: {exc}")

    try:
        telegram_long_poll()
    except KeyboardInterrupt:
        log.info("shutdown requested")
        sched.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
