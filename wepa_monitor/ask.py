"""'Ask the data': answer plain-English questions without an AI model.

A small rule-based interpreter:
  1. Find the time range ("yesterday", "last week", "past 10 days", "in September").
  2. Find what the question is about (a station number, a building, an area, a section).
  3. Pick the intent from keywords (status now, ranking, time to fix, when, faults,
     consumables, paper, forecast, comparison, or a general "how is it going").
  4. Answer with computed numbers in sentences, plus a few follow-up suggestions.

It's deliberately transparent: the answer says what it understood ("Looking at
Weygand Hall, last week"), so a misunderstanding is obvious and easy to rephrase.
"""
from __future__ import annotations

from . import durations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import campus, config, metrics as M, models, narrative as N, ops, support
from .narrative import dur, hour, plural

TZ = config.LOCAL_TZ
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], start=1)}
AREA_ALIASES = {"upper great hill": "Upper Great Hill", "great hill": "Upper Great Hill", "ugh": "Upper Great Hill",
                "university park": "University Park", "the park": "University Park",
                "west side": "West Side", "west campus": "West Side", "academic": "Academic",
                "satellite": "Satellite", "new bedford": "Satellite"}
SECTION_ALIASES = {"resnet": "ResNet", "residence hall": "ResNet", "residence halls": "ResNet", "dorm": "ResNet",
                   "dorms": "ResNet", "lab": "Student Computer Labs", "labs": "Student Computer Labs",
                   "computer lab": "Student Computer Labs"}
OWNER_ALIASES = {"it service center": "IT Service Center", "service center": "IT Service Center",
                 "itsc": "IT Service Center", "help desk": "IT Service Center", "helpdesk": "IT Service Center",
                 "maxwell": "IT Service Center"}
COMPONENT_WORDS = {"toner": "toner", "drum": "drum", "belt": "belt", "fuser": "fuser"}

EXAMPLES = [
    "How was yesterday?",
    "What's coming up on the calendar?",
    "Which station was down the longest last week?",
    "Which printers get used the most?",
    "How is Weygand doing this month?",
    "When do jams happen most?",
    "What toner will run out next?",
    "Compare this week to last week",
    "How many outages start after hours?",
    "Who supports Weygand?",
    "Which buildings ran out of paper the most?",
    "What's down right now?",
]


@dataclass
class Answer:
    understood: str                       # "Looking at Weygand Hall, last week"
    paragraphs: list[list]                # narrative segments (see narrative.py)
    table: pd.DataFrame | None = None
    table_cols: list[tuple[str, str]] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    tone: str = "info"


# --- parsing -----------------------------------------------------------------------------------------

PHASE_WORDS = [("finals", r"finals?( week)?|exam week|final exams?"), ("spring_break", r"spring break"),
               ("winter_break", r"winter break|christmas break|holiday break"), ("thanksgiving", r"thanksgiving"),
               ("move_in", r"move[- ]?in"), ("summer", r"(the )?summer( break)?"),
               ("reading", r"reading days?")]


def phase_period(ds: M.Dataset, phase: str) -> N.Period | None:
    """The most recent stretch of a calendar phase that has started by now ('finals' -> last finals)."""
    c = campus.load()
    if c.empty:
        return None
    today = ds.as_of.tz_convert(TZ).date()
    d = c.days[(c.days.index <= today)]
    hits = d.index[d["phase"] == phase]
    if phase == "summer":
        hits = d.index[d["phase"].isin(["summer", "summer_session"])]
    if not len(hits):
        return None
    end = hits[-1]
    start = end
    for day in reversed(hits[:-1]):
        if (start - day).days > 3:
            break
        start = day
    s = pd.Timestamp(start).tz_localize(TZ).tz_convert("UTC")
    e = min(pd.Timestamp(end + pd.Timedelta(days=1)).tz_localize(TZ).tz_convert("UTC"), ds.as_of)
    label = f"{campus.PHASES[phase].lower() if phase != 'summer' else 'the summer'} ({start:%b %-d}–{end:%b %-d})"
    return N.custom_period(ds, s, e, label)


