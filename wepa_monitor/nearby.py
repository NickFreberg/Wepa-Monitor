"""Walking distances between campus buildings, and the nearest printer a student can use instead.

Distances follow the campus path network (OpenStreetMap footways, the same network Rounds uses),
not straight lines. Residence-hall printers are behind card access, so a student whose printer is
down is pointed to the nearest *open* printer: one in an academic building, the library or the
student union, that is printing right now.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from . import reference, routing

METERS_PER_MILE = 1609.344
FEET_PER_METER = 3.28084


def fmt_distance(meters: float) -> str:
    """US units: '350 ft' under a tenth of a mile, else '0.4 mi'."""
    if meters is None or not np.isfinite(meters):
        return "—"
    miles = meters / METERS_PER_MILE
    if miles < 0.1:
        return f"{max(10, round(meters * FEET_PER_METER, -1)):,.0f} ft"
    return f"{miles:.1f} mi" if miles < 10 else f"{miles:,.0f} mi"


def fmt_walk(seconds: float) -> str:
    if seconds is None or not np.isfinite(seconds):
        return "—"
    m = max(1, round(seconds / 60))
    return f"{m} min walk"


@lru_cache(maxsize=2)
def _matrix(stamp: float) -> tuple[list[str], np.ndarray]:
    """All-pairs walking time (seconds) between main-campus buildings: one Dijkstra per building."""
    b = reference.load_buildings()
    b = b[(b["campus"] == "Main") & b["lat"].notna()].reset_index(drop=True)
    g = routing.graph("walk")
    nodes = [g.nearest(float(r.lat), float(r.lon)) for r in b.itertuples()]
    out = np.full((len(b), len(b)), np.inf)
    for i, n in enumerate(nodes):
        dist, _ = routing.dijkstra(g, n)
        out[i] = dist[nodes]
    return b["building"].tolist(), out


def walk_seconds(a: str, b: str) -> float:
    """Walking time between two main-campus buildings (inf when either isn't mapped)."""
    if a == b:
        return 0.0
    if routing.network() is None:
        return np.inf
    names, m = _matrix(routing.NETWORK_PATH.stat().st_mtime)
    if a not in names or b not in names:
        return np.inf
    return float(m[names.index(a), names.index(b)])


def walk_meters(a: str, b: str) -> float:
    return walk_seconds(a, b) * routing.WALK_SPEED


def backups(cur: pd.DataFrame, station_id: str, limit: int = 3) -> pd.DataFrame:
    """Where to send students when `station_id` is down: other working printers in the same
    building, then the nearest working printers anyone can walk into (not residence halls).

    cur: ops.current_status rows. Returns station_id, label, building, state, kind
    ('same building' | 'open to everyone'), meters, seconds.
    """
    me = cur[cur["station_id"] == station_id]
    if me.empty:
        return pd.DataFrame()
    home = me.iloc[0]["building"]
    up = cur[(cur["station_id"] != station_id) & ~cur["state"].isin(["red", "stale"])]
    same = up[up["building"] == home].assign(kind="same building", meters=0.0, seconds=0.0)
    public = up[(up["building"] != home) & (up["station_type"] != "residence")
                & (up.get("campus", pd.Series("Main", index=up.index)).fillna("Main") == "Main")]
    if len(public):
        secs = public["building"].map(lambda x: walk_seconds(home, x))
        public = public.assign(kind="open to everyone", seconds=secs, meters=secs * routing.WALK_SPEED)
        public = public[np.isfinite(public["seconds"])].sort_values("seconds")
        # One printer per building is enough to point someone there.
        public = public.drop_duplicates("building").head(limit)
    out = pd.concat([same, public], ignore_index=True)
    cols = ["station_id", "label", "description", "building", "state", "kind", "meters", "seconds"]
    return out[[c for c in cols if c in out]]
