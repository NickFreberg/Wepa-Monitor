"""Day-by-day rollups, so the dashboard's memory stays flat as history grows.

Raw snapshots are one row per station per minute (~45k rows a day). Each
finished day is reduced once to four compact tables and cached on disk:

    hourly   station x hour: observed seconds and seconds not in red
    bhourly  building x hour: observed minutes, minutes with any / all printers up
    status   only the snapshots where a station's state changes (start and end of
             every run), with a `brk` flag where a long gap breaks continuity
    cons     consumable change points (see consumables.py)

Incidents are then built from `status` exactly as from the raw data: every
run start and run end is kept, and gaps are carried by `brk` instead of being
inferred from timestamps. Only the newest day is recomputed on each refresh.

Each day is derived with one snapshot of context on either side (the previous
day's last and the next day's first per station), so spans and runs that cross
midnight come out identical to processing the whole history at once.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, consumables, events, store

ROLLUP_VERSION = 1
KINDS = ("hourly", "bhourly", "status", "cons")
STATE_COLS = ("row_status", "status_codes", "printer_text")
_BRIDGE = pd.Timedelta(seconds=config.MAX_INCIDENT_BRIDGE_S)


def _version_key() -> str:
    """Cached rollups are only valid for the rules and station map they were built with."""
    stations = (config.REFERENCE_DIR / "stations.csv").read_bytes()
    rules = f"{ROLLUP_VERSION}|{config.EXPECTED_INTERVAL_S}|{config.MAX_OBSERVED_GAP_S}|{config.MAX_INCIDENT_BRIDGE_S}"
    return hashlib.sha1(rules.encode() + stations).hexdigest()[:12]


def _empty(kind: str) -> pd.DataFrame:
    cols = {
        "hourly": ["station_id", "hour", "covered_s", "up_s"],
        "bhourly": ["building", "hour", "minutes", "any_up_min", "all_up_min"],
        "status": [*store.SNAPSHOT_COLUMNS, "brk"],
        "cons": ["station_id", "scrape_ts", "level", "component"],
    }[kind]
    return pd.DataFrame(columns=cols)


def derive(day_rows: pd.DataFrame, before: pd.DataFrame, after: pd.DataFrame,
           building_of: dict[str, str]) -> dict[str, pd.DataFrame]:
    """Reduce one day's snapshots. `before`/`after` hold at most one context row per station."""
    if day_rows.empty:
        return {k: _empty(k) for k in KINDS}
    day_rows = day_rows.assign(_ctx=0)
    parts = [f for f in (before.assign(_ctx=-1), day_rows, after.assign(_ctx=1)) if not f.empty]
    frame = pd.concat(parts, ignore_index=True)
    frame["scrape_ts"] = pd.to_datetime(frame["scrape_ts"], utc=True)
    frame = frame.sort_values(["station_id", "scrape_ts"], kind="stable", ignore_index=True)
    in_day = (frame["_ctx"] == 0).to_numpy()

    # hourly: observed and up seconds per station-hour (next snapshot may be tomorrow's).
    spans = events.observation_spans(frame)
    spans = spans[in_day]
    up = np.where(spans["row_status"] != "red", spans["covered_s"], 0.0)
    hourly = (spans.assign(up_s=up, hour=spans["start"].dt.floor("h"))
              .groupby(["station_id", "hour"], observed=True)[["covered_s", "up_s"]].sum().reset_index())

    # bhourly: per minute, could at least one / every printer in the building print?
    d = day_rows
    minute = d["scrape_ts"].dt.floor("min")
    per_min = (pd.DataFrame({"building": d["station_id"].astype(str).map(building_of).fillna(d["description"]),
                             "minute": minute, "up": (d["row_status"] != "red").to_numpy()})
               .groupby(["building", "minute"], observed=True)["up"].agg(["any", "all"]).reset_index())
    per_min["hour"] = per_min["minute"].dt.floor("h")
    bhourly = (per_min.groupby(["building", "hour"], observed=True)
               .agg(minutes=("any", "size"), any_up_min=("any", "sum"), all_up_min=("all", "sum")).reset_index())

    # status: keep the first and last snapshot of every run of identical state.
    st = frame["station_id"].to_numpy()
    ts = frame["scrape_ts"].to_numpy(dtype="datetime64[us]")
    codes = np.zeros(len(frame), dtype=np.int64)
    for col in STATE_COLS:
        c, _ = pd.factorize(frame[col].astype(str))
        codes = codes * (int(c.max()) + 2 if len(c) else 1) + c
    n = len(frame)
    same_prev = np.zeros(n, dtype=bool)
    same_prev[1:] = st[1:] == st[:-1]
    brk = np.zeros(n, dtype=bool)
    brk[1:] = same_prev[1:] & ((ts[1:] - ts[:-1]) > _BRIDGE.to_timedelta64())
    starts = ~same_prev | brk
    starts[1:] |= codes[1:] != codes[:-1]
    ends = np.zeros(n, dtype=bool)
    ends[:-1] = starts[1:]
    ends[-1] = True
    keep = (starts | ends) & in_day
    status = frame.loc[keep, list(store.SNAPSHOT_COLUMNS)].assign(brk=brk[keep]).reset_index(drop=True)

    # cons: change points within the day, judged against yesterday's last reading.
    points = consumables.change_points(frame[frame["_ctx"] <= 0].drop(columns="_ctx"))
    first_ts = d["scrape_ts"].min()
    cons = points[points["scrape_ts"] >= first_ts].reset_index(drop=True)

    return {"hourly": hourly, "bhourly": bhourly, "status": status, "cons": cons}


