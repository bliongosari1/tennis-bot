# Tennis World Auto-Booker

Automatically books tennis courts at **Tennis World** (Melbourne Park / Albert Park).
Targets slots **5 days in advance**, preferring **8 PM** and trying the **6–9 PM** range.

## How it works

Slots open **exactly 5 days before the session time**. So a 6:00 PM Friday slot
becomes bookable at 6:00 PM Sunday, a 6:30 PM slot at 6:30 PM, etc.

There are **two modes**:

| | `book.py` | `scheduler.py` |
|---|---|---|
| **Purpose** | One-shot booking attempt | Competitive daily sniper |
| **Speed** | Logs in fresh each run | Pre-logged-in, fires at the exact second |
| **Best for** | Quick manual test | Running every day automatically |

## Deploy free on GitHub Actions (recommended)

This runs the sniper every day on GitHub's servers — your laptop can be off.

### 1. Create a public GitHub repo

```bash
cd /Users/brandonvincent/Documents/personal/tennis-booker

# Make sure .env is NOT committed (it's in .gitignore)
git add -A
git commit -m "tennis booking bot"

# Create the repo on GitHub (requires gh CLI)
gh repo create tennis-booker --public --source=. --push
```

Or create the repo manually at https://github.com/new, then:
```bash
git remote add origin https://github.com/YOUR_USERNAME/tennis-booker.git
git push -u origin main
```

> **Public repo = unlimited free GitHub Actions minutes.**
> Your credentials are NOT in the code — they go in GitHub Secrets (next step).

### 2. Add your credentials as GitHub Secrets

Go to your repo on GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret name | Value |
|---|---|
| `TW_EMAIL` | `brandonvincent567@gmail.com` |
| `TW_PASSWORD` | `Tennis123` |

Or via CLI:
```bash
gh secret set TW_EMAIL --body "brandonvincent567@gmail.com"
gh secret set TW_PASSWORD --body "Tennis123"
```

### 3. Done — it runs automatically every day

The workflow fires at **~5:45 PM Melbourne time** daily. It:
1. Boots up a cloud machine
2. Installs everything (~1 min, cached after first run)
3. Logs in and waits for each slot time (6:00, 6:30, ..., 9:00 PM)
4. Snipes the instant each slot opens
5. Uploads logs as artifacts you can download from the Actions tab

### Test it now (manual trigger)

Go to your repo → **Actions** tab → **Tennis Court Sniper** → **Run workflow** → click the green button.

### Check results

Go to **Actions** tab → click the latest run → see live logs, or download the `logs` artifact.

---

## Quick setup (local)

```bash
cd tennis-booker

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install the Chromium browser for Playwright
playwright install chromium
```

Your credentials are already in `.env`. Double-check they're correct:

```
TW_EMAIL=brandonvincent567@gmail.com
TW_PASSWORD=Tennis123
```

---

## Option 1: Competitive scheduler (recommended)

`scheduler.py` is built for speed. It:

1. Wakes up at **5:57 PM** daily
2. **Logs in and pre-navigates** to the booking page before 6:00 PM
3. At **exactly 6:00:00 PM** → snipes the 6:00 PM slot (just opened!)
4. At **exactly 6:30:00 PM** → snipes the 6:30 PM slot
5. At **exactly 7:00:00 PM** → snipes the 7:00 PM slot
6. ... continues through **9:00 PM**
7. At each trigger, also tries earlier times that may still be free
8. Once booked → stops for the day, sleeps until tomorrow

### Run it manually (foreground)

```bash
cd /Users/brandonvincent/Documents/personal/tennis-booker
source .venv/bin/activate
python scheduler.py
```

### Run in background (survives terminal close)

```bash
cd /Users/brandonvincent/Documents/personal/tennis-booker
source .venv/bin/activate
nohup python scheduler.py > scheduler_output.log 2>&1 &
echo $!    # prints the PID — save this to stop it later
```

To stop it:
```bash
kill <PID>
```

### Test it right now (skip the wait)

```bash
python scheduler.py --test-now
python scheduler.py --test-now --visible   # watch the browser
```

### Auto-start daily with macOS launchd

This makes the scheduler start automatically at 5:50 PM every day,
even after reboots:

