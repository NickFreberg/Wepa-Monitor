"""Plain-language stories about any stretch of time ("How was yesterday?").

This is template-based natural-language generation: every sentence is computed
from the same metric and incident tables the charts use, and a sentence is only
written when there's evidence for it. No AI model, no tokens, nothing leaves the
app.

A story is a list of paragraphs. Each paragraph is a list of segments, so the UI
can bold key numbers and turn station names into links:
    "plain text" | ("b", "bold text") | ("st", station_id, "link text")
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import activity, config, metrics as M, models, support

TZ = config.LOCAL_TZ


@dataclass
class Period:
    key: str
    label: str            # "yesterday"
    title: str            # "Yesterday"
    start: pd.Timestamp
    end: pd.Timestamp
    prev_start: pd.Timestamp
    prev_end: pd.Timestamp
    prev_label: str       # "the day before"
    partial: bool = False


PERIOD_KEYS = [("today", "Today"), ("yesterday", "Yesterday"), ("this_week", "This week"),
               ("last_week", "Last week"), ("this_month", "This month"), ("last_month", "Last month")]


def period(ds: M.Dataset, key: str) -> Period:
    now = ds.as_of.tz_convert(TZ)
    day0 = now.normalize()
    week0 = day0 - pd.Timedelta(days=day0.dayofweek)                     # Monday
    month0 = day0.replace(day=1)
    prev_month0 = (month0 - pd.Timedelta(days=1)).replace(day=1)
    u = lambda t: t.tz_convert("UTC")  # noqa: E731
    if key == "today":
        p = Period(key, "today", "Today", u(day0), ds.as_of, u(day0 - pd.Timedelta(days=1)),
                   u(day0 - pd.Timedelta(days=1)) + (ds.as_of - u(day0)), "yesterday at the same time", True)
    elif key == "yesterday":
        p = Period(key, "yesterday", "Yesterday", u(day0 - pd.Timedelta(days=1)), u(day0),
                   u(day0 - pd.Timedelta(days=2)), u(day0 - pd.Timedelta(days=1)), "the day before")
    elif key == "this_week":
        p = Period(key, "this week", "This week", u(week0), ds.as_of, u(week0 - pd.Timedelta(days=7)),
                   u(week0 - pd.Timedelta(days=7)) + (ds.as_of - u(week0)), "the same point last week", True)
    elif key == "last_week":
        p = Period(key, "last week", "Last week", u(week0 - pd.Timedelta(days=7)), u(week0),
                   u(week0 - pd.Timedelta(days=14)), u(week0 - pd.Timedelta(days=7)), "the week before")
    elif key == "this_month":
        p = Period(key, f"{now:%B} so far", "This month", u(month0), ds.as_of, u(prev_month0),
                   u(prev_month0) + (ds.as_of - u(month0)), f"the same point in {prev_month0:%B}", True)
    else:  # last_month
        pm_prev = (prev_month0 - pd.Timedelta(days=1)).replace(day=1)
        p = Period("last_month", f"{prev_month0:%B}", "Last month", u(prev_month0), u(month0), u(pm_prev),
                   u(prev_month0), f"{pm_prev:%B}")
    return clip(ds, p)


def custom_period(ds: M.Dataset, start: pd.Timestamp, end: pd.Timestamp, label: str) -> Period:
    span = end - start
    title = ("Over " + label if label.startswith("the last") else
             "Across " + label if label.startswith("all") else "In " + label)
    return clip(ds, Period("custom", label, title, start, end, start - span, start, "the period before"))


def during(p: Period) -> str:
    """'yesterday', 'last week', 'over the last 30 days', 'in September' (for mid-sentence use)."""
    return p.title[0].lower() + p.title[1:] if p.key == "custom" else p.label


def clip(ds: M.Dataset, p: Period) -> Period:
    """Never report on time before monitoring began."""
    if ds.data_start is not None:
        p.start = max(p.start, ds.data_start)
        p.prev_start = max(p.prev_start, ds.data_start)
    return p


# --- helpers -------------------------------------------------------------------------------------

def dur(seconds: float) -> str:
    m = seconds / 60
    if m < 90:
        return f"{m:.0f} min"
    if m < 48 * 60:
        return f"{m / 60:.1f} hours"
    return f"{m / 1440:.1f} days"


def hour(h: int) -> str:
    return f"{(h % 12) or 12} {'am' if h < 12 else 'pm'}"


def when(ts: pd.Timestamp, p: Period) -> str:
    local = ts.tz_convert(TZ)
    same_day = (p.end - p.start) <= pd.Timedelta(days=1, hours=1)
    return local.strftime("%-I:%M %p") if same_day else local.strftime("%a %b %-d at %-I:%M %p")


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def names(ds: M.Dataset) -> dict[str, str]:
    return activity.display_names(ds)


@dataclass
class Story:
    title: str
    tone: str                          # good | warning | critical | info
    headline: str
    paragraphs: list[list] = field(default_factory=list)
    stats: list[tuple[str, str]] = field(default_factory=list)     # (label, value) chips
    moments: pd.DataFrame | None = None


def _baseline(ds, p: Period, ids) -> float | None:
    """'Usual' availability: the 30 days before the period."""
    b = M.availability(ds, p.start - pd.Timedelta(days=30), p.start, ids)
    return b.value if b.value is not None and b.extra.get("coverage", 0) > 0.3 else None


def overlapping(ds: M.Dataset, p: Period, ids=None) -> pd.DataFrame:
    """Incidents active at any point in the period, including ones that began before it."""
    inc = ds.sev_inc if ids is None else ds.sev_inc[ds.sev_inc["station_id"].isin(ids)]
    finish = inc["end"].fillna(inc["last_seen"]).where(inc["status"] != "open", ds.as_of)
    return inc[(inc["start"] < p.end) & (finish >= p.start)]


def coverage(ds: M.Dataset, p: Period, red: pd.DataFrame, ids=None) -> list:
    """One paragraph on outages vs each owner's desk hours, plus who is open right now."""
    seg: list = []
    if len(red):
        after = ~red["in_hours"].astype(bool)
        owners = [o for o in support.TEAMS if (red["owner"] == o).any()]
        if after.any():
            seg += [("b", f"{after.sum()} of {len(red)}"), " outages began after support-desk hours"]
            parts = []
            for o in owners:
                d = red[red["owner"] == o]
                a_ = ~d["in_hours"].astype(bool)
                if len(owners) > 1:
                    parts.append(f"{o} {a_.sum()} of {len(d)}")
            seg.append(f" ({', '.join(parts)})" if parts else "")
            wait = red.loc[after, "wait_s"].median()
            seg.append(f"; those waited a median {dur(wait)} for a desk to open. " if wait >= 60 else ". ")
        else:
            seg.append("Every outage began while its support desk was open. ")
        res = M._resolved(red)
        if len(res) >= 3:
            seg += ["Counting only staffed hours, the typical fix took ", ("b", dur(res["staffed_s"].median())),
                    " of desk time"]
            if after.any() and (~after).sum() >= 2 and after.sum() >= 2:
                d_med = res.loc[res["in_hours"].astype(bool), "duration_s"].median()
                n_med = res.loc[~res["in_hours"].astype(bool), "duration_s"].median()
                if np.isfinite(d_med) and np.isfinite(n_med) and d_med > 0:
                    seg.append(f"; on the clock, after-hours outages lasted {dur(n_med)} vs {dur(d_med)} for ones "
                               "that started while the desk was open")
            seg.append(". ")
    if p.partial and p.key in ("today", "this_week"):
        status = []
        st = ds.stations if ids is None else ds.stations[ds.stations["station_id"].isin(ids)]
        for o in [o for o in support.TEAMS if (st["owner"] == o).any()]:
            _, txt = support.desk_status(o, ds.as_of)
            status.append(f"{o} ({support.team(o)['short']}) is {txt[0].lower() + txt[1:]}")
        seg.append("Right now: " + "; ".join(status) + ".")
    return seg


