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

from . import durations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import activity, campus, config, metrics as M, models, nearby, ops, rules, support

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
    from . import durations
    return durations.human(seconds)


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


# "Usual" needs history: a norm from a day or two of monitoring would make ordinary days look
# "better than usual" (or worse) by chance. No norm until a week has been recorded.
MIN_HISTORY_FOR_NORM_DAYS = 7


def history_days(ds: M.Dataset, at: pd.Timestamp | None = None) -> float:
    """Days of monitoring recorded before `at` (default: now)."""
    if ds.data_start is None:
        return 0.0
    return max(0.0, ((at or ds.as_of) - ds.data_start).total_seconds() / 86400)


def _baseline(ds, p: Period, ids) -> float | None:
    """'Usual' availability: the 30 days before the period, once at least a week is recorded."""
    if history_days(ds, p.start) < MIN_HISTORY_FOR_NORM_DAYS:
        return None
    b = M.availability(ds, p.start - pd.Timedelta(days=30), p.start, ids)
    return b.value if b.value is not None and b.extra.get("coverage", 0) > 0.5 else None


def _comparable_prev(ds, p: Period, ids) -> M.Metric:
    """The previous period, only when the monitor actually covered most of it."""
    prev = M.availability(ds, p.prev_start, p.prev_end, ids)
    full = (p.prev_end - p.prev_start).total_seconds()
    covered = prev.extra.get("observed_h", 0) * 3600 / max(1, len(ids) if ids is not None else len(ds.stations))
    if prev.value is None or full <= 0 or covered / full < 0.8:
        return M.Metric(None, 0, False, "the previous period wasn't fully monitored")
    return prev


def availability_tone(value: float | None, norm: float | None = None) -> str:
    """One rule for every availability headline (stories, analytics, executive):
    good unless availability is low in absolute terms or clearly below its own norm.
    One printer out of ~30 down for a whole day is about 96.7%: worth a mention, not a warning."""
    if value is None:
        return "info"
    if norm is None:
        return "critical" if value < 90 else "warning" if value < 95 else "good"
    if value < 80 or value < norm - 4:
        return "critical"
    if value < 85 or value < norm - 1.5:
        return "warning"
    return "good"


def history_note(ds: M.Dataset, p: Period) -> str:
    """'Monitoring began Thu Oct 1 at 9:43 PM, so there's no "usual" to compare with yet.'"""
    if ds.data_start is None or history_days(ds, p.end) >= MIN_HISTORY_FOR_NORM_DAYS:
        return ""
    began = ds.data_start.tz_convert(TZ)
    covered = dur((p.end - p.start).total_seconds())
    if p.start <= ds.data_start + pd.Timedelta(minutes=5):
        return (f"Monitoring began {began:%a %b %-d at %-I:%M %p}, so this covers only the {covered} since, "
                "and there's no 'usual' to compare with until a week has been recorded.")
    return ("There's no 'usual' to compare with yet: monitoring began "
            f"{began:%a %b %-d}, and a norm needs at least a week of history.")


def overlapping(ds: M.Dataset, p: Period, ids=None) -> pd.DataFrame:
    """Incidents active at any point in the period, including ones that began before it."""
    inc = ds.sev_inc if ids is None else ds.sev_inc[ds.sev_inc["station_id"].isin(ids)]
    finish = inc["end"].fillna(inc["last_seen"]).where(inc["status"] != "open", ds.as_of)
    return inc[(inc["start"] < p.end) & (finish >= p.start)]


PHASE_SENTENCE = {
    "finals": "It was finals, the heaviest printing of the term, so every outage cost more than usual.",
    "reading": "It was a reading day before finals, when printing picks up.",
    "move_in": "It was move-in, when residents arrive and printing restarts after the summer.",
    "thanksgiving": "It was Thanksgiving recess, so demand was light.",
    "spring_break": "It was spring break",
    "winter_break": "It was winter break",
    "summer": "It was the summer break",
    "summer_session": "It was a summer session, with a fraction of the usual students on campus.",
}


def _local_date(ts: pd.Timestamp):
    return ts.tz_convert(TZ).date()


