"""Cumulative consumable usage that survives replacements.

The status page shows a *level* (100 -> 0) that jumps back up when a part is
swapped. Usage is reconstructed like an odometer:

1. Keep only the readings where a series changes value (levels move slowly,
   so this shrinks millions of minute-rows to thousands of change points).
2. A rise of >= REPLACEMENT_JUMP_PTS between consecutive readings is a
   replacement. It starts a new "life" and is never counted as negative usage.
3. Within one life, usage so far = first reading - lowest reading so far.
   Using the running minimum means sensor jitter (51 -> 50 -> 51 -> 50) is
   counted once, not every time it wobbles.
4. Usage increments are summed across lives into a cumulative total, measured
   in percentage points; 100 points = one part's worth.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def change_points(snap: pd.DataFrame) -> pd.DataFrame:
    frames = []
    station_codes, _ = pd.factorize(snap["station_id"])
    for comp in config.COMPONENTS:
        valid = snap[comp].notna().to_numpy()
        s = snap.loc[valid, ["station_id", "scrape_ts", comp]]
        if s.empty:
            continue
        v = s[comp].to_numpy(dtype=float)
        st = station_codes[valid]
        keep = np.ones(len(s), dtype=bool)
        keep[1:] = (v[1:] != v[:-1]) | (st[1:] != st[:-1])
        # Keep each series' last reading too, so "current level" is current.
        keep[:-1] |= st[:-1] != st[1:]
        keep[-1] = True
        part = s.loc[keep].rename(columns={comp: "level"})
        part["component"] = comp
        frames.append(part)
    if not frames:
        return pd.DataFrame(columns=["station_id", "scrape_ts", "level", "component"])
    out = pd.concat(frames, ignore_index=True)
    out["station_id"] = out["station_id"].astype(str)
    out["level"] = out["level"].astype(float)
    return out.sort_values(["station_id", "component", "scrape_ts"], ignore_index=True)


def usage(points: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (usage increments per change point, replacement events)."""
    if points.empty:
        return points.assign(life=0, used=0.0, replaced=False), pd.DataFrame(
            columns=["station_id", "component", "ts", "level_before", "level_after"])
    keys = [points["station_id"], points["component"]]
    prev = points.groupby(keys, sort=False)["level"].shift(1)
    replaced = (points["level"] - prev) >= config.REPLACEMENT_JUMP_PTS
    df = points.assign(prev_level=prev, replaced=replaced)
    df["life"] = df.groupby(keys, sort=False)["replaced"].cumsum()
    life_keys = [df["station_id"], df["component"], df["life"]]
    first = df.groupby(life_keys, sort=False)["level"].transform("first")
    run_min = df.groupby(life_keys, sort=False)["level"].cummin()
    used_to_date = first - run_min
    df["used"] = used_to_date.groupby(life_keys, sort=False).diff().fillna(0.0).clip(lower=0)

    repl = df.loc[df["replaced"], ["station_id", "component", "scrape_ts", "prev_level", "level"]]
    repl = repl.rename(columns={"scrape_ts": "ts", "prev_level": "level_before", "level": "level_after"})
    return df, repl.reset_index(drop=True)


def current_levels(points: pd.DataFrame) -> pd.DataFrame:
    last = points.groupby(["station_id", "component"], sort=False).tail(1)
    return last[["station_id", "component", "level", "scrape_ts"]].reset_index(drop=True)
