# Tennis World Auto-Booker

Snipes free tennis courts at **Tennis World Melbourne Park** and
**Albert Reserve** the second they become available, plus a Telegram bot for
viewing, sniping on-demand, and cancelling — all running as a single
always-on Fly.io process.

## What it books

| Day you book | Target session | Slot priority |
|---|---|---|
| Wed–Sun (booking weekdays) | **Evening** 7–9 PM | 8 PM → 8:30 → 7:30 → 7:00 → 9:00 |
| Mon–Tue (booking weekend) | **Morning** 10–12 PM | 11 AM → 10 AM → 12 PM |

- Books **5 days in advance** at the exact minute slots open.
- Tries Melbourne Park first (4 free zones), Albert Reserve as fallback.
- Multi-venue: books up to 1 hour per venue per day (so up to 2 bookings/day).

All knobs live in `settings.py` — edit one file and book/scheduler/scanner all pick up the change.

## Telegram bot

Add your bot to a group, set the secrets, and you get:

| Pattern | Example | What it does |
|---|---|---|
| `/list` | | upcoming bookings with cancel commands |
| `/summary` | | today + next 3 days |
| `/scan` | | live scan for free preferred slots |
| `/settings` | | dump current config |
| `/snipe_today` | | try to book today |
| `/snipe_tomorrow` | | try to book tomorrow |
| `/snipe_<day>` | `/snipe_fri` | try next Friday |
| `/snipe_<date>` | `/snipe_2026-05-29` | try a specific date |
| `/snipe_<when>_<time>` | `/snipe_tomorrow_8:30am` | try a specific date+time |
| `/cancel_next` | | cancel the soonest booking |
| `/cancel_<day>_<time>` | `/cancel_mon_2030`, `/cancel_fri_7pm` | cancel by day + time |
| `/cancel_<date>_<time>` | `/cancel_20260525_2030` | cancel by ISO date + time |
| `/ping`, `/help` | | health check, help text |

Plus automatic messages:

- **Instant** when a booking succeeds
- **Daily 8 AM** summary of today + next 3 days, with tappable cancel buttons
- **Every 15 min** (waking hours) if the scanner finds a new free slot from someone's cancellation

> ⚠️ Tennis World charges if you cancel **less than 6 hours** before the booking.
> The bot does NOT enforce this — it'll cancel whenever you ask.

## Tennis World booking rules (the gotchas)

These are server-side rules, not bot bugs:

1. **5-day window.** Slots open exactly 5 days ahead at the same wall-clock time. Trying to book 1–4 days ahead returns "Too soon to book".
2. **Second-hour rule.** If you already have a booking on day X, adding another booking on day X is treated as a "second hour" and is blocked unless the new slot is within 24 h.
3. **Spatial matching.** A time can appear on the calendar without a "Book now" button (already booked by someone else). The bot uses spatial matching — the Book button must be in the same cell card as the time — to avoid grabbing a neighbour cell's button by mistake.

## Hosting — Fly.io

Single always-on machine in `syd`, running `fly_main.py`:

```
fly_main.py (single Python process)
├── Telegram long-poll loop          ← ~1 s response to commands
├── ThreadPoolExecutor (max 2)        ← Playwright workers
└── APScheduler (BackgroundScheduler) ← cron jobs:
        • evening sniper  Wed–Sun 18:30 Melb
        • morning sniper  Mon–Tue 09:30 Melb
        • daily summary   every day 08:00 Melb
        • scanner         every 15 min, 07:00–22:59 Melb
```

### Why Fly and not GH Actions

GitHub Actions cron is unreliable for low-latency Telegram interaction (5-min minimum + GH skips ticks under load), and a billing failure on the account disables Actions entirely — even on public repos. Fly.io's free allowance comfortably covers a single 1 GB always-on machine.

### Deploy

```bash
# One-time setup
fly auth login
fly apps create tennis-booker --org personal
fly secrets set TW_EMAIL=... TW_PASSWORD=... \
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...

# Each deploy
fly deploy --ha=false
fly logs        # tail
fly status      # see the running machine
fly machine start <id>   # if Fly stopped it after a deploy
```

### Important: delete the Telegram webhook if you ever set one

Long-polling and webhook are mutually exclusive. If `getWebhookInfo` shows a URL, the long-poll loop will quietly fail:

```bash
curl -F "url=" "https://api.telegram.org/bot<TOKEN>/setWebhook"
```

## Files

| File | Purpose |
|---|---|
| `fly_main.py` | Entrypoint — Telegram long-poll + APScheduler + thread pool |
| `Dockerfile`, `fly.toml`, `.dockerignore` | Fly.io image and config |
| `settings.py` | All knobs — venues, time priorities, retry tuning |
| `book.py` | One-shot booking. Spatial-matching Book-button detection |
| `scheduler.py` | Multi-venue sniper session |
| `cancel.py` | Playwright cancel by date/day + time |
| `scan.py` | Scans all venues for free preferred slots |
| `notify.py` | Telegram helper (no deps, stdlib HTTP) |
| `daily_summary.py` | Scrapes My Bookings, sends Telegram digest |
| `telegram_poller.py` | Dispatches `/cancel`, `/snipe`, etc. commands |
| `process_command.py` | Single-command runner (used by legacy GH dispatch) |
| `webhook/` | Old Vercel webhook (kept for reference; no longer used) |
| `.github/workflows/*.yml` | Schedule-disabled fallback workflows |
| `com.tennisbooker.*.plist` | macOS launchd agents (alternative to Fly) |

## Local install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

`.env`:

```
TW_EMAIL=...
TW_PASSWORD=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Useful local commands

```bash
python fly_main.py                       # mimic Fly entrypoint locally
python book.py --date 2026-05-29 --time 7pm
python scheduler.py --test-now
python cancel.py mon 8:30pm              # day-name form
python cancel.py --next --dry-run
python scan.py --no-telegram             # print results, don't ping
python daily_summary.py --days 4         # today + next 3
python notify.py "hello"                 # smoke-test Telegram
```

## Logs

| File | Purpose |
|---|---|
| `fly logs` | live Fly machine output |
| `scheduler.log`, `booker.log` | local run logs |
| `scheduler_state.json` | tracks today's booking state |
| `booking_success.png`, `debug_*.png` | step-by-step screenshots |