```bash
# Install the launch agent
cp com.tennisbooker.scheduler.plist ~/Library/LaunchAgents/

# Load it
launchctl load ~/Library/LaunchAgents/com.tennisbooker.scheduler.plist

# Verify it's registered
launchctl list | grep tennisbooker
```

To uninstall:
```bash
launchctl unload ~/Library/LaunchAgents/com.tennisbooker.scheduler.plist
rm ~/Library/LaunchAgents/com.tennisbooker.scheduler.plist
```

---

## Option 2: Simple one-shot mode

`book.py` is simpler — it logs in fresh, tries all preferred times, and exits.

```bash
# Single attempt
python book.py

# Loop every 30 min until booked
python book.py --loop

# Loop every 15 min
python book.py --loop --interval 15

# Show the browser
python book.py --visible

# Target a specific date
python book.py --date 2026-02-13
```

---

## Option 3: Cron jobs (one per slot time)

If you prefer cron over the scheduler daemon:

```bash
crontab -e
```

```
# Snipe each slot at the exact time it opens
0  18 * * * cd /Users/brandonvincent/Documents/personal/tennis-booker && .venv/bin/python book.py >> cron.log 2>&1
30 18 * * * cd /Users/brandonvincent/Documents/personal/tennis-booker && .venv/bin/python book.py >> cron.log 2>&1
0  19 * * * cd /Users/brandonvincent/Documents/personal/tennis-booker && .venv/bin/python book.py >> cron.log 2>&1
30 19 * * * cd /Users/brandonvincent/Documents/personal/tennis-booker && .venv/bin/python book.py >> cron.log 2>&1
0  20 * * * cd /Users/brandonvincent/Documents/personal/tennis-booker && .venv/bin/python book.py >> cron.log 2>&1
30 20 * * * cd /Users/brandonvincent/Documents/personal/tennis-booker && .venv/bin/python book.py >> cron.log 2>&1
0  21 * * * cd /Users/brandonvincent/Documents/personal/tennis-booker && .venv/bin/python book.py >> cron.log 2>&1
```

Note: cron has ~1-2 second startup delay, so `scheduler.py` is faster for competitive booking.

---

## Adding Albert Park

Right now only **Melbourne Park** (`clubId=3`) is configured. To add Albert Park:

1. Open [tennisworld.perfectgym.com.au/ClientPortal2](https://tennisworld.perfectgym.com.au/ClientPortal2) in your browser
2. Navigate to Albert Park's booking page
3. Look at the URL — it will contain `clubId=X&zoneTypeId=Y`
4. Edit `book.py` and uncomment/fill in the Albert Park entry in `CLUBS`:

```python
CLUBS = [
    {"name": "Melbourne Park", "clubId": 3, "zoneTypeId": 32},
    {"name": "Albert Park", "clubId": X, "zoneTypeId": Y},  # <-- fill these in
]
```

## Logs & debugging

| File | Purpose |
|---|---|
| `scheduler.log` | Scheduler daemon log |
| `booker.log` | book.py run log |
| `scheduler_state.json` | Tracks daily booking state |
| `booking_success.png` | Screenshot on successful booking |
| `debug_*.png` | Screenshots at each step (for troubleshooting) |

- Run with `--visible` to watch the browser
- Run with `--test-now` to skip waiting and test immediately
- If a CAPTCHA appears on login, run with `--visible` and solve it manually

## Configuration reference

All config is at the top of each file:

**`book.py`**:

| Variable | Default | Description |
|---|---|---|
| `CLUBS` | Melbourne Park | Venues to try (in order) |
| `PREFERRED_TIMES` | 8:00 PM first | Time slots in priority order |
| `ADVANCE_DAYS` | 5 | How many days ahead to book |

**`scheduler.py`**:

| Variable | Default | Description |
|---|---|---|
| `SNIPE_SCHEDULE` | 6:00–9:00 PM every 30 min | When to fire |
| `SNIPE_RETRIES` | 8 | Rapid retries per slot opening |
| `RETRY_GAP_S` | 3 | Seconds between retries |
| `PRE_LOGIN_MINUTES` | 3 | Login this many min before first slot |
