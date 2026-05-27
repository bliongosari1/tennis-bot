#!/usr/bin/env python3
"""
Process a single Telegram command, sent via GitHub repository_dispatch.

The dispatch-telegram.yml workflow calls this script with three env vars:

    TELEGRAM_COMMAND   — e.g. "cancel", "scan", "snipe"
    TELEGRAM_ARGS      — the args string (may be empty)
    TELEGRAM_CHAT_ID   — chat to reply to

This script reuses the dispatch logic from telegram_poller.py, so a command
sent via Vercel webhook behaves exactly the same as one picked up by the
old polling worker.
"""

import os
import sys
import logging

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("tw-process-command")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
log.addHandler(_h)


def main() -> int:
    command = (os.getenv("TELEGRAM_COMMAND") or "").strip().lower()
    args = (os.getenv("TELEGRAM_ARGS") or "").strip()
    chat_id_raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not command:
        log.error("TELEGRAM_COMMAND not set; nothing to do.")
        return 0
    if not chat_id_raw:
        log.error("TELEGRAM_CHAT_ID not set; cannot reply.")
        return 0

    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        log.error(f"Bad chat id: {chat_id_raw!r}")
        return 1

    log.info(f"Processing /{command} {args!r} for chat {chat_id}")

    # Use the same dispatch table as the polling worker.
    from telegram_poller import COMMANDS

    handler = COMMANDS.get(command)
    if not handler:
        from telegram_poller import reply
        log.info(f"Unknown command: /{command}")
        reply(chat_id, f"Unknown command <code>/{command}</code>. Try /help.")
        return 0

    try:
        handler(chat_id, args)
    except Exception as exc:
        log.exception(f"handler /{command} failed: {exc}")
        try:
            from telegram_poller import reply
            reply(chat_id, f"❌ <code>/{command}</code> failed: {exc}")
        except Exception:
            pass
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
