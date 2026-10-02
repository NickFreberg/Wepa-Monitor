"""Central configuration: paths, source URL, and the business rules.

Every threshold that shapes a metric lives here so it can be changed in one
place and the whole history recomputed from the raw snapshots.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = ROOT / "reference"

# Live data and demo data never mix: they live in separate directories.
LIVE_DATA_DIR = Path(os.environ.get("WEPA_DATA_DIR", ROOT / "data" / "live"))
DEMO_DATA_DIR = Path(os.environ.get("WEPA_DEMO_DIR", ROOT / "data" / "demo"))

STATUS_URL = os.environ.get(
    "WEPA_STATUS_URL", "https://cs.wepanow.com/000BRIDGEW149.html&filter="
)
HTTP_TIMEOUT_S = 30
USER_AGENT = "BSU-ResNet-Wepa-Monitor/2.0 (+https://github.com/nickfreberg/wepa-monitor)"

# Bridgewater, MA. All "time of day" and "per day" metrics use this zone.
LOCAL_TZ = "America/New_York"

# --- Snapshot cadence -------------------------------------------------------
# The status page refreshes every 60 s, so one snapshot per minute is expected.
EXPECTED_INTERVAL_S = 60
# A gap longer than this between two snapshots of a station is "unobserved"
# time: excluded from availability, never assumed to be up or down.
MAX_OBSERVED_GAP_S = 5 * 60
# An incident that is in the same state on both sides of a gap shorter than
# this is treated as one continuous incident (e.g. a scraper restart).
MAX_INCIDENT_BRIDGE_S = 6 * 60 * 60

# --- Consumables ------------------------------------------------------------
COMPONENTS = [
    "toner_k", "toner_c", "toner_m", "toner_y",
    "drum_k", "drum_c", "drum_m", "drum_y",
    "belt", "fuser",
]
COMPONENT_LABELS = {
    "toner_k": "Toner K", "toner_c": "Toner C", "toner_m": "Toner M", "toner_y": "Toner Y",
    "drum_k": "Drum K", "drum_c": "Drum C", "drum_m": "Drum M", "drum_y": "Drum Y",
    "belt": "Belt", "fuser": "Fuser",
}
# A rise of at least this many points between two consecutive readings is a
# replacement, not negative usage. Sensor jitter is +/-1-2 points.
REPLACEMENT_JUMP_PTS = 15
# Wepa's own definition: "TONER LOW = Toner level is 10% or less."
TONER_LOW_PCT = 10
# Old ResNet practice from the v1 script: <=5% needs replacement, <3% critical.
CONSUMABLE_REPLACE_PCT = 5
CONSUMABLE_CRITICAL_PCT = 3
# Belt policy from the v1 script: change at 2% Mon-Thu, 5% on Fridays.
BELT_CHANGE_PCT_WEEKDAY = 2
BELT_CHANGE_PCT_FRIDAY = 5

# --- Data-quality gating ------------------------------------------------------
# A metric is only shown when it rests on enough evidence; otherwise the
# dashboard says "insufficient data" instead of showing noise.
MIN_INCIDENTS_FOR_MEAN = 3
MIN_COVERAGE_FOR_RATE = 0.5      # share of the window actually observed
MIN_DAYS_FOR_BURN_RATE = 3
BURN_RATE_LOOKBACK_DAYS = 14

# --- Support ownership and desk hours ------------------------------------------
# Who looks after each station, where the team is based, and when its desk is
# staffed (local time; weekday 0 = Monday; hours may be fractional, e.g. 9.5).
# Outages that start outside these hours wait for the desk to open, so metrics
# split downtime into desk hours and after hours for each owner.
SUPPORT_TEAMS = {
    "ResNet": {"base": "East Campus Commons", "short": "ECC",
               "hours": {0: (10, 18), 1: (10, 18), 2: (10, 18), 3: (10, 18), 4: (10, 16)}},
    "IT Service Center": {"base": "Maxwell Library", "short": "Maxwell",
                          "hours": {d: (9, 16) for d in range(5)}},
}
# Status-page section -> owning team. Anything not listed goes to the default.
SECTION_OWNER = {"ResNet": "ResNet", "Student Computer Labs": "IT Service Center",
                 "Satellite Campuses": "IT Service Center"}
DEFAULT_OWNER = "IT Service Center"
# Context (not modeled): ResNet has also run RSR (ResNet Support Representative)
# workstations at Shea-Durgin, Crimson, Scott and the ResNet office in ECC, but they
# weren't always staffed, so the desk hours above are the dependable coverage.
# Days both desks are closed (holidays, breaks), as "YYYY-MM-DD" strings.
SUPPORT_CLOSED_DATES: set[str] = set()

# --- Campus intelligence (bridgew.edu) ----------------------------------------------
# The academic calendar is published years ahead, so a yearly refresh is enough; library
# hours are published a few weeks ahead, so they are refreshed weekly and accumulated.
# Where refreshed campus files are written. Defaults to reference/ (committed copies); on Azure it
# is set to persistent storage so refreshes survive redeploys.
CAMPUS_DIR = Path(os.environ.get("WEPA_CAMPUS_DIR", REFERENCE_DIR))
CAMPUS_CALENDAR_REFRESH_DAYS = 365
CAMPUS_LIBRARY_REFRESH_DAYS = 7
# Treat university holidays (from the academic calendar, plus Massachusetts state holidays)
# as days both support desks are closed.
DESKS_CLOSED_ON_HOLIDAYS = True
