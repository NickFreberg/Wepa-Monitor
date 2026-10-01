"""Support ownership and desk hours.

Every station belongs to a team (ResNet or the IT Service Center, see
config.SUPPORT_TEAMS), and each team's desk is staffed only at certain hours.
An outage that starts after hours can't be fixed until someone is in, so the
useful questions are: did it start during desk hours, how long did it wait for
the desk to open, and how much of its downtime fell inside desk hours (the
part staff could act on)?
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from . import config

TZ = config.LOCAL_TZ
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TEAMS = list(config.SUPPORT_TEAMS)


def owner(section: str) -> str:
    return config.SECTION_OWNER.get(section, config.DEFAULT_OWNER)


def team(name: str) -> dict:
    return config.SUPPORT_TEAMS.get(name, config.SUPPORT_TEAMS[config.DEFAULT_OWNER])


def _clock(h: float) -> str:
    hh, mm = int(h), int(round((h - int(h)) * 60))
    return f"{(hh % 12) or 12}{':%02d' % mm if mm else ''} {'AM' if hh < 12 else 'PM'}"


def hours_text(name: str) -> str:
    """'Mon–Thu 10 AM–6 PM, Fri 10 AM–4 PM'."""
    hours = team(name)["hours"]
    groups: list[list] = []
    for d in range(7):
        span = hours.get(d)
        if span and groups and groups[-1][2] == span and groups[-1][1] == d - 1:
            groups[-1][1] = d
        elif span:
            groups.append([d, d, span])
    return ", ".join(f"{DAYS[a]}{'–' + DAYS[b] if b > a else ''} {_clock(s[0])}–{_clock(s[1])}" for a, b, s in groups)


def _span(name: str, day: pd.Timestamp):
    """(open, close) local timestamps for a local calendar day, or None if closed."""
    if day.strftime("%Y-%m-%d") in config.SUPPORT_CLOSED_DATES:
        return None
    s = team(name)["hours"].get(day.dayofweek)
    if not s:
        return None
    base = day.tz_localize(None).normalize()
    return (pd.Timestamp(base + timedelta(hours=s[0])).tz_localize(TZ),
            pd.Timestamp(base + timedelta(hours=s[1])).tz_localize(TZ))


def is_open(name: str, ts: pd.Timestamp) -> bool:
    local = ts.tz_convert(TZ)
    span = _span(name, local)
    return bool(span and span[0] <= local < span[1])


def next_open(name: str, ts: pd.Timestamp) -> pd.Timestamp | None:
    """The first staffed moment at or after ts (ts itself when the desk is open)."""
    local = ts.tz_convert(TZ)
    for i in range(15):
        span = _span(name, (local + pd.Timedelta(days=i)).normalize())
        if span and span[1] > local:
            return max(span[0], local)
    return None


def staffed_seconds(name: str, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Seconds of [start, end) that fall inside the team's desk hours."""
    if pd.isna(start) or pd.isna(end) or end <= start:
        return 0.0
    s, e = start.tz_convert(TZ), end.tz_convert(TZ)
    total, day = 0.0, s.normalize()
    while day <= e:
        span = _span(name, day)
        if span:
            lo, hi = max(s, span[0]), min(e, span[1])
            if hi > lo:
                total += (hi - lo).total_seconds()
        day = (day + pd.Timedelta(days=1, hours=2)).normalize()   # DST-safe step to the next day
    return total


def desk_status(name: str, now: pd.Timestamp) -> tuple[bool, str]:
    """(open?, "Open until 6 PM" / "Closed until Monday 10 AM")."""
    local = now.tz_convert(TZ)
    span = _span(name, local)
    if span and span[0] <= local < span[1]:
        return True, f"Open until {_clock(span[1].hour + span[1].minute / 60)}"
    nxt = next_open(name, now)
    if nxt is None:
        return False, "Closed"
    if nxt.date() == local.date():
        day = "today"
    elif nxt.date() == (local + pd.Timedelta(days=1)).date():
        day = "tomorrow"
    else:
        day = f"{nxt:%A}"
    return False, f"Closed until {day} {_clock(nxt.hour + nxt.minute / 60)}"


def grid(name: str) -> np.ndarray:
    """7 x 24 share of each weekday-hour that the desk is staffed (for chart overlays)."""
    g = np.zeros((7, 24))
    for d, (a, b) in team(name)["hours"].items():
        for h in range(24):
            g[d, h] = max(0.0, min(b, h + 1) - max(a, h))
    return g


def annotate(inc: pd.DataFrame, stations: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Add owner, in_hours (began while the desk was open), wait_s (until the desk opened),
    staffed_s and after_s (downtime inside / outside desk hours) to an incident table."""
    out = inc.copy()
    own = stations.set_index("station_id")["owner"] if "owner" in stations else pd.Series(dtype=str)
    out["owner"] = out["station_id"].map(own).fillna(config.DEFAULT_OWNER)
    if out.empty:
        for c, v in (("in_hours", False), ("wait_s", 0.0), ("staffed_s", 0.0), ("after_s", 0.0)):
            out[c] = pd.Series(dtype=type(v))
        return out
    ends = out["end"].fillna(as_of) if "end" in out else pd.Series(as_of, index=out.index)
    in_hours, wait, staffed = [], [], []
    for o, s, e in zip(out["owner"], out["start"], ends):
        in_hours.append(is_open(o, s))
        nxt = next_open(o, s)
        wait.append(0.0 if nxt is None else max(0.0, min((nxt - s).total_seconds(), (e - s).total_seconds())))
        staffed.append(staffed_seconds(o, s, e))
    out["in_hours"] = in_hours
    out["wait_s"] = wait
    out["staffed_s"] = staffed
    out["after_s"] = ((ends - out["start"]).dt.total_seconds() - out["staffed_s"]).clip(lower=0)
    return out


def summary(inc: pd.DataFrame) -> pd.DataFrame:
    """Per owner: outages, share that began after hours, downtime split, and desk-hours time to fix."""
    rows = []
    for name in TEAMS:
        d = inc[inc["owner"] == name]
        if d.empty:
            continue
        res = d[d["status"] == "resolved"] if "status" in d else d
        rows.append({
            "owner": name, "base": team(name)["base"], "hours": hours_text(name), "outages": len(d),
            "after_hours": int((~d["in_hours"].astype(bool)).sum()),
            "after_share": float((~d["in_hours"].astype(bool)).mean()),
            "staffed_h": d["staffed_s"].sum() / 3600, "after_h": d["after_s"].sum() / 3600,
            "median_fix_s": res["duration_s"].median() if len(res) else np.nan,
            "median_desk_fix_s": res["staffed_s"].median() if len(res) else np.nan,
            "median_wait_s": d.loc[~d["in_hours"].astype(bool), "wait_s"].median()
            if (~d["in_hours"].astype(bool)).any() else np.nan,
        })
    return pd.DataFrame(rows)
