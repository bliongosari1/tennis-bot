#!/usr/bin/env python3
"""
Helper: discover your Telegram chat/group ID from the bot's updates.

Usage:
    1. Make sure your bot is a member of the group (already done — your
       screenshot shows Tennis Booker is in the "Tennis bookings" group).
    2. In the group, send ANY message (e.g. "/ping").  This is required —
       Telegram only exposes a chat in getUpdates after a fresh message.
    3. Run:
           export TELEGRAM_BOT_TOKEN='<real-token-from-BotFather>'
           python get_chat_id.py
    4. The script prints every chat that has talked to your bot recently.
       Copy the negative number for "Tennis bookings".

If you don't see your group:
    - Verify the bot really is a member.
    - Disable Group Privacy (BotFather → /mybots → your bot → Bot Settings
      → Group Privacy → Turn off) so the bot can see all messages.
    - Send another message in the group, then re-run this script.
"""

import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
    print("  export TELEGRAM_BOT_TOKEN='<real-token-from-BotFather>'",
          file=sys.stderr)
    sys.exit(1)

if token.startswith("123456789:ABCdefGhIJKlmNoPQRstuVWXyz"):
    print("ERROR: That's the Telegram docs placeholder, not a real token.",
          file=sys.stderr)
    print("       Get the real one from @BotFather (the message that starts",
          file=sys.stderr)
    print("       with 'Done! Congratulations on your new bot.').",
          file=sys.stderr)
    sys.exit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
try:
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except Exception as exc:
    print(f"ERROR: API call failed: {exc}", file=sys.stderr)
    sys.exit(1)

if not body.get("ok"):
    print(f"ERROR: Telegram returned: {body}", file=sys.stderr)
    sys.exit(1)

updates = body.get("result", [])
if not updates:
    print("No updates yet — send a message in your bot's chat/group first,")
    print("then re-run this script.")
    sys.exit(0)

# Collect unique chats.
seen = {}
for upd in updates:
    msg = (
        upd.get("message")
        or upd.get("channel_post")
        or upd.get("my_chat_member", {}).get("chat")
        and {"chat": upd["my_chat_member"]["chat"]}
        or {}
    )
    chat = msg.get("chat") if isinstance(msg, dict) else None
    if not chat:
        continue
    cid = chat.get("id")
    if cid in seen:
        continue
    seen[cid] = chat

print(f"Found {len(seen)} chat(s):\n")
for cid, chat in seen.items():
    title = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
    kind = chat.get("type", "?")
    print(f"  Chat ID: {cid}")
    print(f"     name: {title}")
    print(f"     type: {kind}")
    print()
    print(f"  → run:  gh secret set TELEGRAM_CHAT_ID --body \"{cid}\"")
    print()