def story(ds: M.Dataset, p: Period, ids=None, scope_label: str = "BSU print stations",
          singular: bool = False) -> Story:
    nm = names(ds)
    a = M.availability(ds, p.start, p.end, ids)
    if a.value is None or p.end <= p.start:
        return Story(p.title, "info", f"No data for {p.label} yet.",
                     [["The monitor hasn't recorded anything for this period. Once snapshots arrive, the story "
                       "writes itself."]])
    prev = M.availability(ds, p.prev_start, p.prev_end, ids)
    base = _baseline(ds, p, ids)
    so_far = " so far" if p.partial and p.key not in ("this_month",) else ""
    paras: list[list] = []

    # 1. Lead: how did it go, compared with usual and with the previous period?
    ref = base if base is not None else prev.value
    if ref is not None:
        d = a.value - ref
        verdict = "better than usual" if d >= 0.5 else ("about typical" if d > -0.5 else
                                                        "rougher than usual" if d > -2 else "a rough stretch")
    else:
        verdict = None
    # Tone follows the comparison with normal, with absolute floors so a bad norm can't look "good".
    if a.value < 85 or verdict == "a rough stretch":
        tone = "critical"
    elif a.value < 90 or verdict == "rougher than usual":
        tone = "warning"
    elif verdict is None:
        tone = "good" if a.value >= 97 else "warning"
    else:
        tone = "good"
    headline = f"{p.title}{so_far}: {scope_label} could print {a.value:.1f}% of the time"
    were, their = ("was", "its") if singular else ("were", "their")
    lead = [f"{p.title}{so_far}, {scope_label} {were} available ",
            ("b", f"{a.value:.1f}%"), " of the time"]
    if verdict:
        lead += [", which is ", ("b", verdict)]
        if base is not None:
            lead.append(f" ({their} 30-day norm is {base:.1f}%)")
    if prev.value is not None:
        d = a.value - prev.value
        if abs(d) >= 0.1:
            lead.append(f", {'up' if d > 0 else 'down'} {abs(d):.1f} points from {p.prev_label}")
    lead.append(". ")
    down_h = a.extra.get("down_h", 0)
    lead += ["Altogether that's about ", ("b", f"{down_h:,.0f} printer-hours"), " when a station couldn't print."]
    paras.append(lead)

    # 2. What went wrong: outages, the longest, the most frequent.
    inc = overlapping(ds, p, ids)
    red = inc[inc["severity"] == "red"]
    carried = int((red["start"] < p.start).sum())
    faults = M.faults_in(ds, p.start, p.end, ids)
    cause = faults.groupby(["station_id", "start"])["label"].agg(lambda s: ", ".join(sorted(set(s))).lower())
    if red.empty:
        paras.append(["No station went down. ", "Any time lost came from monitoring gaps or warnings, "
                      "not outages." if down_h > 0.5 else "Every station that reported could print throughout."])
    else:
        dur_s = red["duration_s"].fillna(0)
        longest = red.loc[dur_s.idxmax()]
        why = cause.get((longest["station_id"], longest["start"]), "")
        still = longest["status"] == "open"
        carried_txt = (f" (including {carried} that began before {during(p)} started)" if carried > 1 else
                       " (it began before the period started)" if carried == 1 and len(red) == 1 else
                       " (including one that began before the period)" if carried == 1 else "")
        seg = [("b", plural(len(red), "outage")), carried_txt,
               (" in total. The longest was " if len(red) > 1 else ": "),
               ("st", longest["station_id"], nm.get(longest["station_id"], longest["station_id"])),
                f", {'down for ' + dur(longest['duration_s']) + ' and still not fixed' if still else 'down for ' + dur(longest['duration_s'])}"
                f" from {when(longest['start'], p)}" + (f" ({why})" if why else "") + "."]
        by_st = red.groupby("station_id").size().sort_values(ascending=False)
        if len(by_st) and by_st.iloc[0] >= 3 and by_st.index[0] != longest["station_id"]:
            seg += [" ", ("st", by_st.index[0], nm.get(by_st.index[0], by_st.index[0])),
                    f" went down most often ({by_st.iloc[0]} times)."]
        resolved = M._resolved(red)
        if len(resolved) >= 3:
            seg += [f" Outages took a median {dur(resolved['duration_s'].median())} to fix."]
        paras.append(seg)

    # 3. Patterns: what kind of problem, and when.
    if len(faults) >= 3:
        top = faults["label"].value_counts()
        seg = ["The most common problem was ", ("b", top.index[0].lower()),
               f" ({top.iloc[0]} of {len(faults)} faults)"]
        hours = faults["hour"]
        if len(hours) >= 6:
            counts = hours.value_counts().reindex(range(24), fill_value=0).to_numpy()
            win = np.array([counts[[h, (h + 1) % 24, (h + 2) % 24]].sum() for h in range(24)])
            h0 = int(win.argmax())
            if win[h0] / len(hours) >= 0.3:
                seg.append(f", and problems bunched up between {hour(h0)} and {hour((h0 + 3) % 24)}")
        seg.append(".")
        paras.append(seg)

    # 3b. Support coverage: did problems start while the owning desk was open?
    cover = coverage(ds, p, red, ids)
    if cover:
        paras.append(cover)

    # 4. Supplies: paper and parts.
    trays = M._in(ds.tray_inc, "start", p.start, p.end, ids)
    repl = M.replacements(ds, p.start, p.end, ids)
    seg = []
    if len(trays):
        tot = trays.groupby("station_id")["duration_s"].sum().sort_values(ascending=False)
        seg += [("b", plural(len(trays), "paper tray")), " ran empty"]
        if tot.iloc[0] >= 3600:
            seg += ["; ", ("st", tot.index[0], nm.get(tot.index[0], tot.index[0])),
                    f" had an empty tray for {dur(tot.iloc[0])} in total"]
        seg.append(". " if len(repl) else ".")
    if len(repl):
        parts = repl["label"].value_counts()
        listed = ", ".join(f"{n} {lab}" for lab, n in parts.head(3).items())
        seg += [("b", plural(len(repl), "part")), f" {'was' if len(repl) == 1 else 'were'} replaced ({listed})."]
        early = repl[(repl["component"].str.startswith("toner")) & (repl["level_before"] > config.CONSUMABLE_REPLACE_PCT)]
        if len(early):
            seg.append(f" {plural(len(early), 'toner')} {'was' if len(early) == 1 else 'were'} swapped early, "
                       f"with {early['level_before'].mean():.0f}% still left on average.")
    if seg:
        paras.append(seg)

    # 5. Looking ahead (only for periods that include now).
    if p.partial:
        eol = models.eol_forecast(ds, ids)
        due = eol[eol["days"] <= 7].sort_values("days")
        if len(due):
            first = due.iloc[0]
            when_txt = "now" if first["days"] == 0 else f"in about {first['days']:.0f} day{'s' if round(first['days']) != 1 else ''}"
            paras.append(["Looking ahead: ", ("b", plural(len(due), "consumable")),
                          f" {'is' if len(due) == 1 else 'are'} at or within a week of end of life, most urgently ",
                          ("st", first["station_id"], nm.get(first["station_id"], first["station_id"])),
                          f"'s {first['label']} ({when_txt})."])

    # 6. Data caveat, only when it matters.
    dq = M.data_quality(ds, p.start, p.end)
    if dq.value is not None and dq.extra["completeness"] < 97:
        paras.append([f"A caution: the monitor captured only {dq.extra['completeness']:.0f}% of expected snapshots, "
                      "so the figures above may understate what happened."])

    after = (~red["in_hours"].astype(bool)).mean() if len(red) else None
    stats = [("Could print", f"{a.value:.1f}%"), ("Outages", str(len(red))),
             *([("Began after hours", f"{after:.0%}")] if after is not None else []),
             ("Trays emptied", str(len(trays))), ("Parts replaced", str(len(repl)))]
    moments = activity.events(ds, since=p.start, ids=ids)
    moments = moments[(moments["ts"] < p.end) & moments["kind"].isin(["down", "recovered", "replaced", "data_gap"])]
    return Story(p.title, tone, headline, paras, stats, moments.head(6))


def to_text(paragraphs: list[list]) -> str:
    """Flatten segments to plain text (for tests and exports)."""
    out = []
    for para in paragraphs:
        out.append("".join(s if isinstance(s, str) else s[-1] for s in para))
    return "\n\n".join(out)