def calendar_context(ds: M.Dataset, p: Period, ids=None) -> list:
    """Where the period sits in the academic year, notable days in it, and how much of the
    downtime happened while the building was closed or empty."""
    c = campus.load()
    if c.empty:
        return []
    start, end = _local_date(p.start), _local_date(p.end - pd.Timedelta(seconds=1))
    days = c.range(start, end)
    if days.empty:
        return []
    seg: list = []
    main = days["phase"].value_counts().index[0]
    if main in PHASE_SENTENCE and (days["phase"] == main).mean() >= 0.5:
        line = PHASE_SENTENCE[main]
        st = ds.stations if ids is None else ds.stations[ds.stations["station_id"].isin(ids)]
        if not line.endswith("."):
            halls_shut = (~days["halls_open"]).mean() >= 0.5 and (st["station_type"] == "residence").any()
            line += (", with the residence halls closed, so the hall printers sat mostly idle." if halls_shut
                     else ", so demand was light.")
        seg.append(line + " ")
    holidays = c.events[(c.events["kind"] == "holiday") & (c.events["date"] >= start) & (c.events["date"] <= end)]
    if len(holidays) and main != "summer":
        h = holidays.iloc[0]
        name = h["event"].split("–")[0].split(" - ")[0].strip()
        when_ = "today" if h["date"] == _local_date(ds.as_of) else f"{pd.Timestamp(h['date']):%A, %b %-d}"
        seg.append(f"{when_[:1].upper() + when_[1:]} was {name}, with no classes and the support desks closed. ")
    # Exposure: downtime while nobody could have used the printer.
    inc = overlapping(ds, p, ids)
    red = inc[inc["severity"] == "red"]
    if len(red) and "in_use_s" in red:
        finish = red["end"].fillna(ds.as_of)
        total = (finish.clip(upper=p.end) - red["start"].clip(lower=p.start)).dt.total_seconds().clip(lower=0).sum()
        idle = red["start"].count() and max(0.0, total - red["in_use_s"].sum())
        if total > 0 and idle / total >= 0.1:
            seg.append(f"About {idle / total:.0%} of the downtime came while the building was closed or empty "
                       "(residence halls closed, or the library shut), so fewer students felt it than the "
                       "hours suggest. ")
    return seg


KEY_EVENTS = {"finals_start": "finals begin", "reading_day": "reading day", "holiday": None,
              "thanksgiving_start": "Thanksgiving recess begins", "break_start": "spring break begins",
              "halls_close": "residence halls close", "move_in": "move-in begins", "classes_begin": "classes begin",
              "commencement": None}


def coming_up(ds: M.Dataset, eol: pd.DataFrame, ids=None) -> list:
    """The next calendar event worth preparing for, and the parts that will run out before it."""
    c = campus.load()
    if c.empty:
        return []
    today = _local_date(ds.as_of)
    up = c.upcoming(today, days=28)
    up = up[up["kind"].isin(KEY_EVENTS)]
    if up.empty:
        return []
    e = up.iloc[0]
    name = KEY_EVENTS[e["kind"]] or e["event"].split("–")[0].split(" - ")[0].strip()
    days = (e["date"] - today).days
    seg = ["Coming up: ", ("b", f"{name[:1].upper() + name[1:]}"),
           f" on {pd.Timestamp(e['date']):%a %b %-d} (in {plural(days, 'day')})."]
    if e["kind"] in ("finals_start", "reading_day", "move_in", "classes_begin"):
        before = eol[eol["days"].notna() & (eol["days"] <= days + 5)]
        if len(before):
            seg.append(f" {plural(len(before), 'part')} {'is' if len(before) == 1 else 'are'} projected to reach end "
                       "of life before or during it; replacing them ahead of time avoids outages at the busiest "
                       "moment.")
        else:
            seg.append(" No parts are projected to run out before then.")
    elif e["kind"] in ("holiday", "thanksgiving_start", "break_start", "halls_close"):
        seg.append(" Both desks will be closed; a quick check of paper and parts the day before keeps stations "
                   "printing through it." if e["kind"] in ("holiday", "thanksgiving_start") else
                   " Hall printers will go quiet: a good window for maintenance.")
    return seg


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


def outage_causes(ds: M.Dataset, station_id: str, start: pd.Timestamp) -> list[tuple[str, str]]:
    """(issue, detail) for the problems that opened with an outage, most specific first."""
    f = ds.fault_inc[ds.fault_inc["station_id"] == station_id]
    if f.empty:
        return []
    end = f["end"].fillna(ds.as_of)
    hit = f[((f["start"] - start).abs() <= pd.Timedelta(minutes=3)) | ((f["start"] <= start) & (end > start))]
    hit = hit[hit["severity"] == "red"] if (hit["severity"] == "red").any() else hit
    order = list(rules.ISSUES)
    hit = hit.assign(o=hit["code"].map(lambda c: order.index(c) if c in order else 99)).sort_values("o")
    return list(dict.fromkeys(zip(hit["code"], hit["detail"].fillna(""))))


