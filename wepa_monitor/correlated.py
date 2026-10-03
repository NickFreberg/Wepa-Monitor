"""Shared outages: several printers going down together, which points at one common cause (the campus
network, Wepa's service, a building's power) rather than several separate printer problems.

Method
------
Outage starts are swept in time order; starts within WINDOW_MIN of the first one in a group form a
cluster. A cluster of MIN_STATIONS or more different printers is reported. Is it more than chance?
If outages started independently, the number starting in any WINDOW_MIN window would be Poisson with
the rate observed for that hour of the day (weekdays and weekends separately, so busy hours aren't
mistaken for shared causes). Summed over every window in the period, the expected number of windows
with at least k starts by coincidence is sum_b (minutes in bucket b / WINDOW_MIN) x P(N_b >= k). When
that is far below 1, a cluster of k is very unlikely to be coincidence. Even then a cluster whose
printers reported unrelated problems (jams in one, paper in another) is reported as a busy moment,
not a shared cause: timing alone doesn't prove a common cause.

Each cluster is labelled from what the printers reported: mostly "not reachable" -> network or Wepa
service; all in one building -> that building; otherwise mixed. Recovering together (most back
within WINDOW_MIN of each other) strengthens the common-cause reading.
"""
from __future__ import annotations

import pandas as pd
from scipy import stats

from . import config, metrics as M, narrative as N, rules

WINDOW_MIN = 10
MIN_STATIONS = 3


def clusters(ds: M.Dataset, start, end, ids=None, window_min: int = WINDOW_MIN,
             min_stations: int = MIN_STATIONS) -> pd.DataFrame:
    red = M._in(ds.sev_inc, "start", start, end, ids)
    red = red[(red["severity"] == "red") & ~red["censored_start"].fillna(False).astype(bool)].sort_values("start")
    cols = ["start", "end", "stations", "buildings", "station_ids", "labels", "building_list", "causes", "kind",
            "together", "chance_windows", "verdict"]
    if red.empty:
        return pd.DataFrame(columns=cols)
    st = ds.stations.set_index("station_id")
    w = pd.Timedelta(minutes=window_min)
    groups, cur, first = [], [], None
    for _, r in red.iterrows():
        if first is not None and r["start"] - first <= w:
            cur.append(r)
            continue
        if cur:
            groups.append(cur)
        cur, first = [r], r["start"]
    if cur:
        groups.append(cur)

    minutes = max((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 60, window_min)
    # Outage starts per minute for each (weekend?, hour) bucket, and how many minutes of each the period has.
    grid = pd.date_range(pd.Timestamp(start).floor("h"), pd.Timestamp(end), freq="h")
    def bucket(ts):
        loc = pd.DatetimeIndex(ts).tz_convert(config.LOCAL_TZ)
        return list(zip(loc.dayofweek >= 5, loc.hour))
    mins = pd.Series(1, index=bucket(grid)).groupby(level=0).sum() * 60.0
    starts = pd.Series(1, index=bucket(red["start"])).groupby(level=0).sum()
    rates = (starts.reindex(mins.index, fill_value=0) / mins).to_dict()

    def chance_of(k: int) -> float:
        return float(sum(m / window_min * stats.poisson.sf(k - 1, rates[b] * window_min) for b, m in mins.items()))
    rate = len(red) / minutes
    out = []
    for g in groups:
        df = pd.DataFrame(g).drop_duplicates("station_id")
        k = len(df)
        if k < min_stations:
            continue
        codes = []
        for sid, t in zip(df["station_id"], df["start"]):
            c = N.outage_causes(ds, sid, t)
            codes.append(c[0][0] if c else "")
        offline = sum(c == "not_reachable" for c in codes)
        blds = df["station_id"].map(st["building"]).fillna("?")
        if offline >= k / 2:
            kind = "Network or Wepa service"
        elif blds.nunique() == 1:
            kind = f"Inside {blds.iloc[0]}"
        else:
            kind = "Mixed causes"
        ends = df["end"].dropna()
        together = bool(len(ends) == k and (ends.max() - ends.min()) <= w)
        chance = chance_of(k)
        if kind == "Mixed causes":
            verdict = "a busy moment: the printers reported unrelated problems"
        else:
            verdict = ("very unlikely to be coincidence" if chance < 0.05 else
                       "probably a shared cause" if chance < 0.5 else "could be coincidence")
        cause_count = pd.Series([rules.issue_label(c) if c else "not reported" for c in codes]).value_counts()
        out.append({
            "start": df["start"].min(), "end": ends.max() if len(ends) == k else pd.NaT, "stations": k,
            "buildings": int(blds.nunique()), "station_ids": list(df["station_id"]),
            "labels": ", ".join(df["station_id"].map(st["label"]).fillna(df["station_id"])),
            "building_list": ", ".join(sorted(blds.unique())),
            "causes": ", ".join(f"{c} ({n})" for c, n in cause_count.items()),
            "kind": kind, "together": together, "chance_windows": chance, "verdict": verdict})
    res = pd.DataFrame(out, columns=cols)
    res.attrs.update(rate_per_hour=rate * 60, window_min=window_min, min_stations=min_stations, outages=len(red))
    return res.sort_values("start", ascending=False).reset_index(drop=True)


def summary(c: pd.DataFrame) -> str:
    if c.empty:
        return (f"No times when {c.attrs.get('min_stations', MIN_STATIONS)} or more printers went out of service within "
                f"{c.attrs.get('window_min', WINDOW_MIN)} minutes of each other: outages look independent.")
    likely = c[(c["chance_windows"] < 0.5) & (c["kind"] != "Mixed causes")]
    return (f"{len(c)} time{'s' if len(c) != 1 else ''} several printers went out of service together; "
            f"{len(likely)} look{'s' if len(likely) == 1 else ''} like a shared cause (same kind of problem, "
            "more than chance would explain).")
