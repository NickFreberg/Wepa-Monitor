"""Activity feed: one row per thing that happened, in plain language.

Built from the same incident tables as the metrics, so the log, the
notifications and the KPIs always agree. Notifications are the high-signal
subset (stations going down or recovering, monitoring gaps); the activity log
shows everything.
"""
from __future__ import annotations

import pandas as pd

from . import config
from .metrics import Dataset

COLUMNS = ["ts", "kind", "severity", "station_id", "station", "building", "title", "detail"]

KIND_LABEL = {
    "down": "Went down", "recovered": "Back up", "warning": "Warning", "warning_cleared": "Warning cleared",
    "replaced": "Part replaced", "tray_empty": "Tray empty", "tray_refilled": "Tray refilled",
    "data_gap": "Monitoring gap", "data_restored": "Monitoring restored",
}
KIND_GROUP = {
    "down": "Status", "recovered": "Status", "warning": "Status", "warning_cleared": "Status",
    "replaced": "Parts", "tray_empty": "Paper", "tray_refilled": "Paper",
    "data_gap": "Monitoring", "data_restored": "Monitoring",
}
NOTIFY_KINDS = {"down", "recovered", "data_gap", "data_restored"}
MIN_GAP_MINUTES = 5


def display_names(ds: Dataset) -> dict[str, str]:
    """Station description, plus its number when two stations share a description."""
    st = ds.stations
    dup = st["description"].duplicated(keep=False)
    names = st["description"].where(~dup, st["description"] + " #" + st["station_id"])
    return dict(zip(st["station_id"], names))


def _dur(seconds: float) -> str:
    m = seconds / 60
    if m < 90:
        return f"{m:.0f} min"
    if m < 48 * 60:
        return f"{m / 60:.1f} h"
    return f"{m / 1440:.1f} days"


def events(ds: Dataset, since: pd.Timestamp | None = None, ids=None) -> pd.DataFrame:
    since = since if since is not None else (ds.data_start or ds.as_of - pd.Timedelta(days=1))
    label = display_names(ds)
    building = ds.stations.set_index("station_id")["building"].to_dict()
    rows: list[dict] = []

    def add(ts, kind, severity, sid, title, detail=""):
        rows.append({"ts": ts, "kind": kind, "severity": severity, "station_id": sid or "",
                     "station": label.get(sid, sid or ""), "building": building.get(sid, ""),
                     "title": title, "detail": detail})

    # What each red incident was about: the fault codes that opened with it.
    faults = ds.fault_inc[["station_id", "start", "label", "detail"]]
    faults = faults.assign(what=faults["label"] + faults["detail"].fillna("").map(lambda d: f" ({d})" if d else ""))
    cause = faults.groupby(["station_id", "start"])["what"].agg(lambda s: "; ".join(sorted(set(s))))

    inc = ds.sev_inc
    if ids is not None:
        inc = inc[inc["station_id"].isin(ids)]
    for r in inc.itertuples(index=False):
        name = label.get(r.station_id, r.station_id)
        why = cause.get((r.station_id, r.start), "")
        if r.severity == "red":
            if r.start >= since and not r.censored_start:
                add(r.start, "down", "critical", r.station_id, f"{name} went down", why)
            if r.status == "resolved" and r.end >= since:
                add(r.end, "recovered", "good", r.station_id, f"{name} is back up",
                    f"Down for {_dur(r.duration_s)}" + (f" ({why})" if why else ""))
        else:
            if r.start >= since and not r.censored_start:
                add(r.start, "warning", "warning", r.station_id, f"{name} has a warning", why)
            if r.status == "resolved" and r.end >= since:
                add(r.end, "warning_cleared", "info", r.station_id, f"{name} warning cleared",
                    f"After {_dur(r.duration_s)}")

    repl = ds.repl[ds.repl["ts"] >= since]
    if ids is not None:
        repl = repl[repl["station_id"].isin(ids)]
    for r in repl.itertuples(index=False):
        part = config.COMPONENT_LABELS.get(r.component, r.component)
        add(r.ts, "replaced", "info", r.station_id, f"{part} replaced at {label.get(r.station_id, r.station_id)}",
            f"{r.level_before:.0f}% was left in the old part")

    trays = ds.tray_inc
    if ids is not None:
        trays = trays[trays["station_id"].isin(ids)]
    for r in trays.itertuples(index=False):
        name = label.get(r.station_id, r.station_id)
        if r.start >= since and not r.censored_start:
            add(r.start, "tray_empty", "info", r.station_id, f"{r.tray} empty at {name}")
        if r.status == "resolved" and r.end >= since:
            add(r.end, "tray_refilled", "info", r.station_id, f"{r.tray} refilled at {name}",
                f"Empty for {_dur(r.duration_s)}")

    if ids is None:
        _monitoring_gaps(ds, since, add)

    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(rows, columns=COLUMNS).sort_values("ts", ascending=False, ignore_index=True)


def _monitoring_gaps(ds: Dataset, since, add) -> None:
    """Runs of failed scrapes long enough to matter become gap / restored events."""
    log = ds.log[ds.log["attempt_ts"] >= since - pd.Timedelta(hours=1)]
    if log.empty:
        return
    ok = log["ok"].to_numpy()
    ts = log["attempt_ts"].reset_index(drop=True)
    run_start = None
    for i, good in enumerate(ok):
        if not good and run_start is None:
            run_start = i
        elif good and run_start is not None:
            minutes = i - run_start
            if minutes >= MIN_GAP_MINUTES and ts[i] >= since:
                add(ts[run_start], "data_gap", "warning", None, "Monitoring gap: the status page couldn't be read",
                    f"{minutes} failed refreshes in a row")
                add(ts[i], "data_restored", "good", None, "Monitoring restored",
                    f"Gap lasted {_dur((ts[i] - ts[run_start]).total_seconds())}")
            run_start = None
    if run_start is not None and len(ok) - run_start >= MIN_GAP_MINUTES:
        add(ts[run_start], "data_gap", "warning", None, "Monitoring gap: the status page couldn't be read",
            "Still failing")


def notifications(ds: Dataset, hours: int = 72) -> pd.DataFrame:
    ev = events(ds, since=ds.as_of - pd.Timedelta(hours=hours))
    return ev[ev["kind"].isin(NOTIFY_KINDS)].reset_index(drop=True)