def cause_phrase(causes: list[tuple[str, str]]) -> str:
    """'jammed (paper feed, duplex unit)' / 'ran out of paper and had a paper tray pulled out'."""
    parts = [rules.issue_phrase(c) + (f" ({d})" if d else "") for c, d in causes[:2]]
    return " and ".join(parts)


def outage_sentence(ds: M.Dataset, r, p: Period, nm: dict) -> list:
    """One outage, told as what happened and what it meant:
    'Stonehouse Hall jammed (paper feed) at 9:14 PM Tue, after ResNet's desk had closed; it waited
    12.8 hours for the desk to open and was printing again 13.5 hours later.'"""
    sid = r["station_id"]
    causes = outage_causes(ds, sid, r["start"])
    what = cause_phrase(causes) or "stopped printing"
    owner = r.get("owner") or config.DEFAULT_OWNER
    started_before = r["start"] < p.start
    seg: list = [("st", sid, nm.get(sid, sid)), f" {what} "]
    local = r["start"].tz_convert(TZ)
    same_day = (p.end - p.start) <= pd.Timedelta(days=1, hours=1) and not started_before
    seg.append(f"at {local:%-I:%M %p}" if same_day else f"on {local:%a %b %-d} at {local:%-I:%M %p}")
    if not started_before:
        if bool(r.get("in_hours", False)):
            seg.append(f", while the {owner} desk was open")
        else:
            seg.append(f", after the {owner} desk had closed")
    status = r["status"]
    took = r["duration_s"]
    if status == "open":
        seg += ["; it's ", ("b", f"still down after {dur(took)}")]
    elif status == "resolved" and np.isfinite(took):
        wait = r.get("wait_s", 0) or 0
        if not bool(r.get("in_hours", False)) and wait >= 1800 and wait < took:
            seg.append(f"; it waited {dur(wait)} for the desk to open and was printing again {dur(took)} later")
        else:
            seg.append(f"; it was printing again {dur(took)} later")
    else:
        seg.append("; the monitor lost sight of it before it cleared")
    seg.append(". ")
    return seg


def backup_sentence(ds: M.Dataset, sid: str, nm: dict) -> str:
    """Where students could print instead, in plain words with US distances."""
    st = ds.stations.set_index("station_id")
    if sid not in st.index:
        return ""
    building = st.loc[sid, "building"]
    others = st[(st["building"] == building) & (st.index != sid)]
    if len(others):
        return f"{building} has {plural(len(others), 'other printer')}, so students there weren't stranded. "
    cur = ops.current_status(ds)
    alt = nearby.backups(cur.assign(state="green"), sid, limit=1)
    alt = alt[alt["kind"] == "open to everyone"]
    if alt.empty:
        return f"It's the only printer in {building}. "
    a = alt.iloc[0]
    return (f"It's the only printer in {building}; the nearest one anyone can use is in {a['building']}, "
            f"{nearby.fmt_distance(a['meters'])} away. ")