class RollupStore:
    """Keeps finished days' rollups in memory and on disk; recomputes only the newest day."""

    def __init__(self, data_dir: Path, building_of: dict[str, str]):
        self.data_dir = data_dir
        self.building_of = building_of
        self.version = _version_key()
        self.cache_dir = data_dir / "derived" / self.version
        self.finished: dict[str, dict[str, pd.DataFrame]] = {}
        self._combined: dict[str, pd.DataFrame] | None = None
        self._combined_days: tuple = ()
        self._raw: dict[str, pd.DataFrame] = {}

    # -- raw access with a tiny cache (neighbouring days are needed as context) --
    def _raw_day(self, day: str) -> pd.DataFrame:
        if day not in self._raw:
            if len(self._raw) >= 3:
                self._raw.pop(next(iter(self._raw)))
            self._raw[day] = store.load_day(self.data_dir, day)
        return self._raw[day]

    def _context(self, day: str | None, last: bool) -> pd.DataFrame:
        if day is None:
            return pd.DataFrame(columns=store.SNAPSHOT_COLUMNS)
        raw = self._raw_day(day)
        g = raw.groupby("station_id", sort=False, observed=True)
        return (g.tail(1) if last else g.head(1)).reset_index(drop=True)

    def _derive_day(self, days: list[str], i: int) -> dict[str, pd.DataFrame]:
        prev_day = days[i - 1] if i > 0 else None
        next_day = days[i + 1] if i + 1 < len(days) else None
        return derive(self._raw_day(days[i]), self._context(prev_day, True),
                      self._context(next_day, False), self.building_of)

    def _load_or_build(self, days: list[str], i: int) -> dict[str, pd.DataFrame]:
        day = days[i]
        paths = {k: self.cache_dir / k / f"{day}.parquet" for k in KINDS}
        if all(p.exists() for p in paths.values()):
            return {k: pd.read_parquet(p) for k, p in paths.items()}
        out = self._derive_day(days, i)
        for k, p in paths.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            out[k].to_parquet(p, index=False)
        return out

    def refresh(self) -> dict[str, pd.DataFrame]:
        days = store.snapshot_days(self.data_dir)
        self._prune_old_versions()
        if not days:
            return {k: _empty(k) for k in KINDS}
        # Every day but the newest is finished: build (or load) it once and keep it.
        for i, day in enumerate(days[:-1]):
            if day not in self.finished:
                self.finished[day] = self._load_or_build(days, i)
        for day in list(self.finished):
            if day not in days:
                del self.finished[day]
        finished_days = tuple(d for d in days[:-1])
        if self._combined is None or self._combined_days != finished_days:
            self._combined = {k: pd.concat([self.finished[d][k] for d in finished_days], ignore_index=True)
                              if finished_days else _empty(k) for k in KINDS}
            self._combined_days = finished_days
        self._raw.pop(days[-1], None)                 # the newest day is still growing: always re-read
        today = self._derive_day(days, len(days) - 1)
        out = {}
        for k in KINDS:
            parts = [f for f in (self._combined[k], today[k]) if not f.empty]
            out[k] = pd.concat(parts, ignore_index=True) if parts else _empty(k)
        # Callers that can work incrementally (consumable usage) use these instead of the concatenation.
        self.history, self.today, self.history_key = self._combined, today, finished_days
        return out

    def _prune_old_versions(self) -> None:
        root = self.data_dir / "derived"
        if root.exists():
            for d in root.iterdir():
                if d.is_dir() and d.name != self.version:
                    shutil.rmtree(d, ignore_errors=True)
