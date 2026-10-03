"""Operational views: current station state and a prioritized work queue.

The queue's priority score is deliberately simple and explainable, and is the
same score the route planner will use:

    score = base(issue) x (1.5 if no other working printer in the building)
            + hours open (capped at 24)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, rules
from .metrics import Dataset, forecast

BASE = {"red": 100, "yellow": 40, "tray": 20, "consumable_now": 30, "consumable_soon": 12, "stale": 25}


def current_status(ds: Dataset, ids=None) -> pd.DataFrame:
    """Latest snapshot per station, plus 'stale' when the station has gone quiet."""
    cur = ds.latest if ids is None else ds.latest[ds.latest["station_id"].isin(ids)]
    cur = cur.merge(ds.stations[["station_id", "label", "building", "area", "station_type", "owner", "campus"]],
                    on="station_id", how="left")
    age = (ds.as_of - cur["scrape_ts"]).dt.total_seconds()
    cur["stale"] = age > config.MAX_OBSERVED_GAP_S
    cur["state"] = np.where(cur["stale"], "stale", cur["row_status"])
    up = cur[~cur["stale"] & (cur["row_status"] != "red")].groupby("building")["station_id"].nunique()
    cur["working_in_building"] = cur["building"].map(up).fillna(0).astype(int)
    return cur


def work_queue(ds: Dataset, ids=None) -> pd.DataFrame:
    cur = current_status(ds, ids).set_index("station_id")
    rows = []

    def add(sid, kind, issue, fix, opened=None, censored=False):
        st = cur.loc[sid]
        others_up = st["working_in_building"] - (0 if st["state"] == "red" else 1)
        backup = others_up > 0
        open_h = (ds.as_of - opened).total_seconds() / 3600 if opened is not None else 0.0
        score = BASE[kind] * (1.0 if backup else 1.5) + min(open_h, 24)
        rows.append({"station_id": sid, "station": st["label"], "building": st["building"],
                     "area": st["area"], "kind": kind, "issue": issue, "fix": fix,
                     "open_min": open_h * 60, "open_censored": bool(censored), "backup": backup,
                     "score": score})

    open_faults = ds.fault_inc[ds.fault_inc["status"] == "open"]
    for _, f in open_faults[open_faults["station_id"].isin(cur.index)].iterrows():
        sev = rules.issue_severity(f["code"])
        sid = f["station_id"]
        if sev == "red" and cur.loc[sid, "row_status"] != "red":
            sev = cur.loc[sid, "row_status"] if cur.loc[sid, "row_status"] != "green" else "yellow"
        label = rules.issue_status(f["code"]) + (f" ({f['detail']})" if f.get("detail") else "")
        add(sid, sev, label, rules.FIX_CATEGORIES[f["fix_category"]], f["start"], f["censored_start"])

    flagged = {r["station_id"] for r in rows}
    open_sev = ds.sev_inc[(ds.sev_inc["status"] == "open") & ds.sev_inc["station_id"].isin(cur.index)]
    for _, s in open_sev.iterrows():
        if s["station_id"] not in flagged:
            add(s["station_id"], s["severity"], f"{s['severity'].capitalize()} status (no code)",
                rules.FIX_CATEGORIES["other"], s["start"], s["censored_start"])

    for sid, _row in cur[cur["stale"]].iterrows():
        add(sid, "stale", "No data from station", "Check that the station is online")

    trays = ds.tray_inc[(ds.tray_inc["status"] == "open") & ds.tray_inc["station_id"].isin(cur.index)]
    for _, t in trays.iterrows():
        if "paper_out_error" in cur.loc[t["station_id"], "status_codes"]:
            continue   # already queued as a red paper-out
        add(t["station_id"], "tray", f"{t['tray']} empty", rules.FIX_CATEGORIES["paper"], t["start"],
            t["censored_start"])

    fc = forecast(ds, list(cur.index))
    for _, c in fc.iterrows():
        floor = config.CONSUMABLE_REPLACE_PCT if c["component"].startswith("toner") else 2
        if c["level"] <= floor:
            add(c["station_id"], "consumable_now", f"{c['label']} at {c['level']:.0f}%",
                rules.FIX_CATEGORIES["consumable"])
        elif pd.notna(c["days_to_replace"]) and c["days_to_replace"] <= 2:
            add(c["station_id"], "consumable_soon",
                f"{c['label']} at {c['level']:.0f}% (~{c['days_to_replace']:.1f} days left)",
                "Bring a replacement on the next round")

    if not rows:
        return pd.DataFrame(columns=["station_id", "station", "building", "area", "kind", "issue",
                                     "fix", "open_min", "open_censored", "backup", "score"])
    return pd.DataFrame(rows).sort_values("score", ascending=False, ignore_index=True)