def outages_paragraph(ds: M.Dataset, red: pd.DataFrame, p: Period, nm: dict) -> list:
    """The outages that cost students most (long, and in buildings without a backup), told one
    by one; the rest summarized."""
    single = ds.stations.groupby("building")["station_id"].transform("size").eq(1)
    alone = set(ds.stations.loc[single, "station_id"])
    took = red["duration_s"].fillna(0).clip(lower=0)
    weight = took * np.where(red["station_id"].isin(alone), 1.5, 1.0)
    red = red.assign(_w=weight.to_numpy()).sort_values("_w", ascending=False)
    n = len(red)
    carried = int((red["start"] < p.start).sum())
    seg: list = [("b", plural(n, "outage")),
                 f" {'touched' if carried else 'happened'} {during(p)}"
                 + (f" ({carried} already under way when it began)" if carried else "") + ". "]
    top = red[red["_w"] >= max(15 * 60, red["_w"].iloc[0] * 0.2)].head(3) if n > 1 else red
    for i, (_, r) in enumerate(top.iterrows()):
        if i == 0 and n > 1:
            seg.append("The one that mattered most: " if len(top) == 1 else "The ones that mattered most: ")
        seg += outage_sentence(ds, r, p, nm)
        if i == 0:
            b = backup_sentence(ds, r["station_id"], nm)
            if b:
                seg.append(b)
    rest = red.drop(top.index)
    if len(rest):
        res = M._resolved(rest)
        seg.append(f"The other {plural(len(rest), 'outage')} "
                   + (f"were short: typically fixed in {dur(res['duration_s'].median())}." if len(res) >= 2 and
                      res["duration_s"].median() < 3600 else
                      f"took a median {dur(res['duration_s'].median())} to fix." if len(res) >= 2 else
                      "were smaller."))
    by_st = red.groupby("station_id").size().sort_values(ascending=False)
    if len(by_st) and by_st.iloc[0] >= 3:
        sid = by_st.index[0]
        causes = outage_causes(ds, sid, red[red["station_id"] == sid]["start"].iloc[0])
        seg += [" ", ("st", sid, nm.get(sid, sid)), f" was out of service {by_st.iloc[0]} times"
                + (f", usually because it {rules.issue_phrase(causes[0][0])}" if causes else "")
                + ": worth a closer look than another quick fix."]
    return seg


def story(ds: M.Dataset, p: Period, ids=None, scope_label: str = "BSU print stations",
          singular: bool = False) -> Story:
    nm = names(ds)
    a = M.availability(ds, p.start, p.end, ids)
    if a.value is None or p.end <= p.start:
        return Story(p.title, "info", f"No data for {p.label} yet.",
                     [["The monitor hasn't recorded anything for this period. Once snapshots arrive, the story "
                       "writes itself."]])
    prev = _comparable_prev(ds, p, ids)
    base = _baseline(ds, p, ids)
    so_far = " so far" if p.partial and p.key not in ("this_month",) else ""
    paras: list[list] = []

    # 1. Lead: how did it go, compared with this time's own norm (once there is one)?
    verdict = None
    if base is not None:
        d = a.value - base
        verdict = ("better than usual" if d >= 0.5 else "about typical" if d > -0.5 else
                   "rougher than usual" if d > -2 else "a rough stretch")
    tone = availability_tone(a.value, base)
    headline = f"{p.title}{so_far}: {scope_label} could print {a.value:.1f}% of the time"
    were, their = ("was", "its") if singular else ("were", "their")
    lead = [f"{p.title}{so_far}, {scope_label} {were} available ",
            ("b", f"{a.value:.1f}%"), " of the time"]
    if verdict:
        lead += [", which is ", ("b", verdict), f" ({their} 30-day norm is {base:.1f}%)"]
    if prev.value is not None:
        d = a.value - prev.value
        if abs(d) >= 0.1:
            lead.append(f", {'up' if d > 0 else 'down'} {abs(d):.1f} points from {p.prev_label}")
    lead.append(". ")
    down_h = a.extra.get("down_h", 0)
    lead += ["Altogether that's about ", ("b", durations.hours(down_h)), " of printer time when a station couldn't print."]
    note = history_note(ds, p)
    if note:
        lead += [" " + note]
    ctx = calendar_context(ds, p, ids)
    if ctx:
        lead += [" "] + ctx
    paras.append(lead)

    # 2. What went wrong: the outages that mattered most, each told as what happened to whom.
    inc = overlapping(ds, p, ids)
    red = inc[inc["severity"] == "red"]
    faults = M.faults_in(ds, p.start, p.end, ids)
    if red.empty:
        paras.append(["No station was out of service. ", "Any time lost came from monitoring gaps or warnings, "
                      "not outages." if down_h > 0.5 else "Every station that reported could print throughout."])
    else:
        paras.append(outages_paragraph(ds, red, p, nm))
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
        seg = []
        if len(due):
            first = due.iloc[0]
            when_txt = "now" if first["days"] == 0 else f"in about {durations.days(first['days'])}"
            seg += ["Looking ahead: ", ("b", plural(len(due), "consumable")),
                    f" {'is' if len(due) == 1 else 'are'} at or within a week of end of life, most urgently ",
                    ("st", first["station_id"], nm.get(first["station_id"], first["station_id"])),
                    f"'s {first['label']} ({when_txt}). "]
        seg += coming_up(ds, eol, ids)
        if seg:
            paras.append(seg)

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
