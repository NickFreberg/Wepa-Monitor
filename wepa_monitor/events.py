"""Turn minute-level snapshots into observed time spans and incidents.

Definitions (see README "Metric definitions"):

* Observation span - each snapshot "covers" the time until the next snapshot
  of the same station, if that comes within MAX_OBSERVED_GAP_S; otherwise it
  covers one expected interval and the rest is unobserved. Unobserved time is
  excluded from availability: never assumed up, never assumed down.

* Run - consecutive snapshots of one station in the same state. An incident
  opens at the first snapshot showing the state and resolves at the first
  snapshot that no longer shows it. A run in the same state on both sides of a
  gap up to MAX_INCIDENT_BRIDGE_S is one incident; longer gaps split it and
  the first part's resolution time is unknown (excluded from MTTR).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, rules

_INTERVAL = pd.Timedelta(seconds=config.EXPECTED_INTERVAL_S)
_MAX_GAP = pd.Timedelta(seconds=config.MAX_OBSERVED_GAP_S)
_BRIDGE = pd.Timedelta(seconds=config.MAX_INCIDENT_BRIDGE_S)


def observation_spans(snap: pd.DataFrame) -> pd.DataFrame:
    """One row per snapshot with the seconds of station time it covers."""
    ts = snap["scrape_ts"]
    same = snap["station_id"].eq(snap["station_id"].shift(-1))
    nxt = ts.shift(-1).where(same)
    gap = nxt - ts
    end = nxt.where(gap <= _MAX_GAP, ts + _INTERVAL)
    return pd.DataFrame({
        "station_id": snap["station_id"],
        "start": ts,
        "covered_s": (end - ts).dt.total_seconds(),
        "row_status": snap["row_status"],
    }).reset_index(drop=True)


def runs(station: pd.Series, ts: pd.Series, state, as_of: pd.Timestamp) -> pd.DataFrame:
    """Run-length encode `state` per station. Inputs must be sorted by station, ts.

    Pure numpy on integer codes: millions of snapshots encode in well under a second.
    """
    st_codes, st_uniques = pd.factorize(station)
    sv_codes, sv_uniques = pd.factorize(pd.Series(np.asarray(state)))
    tsv = ts.to_numpy(dtype="datetime64[us]")
    n = len(st_codes)
    if n == 0:
        return pd.DataFrame(columns=["station_id", "state", "start", "last_seen", "n_obs",
                                     "censored_start", "end", "status", "duration_s"])
    new = np.ones(n, dtype=bool)
    new[1:] = ((st_codes[1:] != st_codes[:-1]) | (sv_codes[1:] != sv_codes[:-1])
               | ((tsv[1:] - tsv[:-1]) > _BRIDGE.to_timedelta64()))
    starts = np.flatnonzero(new)
    lasts = np.r_[starts[1:] - 1, n - 1]
    g = pd.DataFrame({
        "station_id": np.asarray(st_uniques)[st_codes[starts]],
        "state": np.asarray(sv_uniques)[sv_codes[starts]],
        "start": pd.to_datetime(tsv[starts], utc=True),
        "last_seen": pd.to_datetime(tsv[lasts], utc=True),
        "n_obs": lasts - starts + 1,
    })

    # A run that begins at a station's first snapshot was already in progress when monitoring
    # started: its true start is unknown, so its duration is a lower bound (excluded from MTTR).
    g["censored_start"] = ~g["station_id"].eq(g["station_id"].shift(1))
    same_station_next = g["station_id"].eq(g["station_id"].shift(-1))
    next_start = g["start"].shift(-1).where(same_station_next)
    resolved = next_start.notna() & ((next_start - g["last_seen"]) <= _BRIDGE)
    g["end"] = next_start.where(resolved)
    is_last = ~same_station_next
    g["status"] = np.where(resolved, "resolved",
                           np.where(is_last & (g["last_seen"] >= as_of - _MAX_GAP), "open", "unknown_end"))
    g["duration_s"] = (g["end"] - g["start"]).dt.total_seconds()
    open_mask = g["status"].eq("open")
    g.loc[open_mask, "duration_s"] = (as_of - g.loc[open_mask, "start"]).dt.total_seconds()
    return g


def severity_incidents(snap: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    r = runs(snap["station_id"], snap["scrape_ts"], snap["row_status"], as_of)
    r = r[r["state"].isin(["red", "yellow"])].rename(columns={"state": "severity"})
    return r.reset_index(drop=True)


def _flag_incidents(snap: pd.DataFrame, flags: dict[str, pd.Series], as_of: pd.Timestamp,
                    kind: str) -> pd.DataFrame:
    out = []
    for name, present in flags.items():
        stations = snap.loc[present, "station_id"].unique()
        if len(stations) == 0:
            continue
        sub = snap["station_id"].isin(stations)
        r = runs(snap.loc[sub, "station_id"], snap.loc[sub, "scrape_ts"], present[sub], as_of)
        r = r[r["state"].astype(bool)].drop(columns="state")
        r[kind] = name
        out.append(r)
    if not out:
        return pd.DataFrame(columns=["station_id", "start", "last_seen", "n_obs", "censored_start",
                                     "end", "status", "duration_s", kind])
    return pd.concat(out, ignore_index=True)


def _membership(column: pd.Series, extract) -> tuple[np.ndarray, list[str], dict[str, np.ndarray]]:
    """Factorize a text column once, then answer "does row i contain item x" by lookup."""
    codes, uniques = pd.factorize(column)
    items_per_unique = [set(extract(u)) for u in uniques]
    items = sorted(set().union(*items_per_unique)) if items_per_unique else []
    lookup = {it: np.array([it in s for s in items_per_unique], dtype=bool) for it in items}
    return codes, items, lookup


def fault_incidents(snap: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """One incident per (station, status code) episode."""
    codes, items, lookup = _membership(snap["status_codes"], lambda u: [c for c in str(u).split(",") if c])
    flags = {c: pd.Series(lookup[c][codes], index=snap.index) for c in items}
    inc = _flag_incidents(snap, flags, as_of, "code")
    inc["label"] = inc["code"].map(rules.code_label)
    inc["fix_category"] = inc["code"].map(rules.code_fix_category)
    return inc


def tray_incidents(snap: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """One incident per (station, tray) 'Paper Out Warning for TrayN' episode."""
    codes, items, lookup = _membership(snap["printer_text"],
                                       lambda u: rules.empty_trays(str(u).split(" | ")))
    flags = {t: pd.Series(lookup[t][codes], index=snap.index) for t in items}
    return _flag_incidents(snap, flags, as_of, "tray")
