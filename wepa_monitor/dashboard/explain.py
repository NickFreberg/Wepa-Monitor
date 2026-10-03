"""'What does this point mean?': plain-language explanations for clicked chart points.

Each explainer receives the dataset, the clicked point (Plotly clickData) and the
page context, and returns an Explanation: one plain sentence, then what it tells
you, why it matters, and where to look next. Every number is computed from the
data around the point; nothing is canned except the glossary of what each fault
means physically.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import campus, config, metrics as M, models, narrative as N, support

TZ = config.LOCAL_TZ

# What each fault means on the floor, and what fixes it (from Wepa's alert legend + ResNet practice).
GLOSSARY = {
    "Paper out": ("Every paper tray is empty, so the station can't print at all.", "Refill both trays."),
    "Printer down": ("The printer has stopped. Usually a paper jam or a hardware fault; the printer's own display "
                     "says where.", "Clear the jam at the spot the display shows, then check the queue resumes."),
    "Tray missing": ("A paper tray isn't seated, often right after someone refilled it.",
                     "Push the tray in until it clicks."),
    "Not reachable": ("The kiosk has lost contact with its printer or the network.",
                      "Check the network cable, then restart the print station."),
    "Fatal error": ("The printer hit an internal error.", "Power-cycle the printer only, not the whole station."),
    "Service call required": ("The printer is asking for service.", "Power-cycle the printer only; if it persists, "
                              "call Wepa support."),
    "Incorrect paper tray size": ("A tray's size dial isn't set to Letter, so it's refusing jobs from that tray.",
                                  "Turn the blue dial on the front of the tray to Letter."),
    "Paper low": ("A tray is running low but the station can still print.", "Top it up on the next round."),
    "Paper jam": ("Paper is stuck inside the printer.", "Clear it at the location shown on the display."),
    "Top cover open": ("The printer's top cover isn't closed.", "Open it and close it firmly until it snaps."),
    "Toner sensor error": ("The printer can't read a toner cartridge.", "Reseat the toner(s), then power-cycle the "
                           "printer."),
    "Toner critical": ("A toner is empty or nearly so.", "Replace the toner shown on the status page."),
}


@dataclass
class Explanation:
    eyebrow: str                       # chart name
    title: str                         # the plain-language sentence
    tells: list = field(default_factory=list)      # segments (see narrative.py)
    matters: list = field(default_factory=list)
    next: list[tuple[str, str]] = field(default_factory=list)   # (label, href)


@dataclass
class Context:
    ids: list | None
    start: pd.Timestamp
    end: pd.Timestamp
    period_label: str
    station_id: str | None = None


def _local_day(x) -> pd.Timestamp:
    return pd.Timestamp(str(x)[:10])


def _day_bounds(day: pd.Timestamp):
    s = day.tz_localize(TZ).tz_convert("UTC")
    return s, s + pd.Timedelta(days=1)


def _station(ds, sid):
    return N.names(ds).get(sid, sid)


def _cd(point, i, default=None):
    cd = point.get("customdata")
    if cd is None:
        return default
    if isinstance(cd, list):
        return cd[i] if i < len(cd) else default
    return cd if i == 0 else default


# --- availability ---------------------------------------------------------------------------------

def avail_daily(ds, point, ctx: Context) -> Explanation:
    day = _local_day(point["x"])
    series = _cd(point, 1, "All BSU stations")
    ids = ctx.ids
    if series not in ("All BSU stations", None):
        sec_ids = ds.ids(section=series)
        ids = sec_ids if ids is None else [i for i in ids if i in sec_ids]
    s, e = _day_bounds(day)
    a = M.availability(ds, s, e, ids)
    avg = M.availability(ds, ctx.start, ctx.end, ids)
    who = "all BSU print stations" if series == "All BSU stations" else f"{series} stations"
    title = f"On {day:%A, %B %-d}, {who} could print {a.value:.1f}% of the time." if a.value is not None else \
        f"No data for {day:%B %-d}."
    tells, nxt = [], []
    if a.value is not None and avg.value is not None:
        d = a.value - avg.value
        tells = [f"That's {abs(d):.1f} points {'above' if d >= 0 else 'below'} the {ctx.period_label} average of "
                 f"{avg.value:.1f}%, about ", ("b", f"{a.extra['down_h']:.0f} printer-hours"), " lost that day. "]
        h = ds.hourly[(ds.hourly["local_date"] == day)]
        if ids is not None:
            h = h[h["station_id"].isin(ids)]
        lost = (h["covered_s"] - h["up_s"]).groupby(h["station_id"]).sum().sort_values(ascending=False)
        lost = lost[lost > 600]
        if len(lost):
            tells += ["Most of it came from "]
            for i, (sid, secs) in enumerate(lost.head(3).items()):
                tells += [", " if i else "", ("st", sid, _station(ds, sid)), f" ({N.dur(secs)})"]
            tells.append(".")
            nxt = [(f"Open {_station(ds, sid)}", f"/station/{sid}") for sid in lost.head(2).index]
        if day.dayofweek >= 5:
            tells.append(" It was a weekend, when neither support desk is staffed.")
    cal = campus.load().on(day.date())
    if cal is not None:
        what = cal["event"].split(";")[0] if cal["event"] else campus.PHASES.get(cal["phase"], "")
        tells += [" On the academic calendar: ", ("b", what or cal["label"]),
                  "" if cal["halls_open"] else " (residence halls closed)", "."]
    matters = ["Each lost printer-hour is time students at that building had to find another printer. A dip "
               "concentrated in one station points to a fault; a dip spread across many suggests a staffing or "
               "supply issue that day."]
    return Explanation("Daily availability", title, tells, matters, nxt)


def building_cov(ds, point, ctx: Context) -> Explanation:
    building = point.get("y") or point.get("label")
    b = M.building_availability(ds, ctx.start, ctx.end, ctx.ids)
    row = b[b["building"] == building]
    if row.empty:
        return Explanation("Building coverage", str(building))
    r = row.iloc[0]
    n = int(r["stations"] or 1)
    title = f"Students in {building} could find a working printer {r['any_up']:.1f}% of the time."
    tells = [f"{building} has {N.plural(n, 'print station')}. "]
    if n > 1:
        tells += [f"All of them were up {r['all_up']:.1f}% of the time, so a second printer covered the gap for "
                  f"about {(r['any_up'] - r['all_up']) / 100 * (ctx.end - ctx.start).total_seconds() / 3600:.0f} hours "
                  f"over the {ctx.period_label}."]
    else:
        tells += [f"With only one printer, every outage leaves the building with no printing; it was unavailable for "
                  f"about {(100 - r['any_up']) / 100 * (ctx.end - ctx.start).total_seconds() / 3600:.0f} hours."]
    matters = ["Coverage is what students actually experience. Single-printer buildings with low coverage are the "
               "strongest case for a second printer or faster response."]
    return Explanation("Building coverage", title, tells, matters, [(f"Stations in {building}", f"/stations?q={building}")])


# --- faults -----------------------------------------------------------------------------------------

def fault_types(ds, point, ctx: Context) -> Explanation:
    label = point.get("y") or point.get("label")
    f = M.faults_in(ds, ctx.start, ctx.end, ctx.ids)
    sub = f[f["label"] == label]
    meaning, fix = GLOSSARY.get(label, ("A fault reported by the printer.", "Check the printer's display."))
    title = f"{label}: {len(sub)} times in the {ctx.period_label}, {len(sub) / max(len(f), 1):.0%} of all faults."
    tells = [meaning + " "]
    if len(sub):
        top = sub.groupby(["station_id"]).size().sort_values(ascending=False)
        tells += ["It happened most at ", ("st", top.index[0], _station(ds, top.index[0])), f" ({top.iloc[0]} times)"]
        res = M._resolved(sub)
        if len(res) >= 3:
            tells.append(f", and typically took {N.dur(res['duration_s'].median())} to clear")
        tells.append(". ")
        counts = sub["hour"].value_counts()
        tells.append(f"The most common hour was {N.hour(int(counts.index[0]))}.")
    matters = [("b", "The fix: "), fix]
    return Explanation("Fault types", title, tells, matters,
                       [(f"See {label.lower()} events", f"/activity?q={label}")] +
                       ([(f"Open {_station(ds, top.index[0])}", f"/station/{top.index[0]}")] if len(sub) else []))


def faults_building(ds, point, ctx: Context) -> Explanation:
    building = point.get("y") or point.get("label")
    f = M.faults_in(ds, ctx.start, ctx.end, ctx.ids)
    sub = f[f["building"] == building]
    top = sub["label"].value_counts()
    title = f"{building} had {N.plural(len(sub), 'fault incident')} in the {ctx.period_label}."
    tells = []
    if len(top):
        tells = ["Mostly ", ("b", top.index[0].lower()), f" ({top.iloc[0]})"]
        if len(top) > 1:
            tells.append(f", then {top.index[1].lower()} ({top.iloc[1]})")
        tells.append(". " + GLOSSARY.get(top.index[0], ("", ""))[0])
    matters = ["A building that keeps showing the same fault usually has one root cause worth fixing once, such as a "
               "small tray, a worn feed roller or a flaky network drop, rather than being handled on every round."]
    return Explanation("Faults by building", title, tells, matters, [(f"Stations in {building}", f"/stations?q={building}")])


def heatmap(ds, point, ctx: Context) -> Explanation:
    hour_lbl, day = point.get("x"), point.get("y")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd = days.index(day) if day in days else 0
    hours = [f"{(h % 12) or 12}{'a' if h < 12 else 'p'}" for h in range(24)]
    h = hours.index(hour_lbl) if hour_lbl in hours else 0
    f = M.faults_in(ds, ctx.start, ctx.end, ctx.ids)
    cell = f[(f["weekday"] == wd) & (f["hour"] == h)]
    avg = len(f) / (7 * 24)
    full = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"][wd]
    title = f"{N.plural(len(cell), 'fault')} started on {full} between {N.hour(h)} and {N.hour((h + 1) % 24)}."
    ratio = len(cell) / avg if avg else 0
    tells = [f"A typical hour sees {avg:.1f}, so this hour is ", ("b", f"{ratio:.1f}×"), " the average. "]
    if len(cell):
        tells.append(f"Most were {cell['label'].value_counts().index[0].lower()}.")
    st = ds.stations if ctx.ids is None else ds.stations[ds.stations["station_id"].isin(ctx.ids)]
    owners = [o for o in support.TEAMS if (st["owner"] == o).any()]
    open_ = [o for o in owners if support.grid(o)[wd, h] > 0]
    closed = [o for o in owners if o not in open_]
    if not closed:
        cover = ("This hour is inside desk hours for " + " and ".join(open_) +
                 ", so faults should be caught quickly.")
    elif not open_:
        cover = ("No support desk is staffed at this hour (" + " or ".join(closed) +
                 "), so anything that breaks now waits for the next shift.")
    else:
        cover = (f"{' and '.join(open_)} {'is' if len(open_) == 1 else 'are'} staffed at this hour, but "
                 f"{' and '.join(closed)} {'is' if len(closed) == 1 else 'are'} not, so their stations wait.")
    matters = ["Hot spots line up with busy printing times. " + cover,
               " The outlined squares on the chart are each team's desk hours."]
    return Explanation("When faults start", title, tells, matters)


# --- consumables -------------------------------------------------------------------------------------

def cumulative(ds, point, ctx: Context) -> Explanation:
    comp = _cd(point, 0, "toner_k")
    day = _local_day(point["x"])
    used = float(point["y"])
    label = config.COMPONENT_LABELS.get(comp, comp)
    days_in = max(1, (day - ctx.start.tz_convert(TZ).tz_localize(None).normalize()).days + 1)
    pace = used / days_in
    title = f"By {day:%B %-d}, the stations had used {used:.1f} {label} parts' worth since the period began."
    tells = [f"That's about {pace:.2f} a day, or ", ("b", f"{pace * 30.44:.1f} a month"), " at this pace. "
             "Use is counted from level drops only, so a replacement never counts as negative use."]
    matters = ["This is the ordering signal: at this pace, how many cartridges or drums to keep on the shelf. A "
               "steeper stretch usually means a busy time of the semester, such as finals."]
    return Explanation("Cumulative use", title, tells, matters, [("Consumables tab", "/analytics?tab=consumables")])


# --- station page ----------------------------------------------------------------------------------------

def timeline(ds, point, ctx: Context) -> Explanation:
    state = _cd(point, 3, "green")
    start, end = pd.Timestamp(_cd(point, 4)), pd.Timestamp(_cd(point, 5))
    mins = float(_cd(point, 2, 0))
    sid = ctx.station_id
    label = {"red": "down", "yellow": "showing a warning", "green": "printing normally",
             "nodata": "not reporting"}.get(state, state)
    title = f"From {_cd(point, 0)} to {_cd(point, 1)}, this station was {label} ({N.dur(mins * 60)})."
    tells, matters = [], []
    if state in ("red", "yellow") and sid:
        f = ds.fault_inc[(ds.fault_inc["station_id"] == sid) & (ds.fault_inc["start"] < end) &
                         (ds.fault_inc["end"].fillna(ds.as_of) > start)]
        causes = sorted(set(f["label"]))
        if causes:
            tells = ["Cause: ", ("b", ", ".join(c.lower() for c in causes)), ". " + GLOSSARY.get(causes[0], ("", ""))[0]]
            matters = [("b", "The fix: "), GLOSSARY.get(causes[0], ("", "Check the printer's display."))[1]]
        row = ds.stations[ds.stations["station_id"] == sid]
        own = row["owner"].iloc[0] if len(row) else config.DEFAULT_OWNER
        if state == "red":
            if support.is_open(own, start):
                tells.append(f" It started while {own}'s desk was open.")
            else:
                nxt = support.next_open(own, start)
                wait = (nxt - start).total_seconds() if nxt is not None else 0
                tells.append(f" It started after {own}'s desk hours, so it waited about {N.dur(wait)} for the desk "
                             "to open" + (f"; {N.dur(support.staffed_seconds(own, start, end))} of it fell inside "
                                          "desk hours." if end > start else "."))
    elif state == "nodata":
        tells = ["The monitor didn't receive snapshots for this station, so its status is unknown for this stretch."]
        matters = ["Unobserved time is left out of availability rather than guessed."]
    else:
        tells = ["No alerts during this stretch."]
    return Explanation("Status timeline", title, tells, matters)


def levels(ds, point, ctx: Context) -> Explanation:
    comp = _cd(point, 0, "toner_k")
    before = _cd(point, 1, "")
    label = config.COMPONENT_LABELS.get(comp, comp)
    when = pd.Timestamp(point["x"])
    if before not in ("", None):
        title = f"{label} was replaced around {when:%b %-d, %-I:%M %p}, with {float(before):.0f}% left in the old one."
        tells = ["A jump of 15+ points is counted as a replacement. "
                 + (f"Swapping at {float(before):.0f}% threw away a little usable {label.split()[0].lower()}."
                    if float(before) > config.CONSUMABLE_REPLACE_PCT else "That's right at the replacement point.")]
        matters = ["Replacing parts near their end, not early, saves money; replacing them before they hit 0 avoids "
                   "an outage. The sweet spot is about 3-5%."]
    else:
        title = f"{label} read {float(point['y']):.0f}% on {when:%b %-d at %-I:%M %p}."
        tells = ["Each step down is part of the part being used up; the slope is how fast this station prints."]
        matters = ["The regression forecast on this page projects this line forward to its replacement point."]
    return Explanation("Consumable level", title, tells, matters)


def hour_bars(ds, point, ctx: Context) -> Explanation:
    hours = [f"{(h % 12) or 12}{'a' if h < 12 else 'p'}" for h in range(24)]
    h = hours.index(point.get("x")) if point.get("x") in hours else 0
    n = int(point.get("y") or 0)
    title = f"{N.plural(n, 'fault')} at this station started between {N.hour(h)} and {N.hour((h + 1) % 24)}."
    matters = ["If one hour stands out, schedule a check just before it, for example a paper top-up ahead of the "
               "afternoon rush."]
    return Explanation("When it goes wrong", title, [], matters)


# --- statistics --------------------------------------------------------------------------------------------

def km(ds, point, ctx: Context) -> Explanation:
    group = _cd(point, 0, "")
    hrs, still = float(point["x"]), float(point["y"])
    title = f"After {hrs:.1f} hours, {still:.0f}% of outages that {group.lower().replace('started', 'started')} were still not fixed."
    tells = [f"Read it as: {100 - still:.0f}% were fixed within {hrs:.1f} hours. The steeper the early drop, the faster "
             "the response."]
    matters = ["The gap between the two curves is the cost of outages starting when no support desk is staffed "
               "(ResNet: " + support.hours_text("ResNet") + "; IT Service Center: " +
               support.hours_text("IT Service Center") + "), measured in hours students can't print."]
    return Explanation("How long outages last", title, tells, matters)


def cchart(ds, point, ctx: Context) -> Explanation:
    day = _local_day(point["x"])
    n = int(point["y"])
    cc = models.fault_control_chart(ds, ctx.start, ctx.end, ctx.ids)
    s, e = _day_bounds(day)
    f = M.faults_in(ds, s, e, ctx.ids)
    title = f"{N.plural(n, 'fault')} on {day:%A, %B %-d}."
    if cc:
        status = ("above the upper limit: a real change, worth investigating" if n > cc.ucl else
                  "within the normal range: ordinary day-to-day variation")
        tells = [f"The average day has {cc.center:.1f}; this one was ", ("b", status), ". "]
    else:
        tells = []
    if len(f):
        top = f["label"].value_counts()
        tells.append(f"Mostly {top.index[0].lower()} ({top.iloc[0]}).")
    matters = ["Control limits separate signal from noise: react to points outside them, not to every up-and-down."]
    return Explanation("Fault control chart", title, tells, matters,
                       [("That day's activity", "/activity")])


def usage(ds, point, ctx: Context) -> Explanation:
    label, sid = _cd(point, 0, ""), _cd(point, 1, "")
    uf = models.usage_vs_reliability(ds, ctx.start, ctx.end, ctx.ids)
    row = uf.points[uf.points["station_id"] == sid].iloc[0] if uf is not None and sid in set(uf.points["station_id"]) else None
    title = f"{label}: {float(point['x']):.2f} pts of black toner a day, {float(point['y']):.1f} outages a week."
    tells, matters = [], []
    if row is not None:
        diff = row["resid"]
        tells = [f"For a station this busy, the trend line predicts {row['fitted']:.1f} outages a week, so it's ",
                 ("b", f"{abs(diff):.1f} {'more' if diff > 0 else 'fewer'}"), " than expected."]
        matters = ["Stations well above the line fail more than their workload explains, which points to the machine "
                   "or its setup (for example a small paper tray) rather than heavy use." if diff > 0 else
                   "Stations at or below the line are holding up well for how much they're used."]
    return Explanation("Usage vs reliability", title, tells, matters, [(f"Open {label}", f"/station/{sid}")] if sid else [])


def eol(ds, point, ctx: Context) -> Explanation:
    title = "This line is the projected path of the part's level."
    tells = ["Dots are the part's readings since it was last replaced. The solid line is the best-fit trend (Theil-Sen "
             "regression, which isn't thrown off by a few odd readings); the dotted part extends it to the replacement "
             "point."]
    matters = ["The diamond is when to have a replacement in hand. Ordering then avoids both early swaps (waste) and "
               "running out (an outage)."]
    return Explanation("End-of-life projection", title, tells, matters)


def monthly_avail(ds, point, ctx: Context) -> Explanation:
    label = point.get("x")
    title = f"In {label}, BSU print stations could print {float(point['y']):.1f}% of the time."
    tells = ["Gray bars are partial months, so they aren't directly comparable with full ones."]
    matters = ["Month-to-month changes follow the academic calendar: quiet in summer and breaks, busiest around "
               "move-in and finals."]
    return Explanation("Availability by month", title, tells, matters, [("Executive summary", "/executive")])


def monthly_toner(ds, point, ctx: Context) -> Explanation:
    comp = _cd(point, 0, "toner_k")
    label = config.COMPONENT_LABELS.get(comp, comp)
    title = f"{point.get('x')}: {float(point['y']):.1f} {label} cartridges' worth used."
    matters = ["This is the actual demand to budget against; compare it with what was ordered."]
    return Explanation("Toner used by month", title, [], matters)


def owner_hours(ds, point, ctx: Context) -> Explanation:
    owner = _cd(point, 0, config.DEFAULT_OWNER)
    part = _cd(point, 1, "after")
    t = support.team(owner)
    inc = M._in(ds.sev_inc, "start", ctx.start, ctx.end, ctx.ids)
    red = inc[(inc["severity"] == "red") & (inc["owner"] == owner)]
    summ = support.summary(red)
    if summ.empty:
        return Explanation("Desk hours", f"No {owner} outages {ctx.period_label}.", [], [])
    r = summ.iloc[0]
    tot = r["staffed_h"] + r["after_h"]
    if part == "after":
        title = f"{r['after_h']:,.0f} of {owner}'s {tot:,.0f} printer-hours down fell outside desk hours."
    else:
        title = f"{r['staffed_h']:,.0f} of {owner}'s {tot:,.0f} printer-hours down fell inside desk hours."
    tells = [f"{owner} works from {t['base']}, staffed {support.hours_text(owner)}. ",
             ("b", f"{r['after_hours']} of {r['outages']}"), " outages began after hours"]
    if np.isfinite(r["median_wait_s"]):
        tells.append(f" and waited a median {N.dur(r['median_wait_s'])} for someone to be in")
    tells.append(". ")
    if np.isfinite(r["median_desk_fix_s"]):
        tells.append(f"Once the desk was open, the typical fix took {N.dur(r['median_desk_fix_s'])}.")
    matters = ["Downtime inside desk hours is what staff can act on; downtime after hours is a coverage question. "
               "If most of the bar is after hours, an evening or weekend check would buy more uptime than faster "
               "repairs."]
    return Explanation("Desk hours", title, tells, matters, [("Ask about after-hours outages", "/insights")])


def phases(ds, point, ctx: Context) -> Explanation:
    phase = _cd(point, 0, "classes")
    days, avail, toner = int(_cd(point, 1, 0)), float(_cd(point, 2, 0)), float(_cd(point, 3, 0))
    label = campus.PHASES.get(phase, phase)
    title = f"During {label.lower()}, there were {float(point['x']):.1f} outages a day."
    tells = [f"That's across {N.plural(days, 'day')} {ctx.period_label}; stations were available ",
             ("b", f"{avail:.1f}%"), f" of the time and used {toner:.2f} black toners' worth a day."]
    matters = {"finals": "Finals are when a dead printer hurts most: stock paper and swap near-empty parts in "
                         "the week before.",
               "classes": "This is the baseline to compare other parts of the year against.",
               "move_in": "Move-in restarts printing after the summer; parts that sat idle all summer fail "
                          "here first.",
               }.get(phase, "Quiet stretches are the best time for maintenance and part swaps.")
    return Explanation("Academic calendar", title, tells, [matters])


EXPLAINERS = {
    "phases": phases,
    "owner_hours": owner_hours,
    "avail_daily": avail_daily, "building_cov": building_cov, "fault_types": fault_types,
    "faults_building": faults_building, "heatmap": heatmap, "cum_toner": cumulative, "cum_drum": cumulative,
    "cum_other": cumulative, "timeline": timeline, "levels_toner": levels, "levels_drum": levels,
    "levels_other": levels, "station_faults": fault_types, "station_hours": hour_bars, "km": km, "cchart": cchart,
    "usage": usage, "eol": eol, "monthly_avail": monthly_avail, "monthly_toner": monthly_toner,
}


def explain(ds, chart: str, point: dict, ctx: Context) -> Explanation | None:
    fn = EXPLAINERS.get(chart)
    if fn is None or not point:
        return None
    try:
        return fn(ds, point, ctx)
    except Exception as exc:  # noqa: BLE001 - an explanation must never break the page
        return Explanation(chart, "No explanation available for this point.", [f"({type(exc).__name__})"])