def parse_period(ds: M.Dataset, q: str) -> N.Period:
    for phase, pat in PHASE_WORDS:
        if re.search(rf"\b({pat})\b", q) and not re.search(r"\b(when|next|coming|upcoming|until)\b", q):
            per = phase_period(ds, phase)
            if per is not None:
                return per
    for key, words in (("yesterday", ["yesterday"]), ("today", ["today", "so far today", "right now", "now"]),
                       ("last_week", ["last week", "previous week"]), ("this_week", ["this week", "week so far"]),
                       ("last_month", ["last month", "previous month"]), ("this_month", ["this month", "month so far"])):
        if any(re.search(rf"\b{w}\b", q) for w in words):
            return N.period(ds, key)
    m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s+(day|week|month)s?\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return N.custom_period(ds, ds.as_of - pd.Timedelta(days=days), ds.as_of, f"the last {n} {unit}{'s' if n > 1 else ''}")
    for name, num in MONTHS.items():
        if re.search(rf"\b(in\s+)?{name}\b", q):
            now = ds.as_of.tz_convert(TZ)
            year = now.year if num <= now.month else now.year - 1
            start = pd.Timestamp(year=year, month=num, day=1, tz=TZ)
            end = (start + pd.offsets.MonthBegin(1))
            return N.custom_period(ds, start.tz_convert("UTC"), min(end.tz_convert("UTC"), ds.as_of), f"{start:%B}")
    if re.search(r"\b(all time|ever|overall|since the start)\b", q):
        return N.custom_period(ds, ds.data_start or ds.as_of - pd.Timedelta(days=30), ds.as_of, "all recorded time")
    return N.custom_period(ds, ds.as_of - pd.Timedelta(days=30), ds.as_of, "the last 30 days")


@dataclass
class Subject:
    ids: list[str] | None
    label: str                    # "the printers at Weygand Hall", "Upper Great Hill stations", ...
    station_id: str | None = None
    singular: bool = False


