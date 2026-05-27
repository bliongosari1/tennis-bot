"""
Tennis booker — central settings.

Edit the constants below to change the bot's behaviour.  No env-var
overrides yet; just edit, commit, push.
"""

# ─── Venues to attempt, in priority order ────────────────────────────────────
# To skip a venue entirely, set "enabled": False.
# These names must match the "name" field in book.CLUBS.
VENUES = [
    {"name": "Melbourne Park",  "enabled": True},
    {"name": "Albert Reserve",  "enabled": True},
]


# ─── How many bookings to make per day, per venue ────────────────────────────
# Tennis World caps you at 1 hour per venue per day for standard bookings,
# so 1 is the right answer.  The sniper will book up to MAX_BOOKINGS_PER_VENUE
# at each enabled venue before stopping for the day.
#
#   2 venues × 1 booking = up to 2 bookings/day  (default)
MAX_BOOKINGS_PER_VENUE_PER_DAY = 1


# ─── Time slot preferences ───────────────────────────────────────────────────
# Weekday targets (Wed–Sun book Mon–Fri slots).  Order = priority.
EVENING_TIMES = [
    "08:00 PM",
    "08:30 PM",
    "07:30 PM",
    "07:00 PM",
    "09:00 PM",
]

# Weekend targets (Mon/Tue book Sat/Sun slots).  Order = priority.
MORNING_TIMES = [
    "11:00 AM",
    "10:00 AM",
    "12:00 PM",
]


# ─── Booking window ──────────────────────────────────────────────────────────
# Slots become bookable exactly this many days before the session time.
ADVANCE_DAYS = 5


# ─── Opportunistic scanner ───────────────────────────────────────────────────
# scan.py looks for free slots in the next SCAN_LOOKAHEAD_DAYS that match
# our preferred times — pickups from other people's cancellations.
SCAN_LOOKAHEAD_DAYS = 5

# If True the scanner will silently auto-book any free slot it finds (subject
# to the per-venue/day cap above).  If False it only sends a Telegram alert
# and you book it manually with /snipe_<date>.
#
# Auto-book is OFF by default because most "found" slots inside the 5-day
# window will hit the "Too soon" member rule and you don't want noise.
SCAN_AUTO_BOOK = False


# ─── Sniper retry / timing tuning ────────────────────────────────────────────
SNIPE_RETRIES = 8     # rapid-fire attempts per slot opening
RETRY_GAP_S = 3       # seconds between snipe retries
PRE_LOGIN_MINUTES = 3 # log in this many min before first slot of session
PRE_REFRESH_S = 10    # refresh booking page this many sec before slot opens


# ─── Helper used by book.py / scheduler.py ───────────────────────────────────

def enabled_venue_names() -> set[str]:
    return {v["name"] for v in VENUES if v.get("enabled", True)}