def parse_subject(ds: M.Dataset, q: str) -> Subject:
    st = ds.stations
    m = re.search(r"\b0?(\d{4,5})\b", q)
    if m:
        sid = m.group(1).zfill(5)
        if sid in set(st["station_id"]):
            row = st[st["station_id"] == sid].iloc[0]
            return Subject([sid], f"{row['description']} (#{sid})", sid, singular=True)
    for alias, team in sorted(OWNER_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias != "maxwell" and re.search(rf"\b{alias}\b", q):
            return Subject(ds.ids(owner=team), f"{team} stations")
    # Buildings, by full or short name ("Weygand", "RSU", "ECC", "Tilly").
    candidates = []
    for _, r in st.drop_duplicates("building").iterrows():
        names = {r["building"].lower(), str(r.get("short_name", "")).lower()}
        names |= {r["building"].lower().replace(" hall", "").replace(" center", "").replace(" library", "")}
        for n in names:
            if n and len(n) >= 3 and re.search(rf"\b{re.escape(n)}\b", q):
                candidates.append((len(n), r["building"]))
    if candidates:
        building = max(candidates)[1]
        ids = st.loc[st["building"] == building, "station_id"].tolist()
        if len(ids) == 1:
            row = st[st["station_id"] == ids[0]].iloc[0]
            return Subject(ids, f"the printer at {building}", ids[0], singular=True)
        return Subject(ids, f"the printers at {building}")
    for alias, area in sorted(AREA_ALIASES.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{alias}\b", q):
            return Subject(ds.ids(area=area), f"{area} stations")
    for alias, section in sorted(SECTION_ALIASES.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{alias}\b", q):
            return Subject(ds.ids(section=section), "residence hall stations" if section == "ResNet" else "lab stations")
    return Subject(None, "BSU print stations")


def intent(q: str) -> str:
    rules = [
        ("calendar", r"\b(library (open|closed|hours)|is the library|library today|when (is|are|do|does) .*"
                     r"(finals?|break|classes|move[- ]?in|commencement|graduation|holiday|halls? (close|open)|"
                     r"semester|thanksgiving)|coming up|upcoming|next (holiday|break|day off)|academic calendar|"
                     r"no classes)\b"),
        ("usage", r"\b(busiest (printer|station|building|hall)s?|most used|least used|used (the )?(most|least)|"
                  r"usage|volume|how busy|popular|underused|under-used|idle|residents? per printer|"
                  r"students? per printer|crowded|capacity|add (a|another) printer|relocat|remove (a )?printer)\b"),
        ("compare", r"\b(compare|compared|vs\.?|versus|better or worse|difference between)\b"),
        ("forecast", r"\b(run out|running out|next|end of life|forecast|predict|due|need(s)? replac|about to)\b"),
        ("now", r"\b(right now|currently|at the moment|is .* down|what'?s down|down now|broken now)\b"),
        ("support", r"\b(after[- ]hours|business hours|desk hours|office hours|staffed|unstaffed|off[- ]hours|"
                    r"outside (of )?hours|weekends?|who (owns|supports|handles|looks after|is responsible)|owner|"
                    r"owns|support desk|desk open|is the desk|resnet hours|service center hours)\b|"
                    r"\b(open|closed)\b(?!.*\b(tray|trays|ticket)\b)"),
        ("time_to_fix", r"\b(how long|fix|repair|resolve|mttr|time to)\b"),
        ("paper", r"\b(paper|tray|trays)\b"),
        ("consumables", r"\b(toner|drum|belt|fuser|consumable|consumables|cartridge|cartridges|ink|parts?)\b"),
        ("when", r"\b(when|what time|which hours?|what day|busiest|peak)\b"),
        ("ranking", r"\b(worst|best|most|least|longest|which station|which building|top|unreliable|reliable)\b"),
        ("faults", r"\b(jam|jams|fault|faults|error|errors|problem|problems|issue|issues|why|cause)\b"),
    ]
    for name, pattern in rules:
        if re.search(pattern, q):
            return name
    return "overview"


def answer(ds: M.Dataset, question: str, scope_ids=None) -> Answer:
    q = " " + re.sub(r"[^\w\s'#.-]", " ", (question or "").lower()) + " "
    p = parse_period(ds, q)
    subj = parse_subject(ds, q)
    if subj.ids is None and scope_ids is not None:
        subj = Subject(scope_ids, "stations in your current scope")
    kind = intent(q)
    understood = f"Looking at {subj.label}, {N.during(p)}"
    recognized = (kind != "overview" or subj.ids is not None or p.label != "the last 30 days"
                  or re.search(r"\b(how|doing|going|summary|overview|story|status|what happened)\b", q))
    handler = {"compare": _compare, "forecast": _forecast, "now": _now, "time_to_fix": _time_to_fix,
               "paper": _paper, "consumables": _consumables, "when": _when, "ranking": _ranking,
               "faults": _faults, "support": _support, "calendar": _calendar, "usage": _usage,
               "overview": _overview}[kind]
    ans = handler(ds, p, subj, q)
    ans.understood = understood if kind != "now" else f"Looking at {subj.label}, right now"
    if kind == "calendar":
        ans.understood = "Looking at BSU's academic calendar and library hours"
    if kind == "compare":
        ans.understood = "Comparing two periods" + (f" for {subj.label}" if subj.ids is not None else "")
    if not recognized:
        ans.understood = "I didn't recognize that question, so here's an overview of the last 30 days"
    return ans


# --- handlers ------------------------------------------------------------------------------------------

def _link(ds, sid):
    return ("st", sid, N.names(ds).get(sid, sid))


def _overview(ds, p, subj, q):
    s = N.story(ds, p, subj.ids, subj.label, subj.singular)
    return Answer("", s.paragraphs, tone=s.tone,
                  followups=["Which station was down the longest " + p.label + "?",
                             "When do problems happen most?", "What will run out next?"])


def _now(ds, p, subj, q):
    cur = ops.current_status(ds, subj.ids)
    red, yel = cur[cur["state"] == "red"], cur[cur["state"] == "yellow"]
    if red.empty and yel.empty:
        what = subj.label if subj.label.endswith("stations") else f"the stations ({subj.label})"
        n_txt = "The one station" if len(cur) == 1 else f"All {len(cur)}"
        return Answer("", [[f"{n_txt} {what.replace('the stations', 'stations') if n_txt.startswith('All') else ''}"
                            f"{' is' if len(cur) == 1 else ' are'} printing right now, with no warnings."]],
                      tone="good", followups=["What will run out next?", "How was yesterday?"])
    paras = []
    if len(red):
        q_ = ops.work_queue(ds, subj.ids)
        q_ = q_[q_["kind"] == "red"].drop_duplicates("station_id")
        seg = [("b", f"{len(red)} {'station is' if len(red) == 1 else 'stations are'} down"), ": "]
        for i, r in enumerate(q_.itertuples(index=False)):
            seg += [", " if i else "", _link(ds, r.station_id),
                    f" ({r.issue.lower()}, {dur(r.open_min * 60)}{'+' if r.open_censored else ''})"]
        seg.append(".")
        paras.append(seg)
    if len(yel):
        seg = [f"{plural(len(yel), 'station')} {'has' if len(yel) == 1 else 'have'} a warning: "]
        for i, r in enumerate(yel.itertuples(index=False)):
            seg += [", " if i else "", _link(ds, r.station_id)]
        paras.append(seg + ["."])
    return Answer("", paras, tone="critical" if len(red) else "warning",
                  followups=["How long do outages usually take to fix?", "What will run out next?"])


def _ranking(ds, p, subj, q):
    sc = M.station_scorecard(ds, p.start, p.end, subj.ids)
    if sc.empty:
        return Answer("", [["No data for that period."]])
    best = bool(re.search(r"\b(best|most reliable|least)\b", q))
    if re.search(r"\b(building|buildings)\b", q):
        b = M.building_availability(ds, p.start, p.end, subj.ids).sort_values("any_up", ascending=not best)
        top = b.head(5)
        word = "best" if best else "least"
        para = [f"Measured by the share of time at least one printer could print, the {word}-covered buildings "
                f"{N.during(p)} were: "] + [f"{r.building} ({r.any_up:.1f}%)" + (", " if i < len(top) - 1 else ".")
                                         for i, r in enumerate(top.itertuples())]
        return Answer("", [para], table=top, table_cols=[("building", "Building"), ("any_up", "Any printer up %"),
                                                         ("stations", "Stations")],
                      followups=["Which station was down the longest " + p.label + "?"])
    down_h = (sc["observed_h"] * (100 - sc["availability"]) / 100).rename("down_h")
    sc = sc.assign(down_h=down_h)
    if re.search(r"\blongest\b", q):
        inc = M._in(ds.sev_inc, "start", p.start, p.end, subj.ids)
        red = inc[inc["severity"] == "red"].sort_values("duration_s", ascending=False).head(5)
        if red.empty:
            return Answer("", [[f"No station went down {N.during(p)}."]], tone="good")
        r0 = red.iloc[0]
        para = ["The longest single outage was at ", _link(ds, r0["station_id"]),
                f": down for {dur(r0['duration_s'])} starting {N.when(r0['start'], p)}"
                f"{' (still open)' if r0['status'] == 'open' else ''}."]
        tbl = red.assign(station=red["station_id"].map(N.names(ds)), lasted=red["duration_s"].map(dur),
                         started=red["start"].dt.tz_convert(TZ).dt.strftime("%a %b %-d %-I:%M %p"))
        return Answer("", [para], table=tbl, table_cols=[("station", "Station"), ("started", "Started"),
                                                         ("lasted", "Lasted")],
                      followups=["Why do stations go down?", "How long do outages usually take to fix?"])
    sc = sc.sort_values("availability", ascending=not best)
    top = sc.head(5)
    t0 = top.iloc[0]
    word = "most reliable" if best else "least reliable"
    para = [f"The {word} station {N.during(p)} was ", _link(ds, t0["station_id"]),
            f", available {t0['availability']:.1f}% of the time with {plural(int(t0['red_incidents']), 'outage')}"
            f"{', most often ' + str(t0['top_fault']).lower() if isinstance(t0['top_fault'], str) else ''}."]
    tbl = top.assign(avail=top["availability"].map(lambda v: f"{v:.1f}%"), mttr=top["mttr_red_min"].map(
        lambda v: dur(v * 60) if pd.notna(v) else "—"))
    return Answer("", [para], table=tbl, table_cols=[("label", "Station"), ("avail", "Available"),
                                                     ("red_incidents", "Outages"), ("mttr", "Typical fix time")],
                  followups=[f"How is {N.names(ds).get(t0['station_id'])} doing this week?", "When do jams happen most?"])


def _time_to_fix(ds, p, subj, q):
    ttf = models.time_to_fix(ds, p.start, p.end, subj.ids)
    red = M._resolved(M._in(ds.sev_inc, "start", p.start, p.end, subj.ids))
    red = red[red["severity"] == "red"]
    if red.empty:
        return Answer("", [[f"There were no completed outages for {subj.label} {N.during(p)}."]])
    paras = [[f"{p.title}, outages at {subj.label} took a median ",
              ("b", dur(red["duration_s"].median())), " to fix; ",
              f"{(red['duration_s'] <= 3600).mean():.0%} were fixed within an hour and "
              f"{(red['duration_s'] > 4 * 3600).mean():.0%} took more than four hours."]]
    if ttf:
        names_ = list(ttf.medians)
        d, n = ttf.medians[names_[0]], ttf.medians[names_[1]]
        if np.isfinite(d) and np.isfinite(n):
            sig = "a statistically significant difference" if ttf.p_value < 0.05 else "though that gap could be chance"
            paras.append([f"Timing matters: outages that start while the support desk is open are typically fixed in "
                          f"{dur(d * 3600)}, but ones that start after hours take {dur(n * 3600)} ({sig}). After-hours "
                          "outages wait for the desk to open."])
    return Answer("", paras, followups=["How many outages start after hours?",
                                        "Which station was down the longest " + p.label + "?"])


def _when(ds, p, subj, q):
    f = M.faults_in(ds, p.start, p.end, subj.ids)
    if re.search(r"\bjams?\b", q):
        f = f[f["label"].str.contains("down|jam", case=False)]
    if len(f) < 3:
        return Answer("", [["Not enough faults in that period to see a pattern."]])
    counts = f["hour"].value_counts().reindex(range(24), fill_value=0).to_numpy()
    win = np.array([counts[[h, (h + 1) % 24, (h + 2) % 24]].sum() for h in range(24)])
    h0 = int(win.argmax())
    days = f["weekday"].value_counts()
    dname = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][int(days.index[0])]
    what = "jams and printer-down faults" if re.search(r"\bjams?\b", q) else "faults"
    return Answer("", [[f"Of {len(f)} {what} {N.during(p)}, ", ("b", f"{win[h0] / len(f):.0%}"),
                        f" started between {hour(h0)} and {hour((h0 + 3) % 24)}. The busiest day was {dname} "
                        f"({days.iloc[0]} faults). Busy printing hours drive this: more pages, more chances to jam "
                        "or run out of paper."]],
                  followups=["How many outages start after hours?", "Which station has the most faults?"])


def _faults(ds, p, subj, q):
    f = M.faults_in(ds, p.start, p.end, subj.ids)
    if f.empty:
        return Answer("", [[f"No faults at {subj.label} {N.during(p)}."]], tone="good")
    top = f["label"].value_counts()
    rest = ", ".join(f"{lab.lower()} ({n})" for lab, n in top.iloc[1:4].items())
    by_st = f.groupby("station_id").size().sort_values(ascending=False)
    para = [f"{p.title}, there were {plural(len(f), 'fault')}. The most common was ",
            ("b", top.index[0].lower()), f" ({top.iloc[0]})" + (f", then {rest}" if rest else "") + ". "]
    if subj.ids is None or len(subj.ids) > 1:
        para += ["The station with the most was ", _link(ds, by_st.index[0]), f" ({by_st.iloc[0]})."]
    tbl = top.rename("n").reset_index().rename(columns={"label": "fault"})
    return Answer("", [para], table=tbl, table_cols=[("fault", "Fault"), ("n", "Times")],
                  followups=["When do jams happen most?", "Which station was least reliable " + p.label + "?"])


def _paper(ds, p, subj, q):
    t = M._in(ds.tray_inc, "start", p.start, p.end, subj.ids)
    outs = M._in(ds.fault_inc, "start", p.start, p.end, subj.ids)
    outs = outs[outs["code"] == "paper_out"]
    if t.empty and outs.empty:
        return Answer("", [[f"No paper trays ran empty at {subj.label.replace('the printers at ', '').replace('the printer at ', '')} {N.during(p)}."]], tone="good")
    by_b = (t.merge(ds.stations[["station_id", "building"]], on="station_id")
            .groupby("building")["duration_s"].agg(["size", "sum"]).sort_values("sum", ascending=False))
    para = [f"{p.title}, {plural(len(t), 'tray')} ran empty and stations were fully out of paper "
            f"{plural(len(outs), 'time')}. "]
    if len(by_b):
        para += ["The building with the most empty-tray time was ", ("b", by_b.index[0]),
                 f" ({dur(by_b['sum'].iloc[0])} across {by_b['size'].iloc[0]} empties): a candidate for a bigger "
                 "refill or an extra round."]
    tbl = by_b.head(6).reset_index().assign(empty=lambda d: d["sum"].map(dur))
    return Answer("", [para], table=tbl, table_cols=[("building", "Building"), ("size", "Times empty"),
                                                     ("empty", "Total time empty")],
                  followups=["When do problems happen most?", "What's down right now?"])


def _consumables(ds, p, subj, q):
    comp = next((c for w, c in COMPONENT_WORDS.items() if re.search(rf"\b{w}s?\b", q)), None)
    burn = M.burn_rates(ds, p.start, p.end, subj.ids).set_index("component")
    if comp:
        rows = burn[burn.index.str.startswith(comp)]
    else:
        rows = burn
    used = rows["used_units"].sum()
    para = [f"{p.title}, {subj.label} used about ", ("b", f"{used:.1f}"),
            f" {comp + ' ' if comp else ''}parts' worth" + (" (toner, drums, belts and fusers combined)" if not comp else "")]
    if comp == "toner":
        k = burn.loc["toner_k", "used_units"]
        para.append(f", of which {k:.1f} were black")
    para.append(". ")
    repl = M.replacements(ds, p.start, p.end, subj.ids)
    if comp:
        repl = repl[repl["component"].str.startswith(comp)]
    para.append(f"{plural(len(repl), 'replacement')} {'was' if len(repl) == 1 else 'were'} detected.")
    tbl = rows.reset_index()[["label", "used_units", "per_month"]].assign(per_month=lambda d: d["per_month"] / 100)
    return Answer("", [para], table=tbl, table_cols=[("label", "Part"), ("used_units", "Parts used"),
                                                     ("per_month", "Per month at this rate")],
                  followups=["What toner will run out next?", "Compare this month to last month"])


def _forecast(ds, p, subj, q):
    eol = models.eol_forecast(ds, subj.ids)
    comp = next((c for w, c in COMPONENT_WORDS.items() if re.search(rf"\b{w}s?\b", q)), None)
    if comp:
        eol = eol[eol["component"].str.startswith(comp)]
    eol = eol[eol["days"].notna()].sort_values("days")
    if eol.empty:
        return Answer("", [["No forecast is possible yet; parts need a few days of readings."]])
    first = eol.iloc[0]
    when_txt = "now (already at its replacement point)" if first["days"] == 0 else \
        f"in about {durations.days(first['days'])}"
    soon = eol[eol["days"] <= 7]
    para = ["Next to reach end of life: ", _link(ds, first["station_id"]), f"'s {first['label']}, {when_txt}. ",
            f"{plural(len(soon), 'part')} {'is' if len(soon) == 1 else 'are'} due within a week. Forecasts come from a "
            "regression on each part's readings since its last replacement."]
    from .dashboard.views.analytics import eol_window   # one formatter for windows everywhere
    tbl = eol.head(8).assign(window=lambda d: d.apply(eol_window, axis=1))
    return Answer("", [para], table=tbl, table_cols=[("station", "Station"), ("label", "Part"),
                                                     ("window", "End of life in")],
                  followups=["How much toner did we use this month?", "What's down right now?"])


def _compare(ds, p, subj, q):
    keys = [k for k, _ in N.PERIOD_KEYS if k.replace("_", " ") in q]
    if len(keys) >= 2:
        a, b = N.period(ds, keys[0]), N.period(ds, keys[1])
    else:
        a = p if p.key != "custom" or "last 30" not in p.label else N.period(ds, "this_week")
        b = N.custom_period(ds, a.prev_start, a.prev_end, a.prev_label)
    rows = []
    for per in (a, b):
        av = M.availability(ds, per.start, per.end, subj.ids)
        inc = M._in(ds.sev_inc, "start", per.start, per.end, subj.ids)
        red = inc[inc["severity"] == "red"]
        res = M._resolved(red)
        rows.append({"period": per.label, "avail": av.value, "outages": len(red),
                     "fix": res["duration_s"].median() if len(res) else np.nan,
                     "days": max((per.end - per.start).total_seconds() / 86400, 1e-9)})
    x, y = rows
    seg = [f"{x['period'][:1].upper() + x['period'][1:]}: "]
    if x["avail"] is not None and y["avail"] is not None:
        d = x["avail"] - y["avail"]
        seg += [("b", f"{x['avail']:.1f}%"), f" available vs {y['avail']:.1f}% ({'better' if d >= 0 else 'worse'} by "
                                             f"{abs(d):.1f} points). "]
    rate_x, rate_y = x["outages"] / x["days"], y["outages"] / y["days"]
    seg.append(f"Outages ran at {rate_x:.1f} a day vs {rate_y:.1f}")
    if np.isfinite(x["fix"]) and np.isfinite(y["fix"]):
        seg.append(f", and took a median {dur(x['fix'])} to fix vs {dur(y['fix'])}")
    seg.append(".")
    tbl = pd.DataFrame([{"period": r["period"], "avail": f"{r['avail']:.1f}%" if r["avail"] is not None else "—",
                         "outages": r["outages"], "fix": dur(r["fix"]) if np.isfinite(r["fix"]) else "—"} for r in rows])
    return Answer("", [seg], table=tbl, table_cols=[("period", "Period"), ("avail", "Available"),
                                                    ("outages", "Outages"), ("fix", "Median fix time")],
                  followups=["What went wrong last week?", "Which station was least reliable this week?"])


def _support(ds, p, subj, q):
    st = ds.stations if subj.ids is None else ds.stations[ds.stations["station_id"].isin(subj.ids)]
    owners = [o for o in support.TEAMS if (st["owner"] == o).any()]
    paras = []
    # "Who supports X?": ownership first.
    ask_open = bool(re.search(r"\b(open|closed|staffed right now|hours)\b", q)) and not re.search(r"\bafter[- ]hours\b", q)
    if ask_open and not re.search(r"\b(who|owner|owns|responsible|supports)\b", q):
        for o in owners:
            open_, now_txt = support.desk_status(o, ds.as_of)
            t = support.team(o)
            paras.append([("b", f"{o} ({t['base']}) is {now_txt[0].lower() + now_txt[1:]}."),
                          f" Its desk is staffed {support.hours_text(o)}."])
    elif re.search(r"\b(who|owner|owns|responsible|supports|handles|looks after)\b", q) and subj.ids is not None:
        for o in owners:
            t = support.team(o)
            _, now_txt = support.desk_status(o, ds.as_of)
            paras.append([subj.label[:1].upper() + subj.label[1:], " ", "is" if subj.singular else "are",
                          " supported by ", ("b", o), f", based at {t['base']}. The desk is staffed "
                          f"{support.hours_text(o)}; right now it's {now_txt[0].lower() + now_txt[1:]}."])
    red = M._in(ds.sev_inc, "start", p.start, p.end, subj.ids)
    red = red[red["severity"] == "red"]
    if red.empty:
        paras.append([f"There were no outages for {subj.label} {N.during(p)}."])
        return Answer("", paras, tone="good")
    summ = support.summary(red)
    weekend_q = bool(re.search(r"\bweekends?\b", q))
    if weekend_q:
        wk = red["start"].dt.tz_convert(config.LOCAL_TZ).dt.dayofweek >= 5
        res = M._resolved(red)
        rw = res["start"].dt.tz_convert(config.LOCAL_TZ).dt.dayofweek >= 5
        line = [("b", f"{wk.sum()} of {len(red)}"), f" outages ({wk.mean():.0%}) started on a weekend, when neither desk "
                "is staffed"]
        if rw.sum() >= 2 and (~rw).sum() >= 2:
            line += ["; they lasted a median ", ("b", dur(res.loc[rw, "duration_s"].median())),
                     f" vs {dur(res.loc[~rw, 'duration_s'].median())} for ones that started on a weekday"]
        paras.append(line + ["."])
    for r in summ.itertuples(index=False):
        seg = [("b", r.owner), f" ({r.hours}): ", ("b", f"{r.after_hours} of {r.outages}"),
               f" outages ({r.after_share:.0%}) began after hours"]
        if np.isfinite(r.median_wait_s):
            seg.append(f" and waited a median {dur(r.median_wait_s)} for the desk to open")
        tot = r.staffed_h + r.after_h
        if tot > 0:
            seg.append(f". Of {durations.hours(tot)} of printer downtime, {r.after_h / tot:.0%} fell outside desk hours")
        if np.isfinite(r.median_desk_fix_s):
            seg.append(f"; once staff were in, the typical fix took {dur(r.median_desk_fix_s)} of desk time")
        paras.append(seg + ["."])
    paras.append(["The time an outage waits for the desk to open is a staffing question, not a repair one: shorter "
                  "waits come from coverage (an evening or weekend check), not faster fixes."])
    tbl = summ.assign(after=summ["after_share"].map(lambda v: f"{v:.0%}"),
                      wait=summ["median_wait_s"].map(lambda v: dur(v) if np.isfinite(v) else "—"),
                      desk=summ["median_desk_fix_s"].map(lambda v: dur(v) if np.isfinite(v) else "—"))
    return Answer("", paras, table=tbl, table_cols=[("owner", "Owner"), ("hours", "Desk hours"),
                                                     ("outages", "Outages"), ("after", "Began after hours"),
                                                     ("wait", "Median wait for desk"), ("desk", "Desk time to fix")],
                  followups=["Do weekend outages last longer?", "When do problems happen most?",
                             "How long do outages take to fix?"])


def _calendar(ds, p, subj, q):
    c = campus.load()
    if c.empty:
        return Answer("", [["The academic calendar hasn't been loaded yet. Run: python -m wepa_monitor campus"]])
    today = ds.as_of.tz_convert(TZ).date()
    paras, table, cols = [], None, []
    if re.search(r"\blibrary\b", q):
        rows = []
        for i in range(7):
            d = today + pd.Timedelta(days=i)
            r = c.on(d)
            if r is None or not r.get("library_known", False):
                continue
            o, cl = r.get("library_open"), r.get("library_close")
            rows.append({"day": f"{pd.Timestamp(d):%a %b %-d}",
                         "hours": "Closed" if pd.isna(o) else f"{support._clock(o)}–{support._clock(float(cl) % 24)}"})
        if not rows:
            return Answer("", [["Library hours for this week haven't been published yet."]])
        paras.append(["Maxwell Library is ", ("b", rows[0]["hours"].lower() if rows[0]["hours"] == "Closed"
                                               else f"open {rows[0]['hours']}"), " today. The three library printers "
                      "can only be used while it's open, so outages after closing don't strand anyone."])
        return Answer("", paras, table=pd.DataFrame(rows), table_cols=[("day", "Day"), ("hours", "Hours")],
                      followups=["What's coming up on the calendar?", "How is Maxwell doing this week?"])
    want = next((k for k, pat in [("finals_start", r"finals?|exam"), ("break_start", r"spring break"),
                                  ("thanksgiving_start", r"thanksgiving"), ("move_in", r"move[- ]?in"),
                                  ("commencement", r"commencement|graduation"), ("classes_begin", r"classes|semester"),
                                  ("holiday", r"holiday|day off|no classes"),
                                  ("halls_close", r"halls? close|winter break")] if re.search(pat, q)), None)
    future = c.events[c.events["date"] >= today]
    if want:
        hit = future[future["kind"] == want].head(1)
        if hit.empty:
            return Answer("", [["That isn't on the published calendar yet."]])
        e = hit.iloc[0]
        days = (e["date"] - today).days
        extra = ""
        if want == "finals_start":
            end = future[(future["kind"] == "finals_end") & (future["date"] >= e["date"])].head(1)
            extra = f", running through {end.iloc[0]['date']:%a %b %-d}" if len(end) else ""
        paras.append([("b", e["event"].split("–")[0].strip().rstrip(".")), f": {e['date']:%A, %B %-d, %Y}{extra} "
                      f"(in {plural(days, 'day')})."])
        if want in ("finals_start", "move_in", "classes_begin"):
            eol = models.eol_forecast(ds, subj.ids)
            during = eol[eol["days"].notna() & (eol["days"] >= days - 3) & (eol["days"] <= days + 5)]
            if len(during):
                paras.append([("b", plural(len(during), "part")), f" {'is' if len(during) == 1 else 'are'} projected "
                              "to reach end of life right around then. Swap them a few days early so they don't fail "
                              "at the busiest time."])
            else:
                paras.append(["No parts are projected to run out right around then (at current usage)."])
    else:
        up = c.upcoming(today, days=60)
        up = up[~up["kind"].isin(["halls_open"])].head(6)
        paras.append(["Here's what's coming up on BSU's academic calendar. Holidays close both support desks; "
                      "finals and move-in are the busiest printing of the term."])
        table = up.assign(when=up["date"].map(lambda d: f"{pd.Timestamp(d):%a %b %-d}"),
                          what=up["event"].str.replace(r"\s*\(.*\)$", "", regex=True))
        cols = [("when", "Date"), ("what", "Event")]
    return Answer("", paras, table=table, table_cols=cols,
                  followups=["How did the last finals go?", "Is the library open?", "When are finals?"])


def _usage(ds, p, subj, q):
    """Which printers are used most and least: toner burned per day, a stand-in for pages."""
    u = M.usage_by_station(ds, p.start, p.end, subj.ids)
    u = u[u["usage_per_day"].notna()]
    if u.empty:
        return Answer("", [["Not enough history yet: usage needs at least "
                            f"{config.MIN_DAYS_FOR_BURN_RATE} days of toner readings per printer."]])
    top, low = u.head(3), u.tail(3).iloc[::-1]
    para = ["Busiest: "]
    for i, r in enumerate(top.itertuples()):
        para += [("st", r.station_id, r.description), f" ({r.relative:.1f}× the typical printer)",
                 ", " if i < len(top) - 2 else (" and " if i == len(top) - 2 else ". ")]
    para += ["Quietest: "]
    for i, r in enumerate(low.itertuples()):
        para += [("st", r.station_id, r.description), f" ({r.relative:.1f}×)",
                 ", " if i < len(low) - 2 else (" and " if i == len(low) - 2 else ". ")]
    paras = [para]
    lone_busy = top[top["printers_in_building"] == 1]
    if len(lone_busy):
        r = lone_busy.iloc[0]
        paras.append([f"{r['building']} is among the busiest and has only one printer: a second printer there "
                      "would cut both the queue and the impact of an outage."])
    quiet_pairs = low[low["printers_in_building"] > 1]
    if len(quiet_pairs):
        r = quiet_pairs.iloc[0]
        paras.append([f"{r['description']} is one of the quietest and shares {r['building']} with another "
                      "printer: a candidate to move somewhere busier."])
    paras.append(["Usage here is black toner burned per day (Wepa doesn't publish page counts), so treat it as a "
                  "relative measure: good for ranking, not for exact page totals."])
    tbl = u.assign(rel=u["relative"].map(lambda v: f"{v:.1f}×"),
                   month=u["cartridges_per_month"].map(lambda v: f"{v:.2f}"))
    return Answer("", paras, table=tbl, table_cols=[("label", "Station"), ("rel", "vs typical"),
                                                     ("month", "Black cartridges / month")],
                  followups=["Which station was down the longest last week?", "What toner will run out next?"])
