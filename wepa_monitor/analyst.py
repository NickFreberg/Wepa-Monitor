"""The AI analyst's toolkit: read-only tools over all of the monitoring data.

The AI model is the interpreter; this module is its only window onto the data. Every tool runs the
same computations the dashboard pages use (metrics, report card, forecasts, statistics, campus
context) and returns a compact, labelled text table, so every number the model can see was computed
here, with units in the column names. Nothing here writes anything, runs model-supplied code, or
exposes anything about people: the data is printers, supplies, schedules and the monitor itself.

A Toolkit is made per request. It remembers what it returned, so ai.ask can check that each number
in the model's reply appears in what the model was actually shown (see ai.verify).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import config, metrics as M

TZ = config.LOCAL_TZ
MAX_ROWS = 40
MAX_CHARS = 7000

PERIOD_HELP = ("Time range: today, yesterday, this_week, last_week, this_month, last_month, 7d, 30d, 90d, all, "
               "a month like 2026-09, a day like 2026-09-14, or a range like 2026-09-01..2026-09-15. Default 30d.")
STATIONS_HELP = "Optional: station ID, station name, building or area (e.g. 'Weygand', '02063', 'East Campus')."


@dataclass
class Tool:
    name: str
    description: str
    params: dict                       # JSON-schema properties
    fn: Callable
    required: list = field(default_factory=list)

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": self.required}


# --- formatting ---------------------------------------------------------------------------------

def _num(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    if isinstance(v, (bool, np.bool_)):
        return "yes" if v else "no"
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    if isinstance(v, (float, np.floating)):
        v = float(v)
        return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}" if abs(v) >= 10 else f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, pd.Timestamp):
        return _when(v)
    s = str(v)
    return s if s not in ("nan", "NaT", "None") else "n/a"


def _when(ts) -> str:
    if ts is None or pd.isna(ts):
        return "n/a"
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert(TZ).strftime("%a %b %-d %Y %-I:%M %p")


def _table(df: pd.DataFrame | None, cols: dict, n: int = MAX_ROWS, title: str = "") -> str:
    """cols: {source column: header with units}. Pipe-separated, n rows max, row count stated."""
    if df is None or not len(df):
        return f"{title}\n(no rows)" if title else "(no rows)"
    cols = {c: h for c, h in cols.items() if c in df.columns}
    lines = [title] if title else []
    lines.append(" | ".join(cols.values()))
    for _, r in df.head(n).iterrows():
        lines.append(" | ".join(_num(r[c]) for c in cols))
    if len(df) > n:
        lines.append(f"... {len(df) - n} more rows not shown (narrow the question to see them)")
    return "\n".join(lines)


def _hours(seconds) -> float:
    return float(seconds) / 3600 if seconds is not None and pd.notna(seconds) else np.nan


# --- arguments -----------------------------------------------------------------------------------

def parse_period(ds: M.Dataset, spec: str | None) -> tuple[pd.Timestamp, pd.Timestamp, str]:
    from . import narrative as N
    spec = (spec or "30d").strip().lower().replace(" ", "_")
    end = ds.as_of
    first = ds.data_start if ds.data_start is not None else end - pd.Timedelta(days=365)
    if spec in dict(N.PERIOD_KEYS):
        p = N.period(ds, spec)
        return max(p.start, first), min(p.end, end), p.label
    if spec == "all":
        return first, end, "all data collected"
    m = re.fullmatch(r"(\d+)\s*d(ays?)?", spec)
    if m:
        return max(end - pd.Timedelta(days=int(m.group(1))), first), end, f"the last {int(m.group(1))} days"

    def day(s):
        return pd.Timestamp(s).tz_localize(TZ)
    try:
        if ".." in spec:
            a, b = spec.split("..", 1)
            s, e = day(a), day(b) + pd.Timedelta(days=1)
            label = f"{a} to {b}"
        elif re.fullmatch(r"\d{4}-\d{2}", spec):
            s = day(spec + "-01")
            e = s + pd.offsets.MonthBegin(1)
            label = s.strftime("%B %Y")
        else:
            s = day(spec)
            e = s + pd.Timedelta(days=1)
            label = s.strftime("%a %b %-d, %Y")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Couldn't read the period '{spec}'. {PERIOD_HELP}") from exc
    s, e = s.tz_convert("UTC"), e.tz_convert("UTC")
    return max(s, first), min(e, end), label


def resolve(ds: M.Dataset, text: str | None, base_ids=None) -> tuple[list[str] | None, str]:
    """Station IDs for a station/building/area name (within the dashboard's filter), and a label."""
    st = ds.stations if base_ids is None else ds.stations[ds.stations["station_id"].isin(base_ids)]
    if not text or not str(text).strip():
        return (None if base_ids is None else list(base_ids)), "the selected stations"
    q = str(text).strip().lower()
    exact = st[st["station_id"] == q.strip()]
    if len(exact):
        return list(exact["station_id"]), exact.iloc[0]["label"]
    for col in ("building", "area", "section", "short_name", "description", "label"):
        hit = st[st[col].fillna("").str.lower() == q]
        if len(hit):
            return list(hit["station_id"]), str(hit.iloc[0][col])
    hay = (st["label"] + " " + st["building"].fillna("") + " " + st["area"].fillna("")).str.lower()
    hit = st[hay.str.contains(re.escape(q), regex=True)]
    if len(hit):
        return list(hit["station_id"]), (hit.iloc[0]["label"] if len(hit) == 1 else f"stations matching '{text}'")
    raise ValueError(f"No station, building or area matches '{text}'. Use list_stations to see the names.")


# --- the toolkit -----------------------------------------------------------------------------------

class Toolkit:
    """Read-only analysis tools bound to one dataset and the viewer's station filter."""

    def __init__(self, ds: M.Dataset, ids=None):
        self.ds, self.ids = ds, ids
        self.outputs: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        P = {"type": "string", "description": PERIOD_HELP}
        S = {"type": "string", "description": STATIONS_HELP}
        self.tools = {t.name: t for t in [
            Tool("data_overview", "Start here. What data exists: when monitoring began, how current it is, whether "
                 "it's demo data, how many stations of each kind, collection quality, and what each measure means.",
                 {}, self.data_overview),
            Tool("list_stations", "Every print station in scope with its building, area, type (residence hall, "
                 "library, academic...), who supports it, and its status right now.", {"stations": S},
                 self.list_stations),
            Tool("station_profile", "Everything about one station: status now and why, supplies, availability, "
                 "outages, faults, repair times, report-card grade, and the nearest working backups.",
                 {"station": {"type": "string", "description": "Station ID or name"}, "period": P},
                 self.station_profile, ["station"]),
            Tool("availability", "Share of time printers could print, grouped as asked.",
                 {"period": P, "stations": S, "group_by": {"type": "string", "enum": [
                     "total", "station", "building", "area", "section", "day", "hour_of_day"]}},
                 self.availability),
            Tool("incidents", "Outages (station can't print), warnings, printer faults or paper-tray problems: "
                 "a list, or counts and durations grouped as asked.",
                 {"kind": {"type": "string", "enum": ["outage", "warning", "fault", "paper"]}, "period": P,
                  "stations": S, "group_by": {"type": "string", "enum": [
                      "none", "station", "building", "cause", "hour_of_day", "weekday", "day"]},
                  "limit": {"type": "integer"}}, self.incidents),
            Tool("repair_times", "How long problems lasted before being fixed: mean, median, 90th percentile; and "
                 "the support desks' share fixed in staffed hours vs after hours.",
                 {"period": P, "stations": S}, self.repair_times),
            Tool("supplies", "Toner, drums, belt and fuser: current levels, how fast they're used, forecast days "
                 "until replacement, recent replacements, and a Monte Carlo range of how many will be needed.",
                 {"stations": S, "component": {"type": "string", "description": "Optional: toner_k, toner_c, "
                                               "toner_m, toner_y, drum_k... belt, fuser"},
                  "period": P}, self.supplies),
            Tool("usage", "How much each printer is used, measured by toner consumed (Wepa doesn't publish page "
                 "counts), grouped as asked.", {"period": P, "stations": S, "group_by": {"type": "string", "enum": [
                     "station", "building", "area", "day", "weekday"]}}, self.usage),
            Tool("report_card", "The management grade (A-F) per printer with its score parts and the main reason.",
                 {"period": P, "stations": S}, self.report_card),
            Tool("compare_periods", "The same measures for two time ranges side by side.",
                 {"a": P, "b": P, "stations": S}, self.compare_periods, ["a", "b"]),
            Tool("campus_context", "The university around the printers: academic calendar phase and upcoming "
                 "dates, support desk hours, library hours, residence halls and residents, and how printing tracks "
                 "class schedules.", {"period": P}, self.campus_context),
            Tool("statistics", "Deeper analysis: which stations fail more than chance explains (Bayesian rates), "
                 "how often warnings turn into outages, recent significant changes, times several printers went "
                 "down together (shared network/Wepa/building causes), whether humid weather goes with more jams, downtime "
                 "weighted by how busy the printer usually is, coverage gaps when a station is "
                 "down, what evening staffing would save, and availability by academic phase.",
                 {"kind": {"type": "string", "enum": ["failure_rates", "warning_to_outage", "recent_changes",
                                                      "coverage_gaps", "staffing_whatif", "by_phase",
                                                      "downtime_drivers", "shared_outages", "weather_jams",
                                                      "busy_downtime"]},
                  "period": P, "stations": S}, self.statistics, ["kind"]),
            Tool("outage_risk", "The machine-learning outage-risk model: whether it is live (has proven itself "
                 "against a simple baseline on held-out weeks), its test scores and track record, and, if live, each "
                 "printer's chance of going down in the next 24 hours with the main reasons.",
                 {"stations": S}, self.outage_risk),
            Tool("investigations", "Documented investigations (INV numbers) into recurring printer problems: state, "
                 "location, root cause, actions, Wepa case and ITSM references. Read-only. Give a reference for one "
                 "investigation's full record, or nothing for the list.",
                 {"ref": {"type": "string", "description": "Optional: an INV reference"}}, self.investigations),
            Tool("self_check", "The app's check of itself: collector freshness, data quality, the investigation "
                 "audit trail, sign-in and security settings, known vulnerabilities in its packages (CVE numbers, "
                 "fixes, exploitation per CISA KEV/NVD/Microsoft/EPSS/Exploit-DB/Metasploit), the OWASP Top 10 checklist, "
                 "the running version and what changed in it, update packages and backup collectors. Use "
                 "for any question about the app itself.", {}, self.self_check),
            Tool("ask_dashboard", "The dashboard's own built-in answer to a plain-English question (a quick "
                 "cross-check, or for things like hall support contacts).",
                 {"question": {"type": "string"}}, self.ask_dashboard, ["question"]),
        ]}

    # Each tool returns text; run() records it for fact-checking and never raises.
    def run(self, name: str, args: dict | None) -> str:
        args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        self.calls.append((name, args))
        tool = self.tools.get(name)
        try:
            out = tool.fn(**args) if tool else f"Unknown tool '{name}'."
        except (ValueError, KeyError) as exc:
            out = f"Couldn't run {name}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a tool error is reported to the model, not raised
            out = f"{name} failed ({type(exc).__name__}). Try a different question or period."
        out = str(out)
        if len(out) > MAX_CHARS:
            out = out[:MAX_CHARS] + "\n... (cut short; narrow the question)"
        self.outputs.append(out)
        return out

    def specs(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "input_schema": t.schema()}
                for t in self.tools.values()]

    def _ids(self, stations=None):
        return resolve(self.ds, stations, self.ids)

    def _period(self, period=None):
        return parse_period(self.ds, period)

    # --- tools --------------------------------------------------------------------------------------

    def data_overview(self) -> str:
        from . import narrative as N
        ds = self.ds
        st = ds.stations if self.ids is None else ds.stations[ds.stations["station_id"].isin(self.ids)]
        start, end = M.window(ds, 7)
        q = M.data_quality(ds, start, end)
        lines = [
            f"Data as of: {_when(ds.as_of)} (US Eastern).",
            "DEMO DATA: synthetic, generated to behave like the real status page. It does not describe real BSU "
            "printers; say so in any answer." if ds.is_demo else "Live data from Wepa's public status page.",
            f"Monitoring began: {_when(ds.data_start)}; {N.history_days(ds):.1f} days recorded. Anything before "
            "that is unknown, not zero.",
            f"Stations in scope: {len(st)} (" + ", ".join(f"{k}: {v}" for k, v in
                                                       st["station_type"].value_counts().items()) + "); sections: "
            + ", ".join(f"{k}: {v}" for k, v in st["section"].value_counts().items()) + ".",
            f"Collection quality, last 7 days: {_num(q.value)}/100 (completeness {_num(q.extra.get('completeness'))}%, "
            f"failed checks {_num(q.extra.get('failure_rate'))}%).",
            "",
            "What the measures mean:",
            "- The monitor reads Wepa's status page about once a minute: each station's status codes, the "
            "printer's own message, and toner/drum/belt/fuser levels (percent).",
            "- Availability: share of observed time a station could print (not red). Warning (yellow) still prints.",
            "- Outage: a continuous stretch in red (can't print). Repair time: from the first red reading to "
            "the first non-red reading. A stretch that began before monitoring is marked censored.",
            "- Fault: a specific problem the printer reported (paper jam, tray open, toner out...), with a cause.",
            "- Usage: toner consumed, in points of a cartridge (100 points = one cartridge). Wepa doesn't publish "
            "page counts, so usage is relative, not pages.",
            "- Support: ResNet runs residence-hall stations from East Campus Commons; the IT Service Center runs "
            "the others from Maxwell Library. Both have desk hours; outside them nobody is on duty.",
            "- Report card: availability 35%, outages per week 25%, faults for workload 15%, parts wear for "
            "workload 15%, time in warning 10%. Busy printers are not penalized for being busy.",
        ]
        return "\n".join(lines)

    def list_stations(self, stations=None) -> str:
        from . import ops
        ids, _ = self._ids(stations)
        cur = ops.current_status(self.ds, ids)
        cur = cur.assign(now=cur["state"].map({"red": "DOWN", "yellow": "warning", "green": "printing"})
                         .fillna(cur["state"]), stale=cur["stale"])
        return _table(cur.sort_values(["section", "building", "label"]),
                      {"station_id": "id", "label": "station", "building": "building", "area": "area",
                       "station_type": "type", "owner": "supported by", "now": "status now",
                       "printer_text": "printer message"}, n=60)

    def station_profile(self, station, period=None) -> str:
        from . import narrative as N, nearby, ops, report_card
        from .dashboard.views.common import station_messages
        ids, label = self._ids(station)
        if len(ids) != 1:
            return (f"'{station}' matches {len(ids)} stations; pick one:\n"
                    + _table(self.ds.stations[self.ds.stations["station_id"].isin(ids)],
                             {"station_id": "id", "label": "station"}))
        sid = ids[0]
        start, end, plabel = self._period(period)
        cur_all = ops.current_status(self.ds)
        r = cur_all[cur_all["station_id"] == sid].iloc[0]
        lines = [f"{r['label']}, {r['building']} ({r['area']}); type {r['station_type']}; supported by {r['owner']}.",
                 f"Now: {'DOWN' if r['state'] == 'red' else 'warning' if r['state'] == 'yellow' else 'printing'}"
                 f" ({'; '.join(station_messages(r)) or 'no messages'}).",
                 "Supply levels now (%): " + ", ".join(f"{c} {_num(r[c])}" for c in config.COMPONENTS if c in r
                                                        and pd.notna(r[c]))]
        a = M.availability(self.ds, start, end, ids)
        lines.append(f"Availability, {plabel}: {_num(a.value)}% ({_num(a.extra.get('down_h', 0))} hours down).")
        sc = M.station_scorecard(self.ds, start, end, ids)
        if len(sc):
            s = sc.iloc[0]
            lines.append(f"Outages, {plabel}: {_num(s['red_incidents'])}; mean repair time "
                         f"{_num(s['mttr_red_min'])} min; most common fault: {s['top_fault'] or 'none'}.")
        f = M.faults_in(self.ds, start, end, ids)
        if len(f):
            lines.append("Faults by type: " + ", ".join(f"{k} {v}" for k, v in f["label"].value_counts().head(6).items()))
        card = report_card.build(self.ds, start, end, ids)
        if len(card) and pd.notna(card.iloc[0]["score"]):
            c = card.iloc[0]
            lines.append(f"Report card, {plabel}: grade {c['grade']} ({c['verdict']}), score {_num(c['score'])}/100. "
                         f"{c['why']}")
        fc = M.forecast(self.ds, ids)
        if len(fc):
            lines.append(_table(fc.sort_values("days_to_replace"), {
                "component": "part", "level": "level now %", "burn_per_day": "used per day (points)",
                "days_to_replace": "days until replacement (forecast)"}, n=10, title="Supplies forecast:"))
        b = nearby.backups(cur_all, sid, 3)
        if len(b):
            b = b.assign(dist=b["meters"].map(nearby.fmt_distance), walk=b["seconds"].map(nearby.fmt_walk))
            lines.append(_table(b, {"label": "nearest other station", "state": "status now", "dist": "distance",
                                    "walk": "walk"}, title="Backups:"))
        lines.append("Story: " + N.to_text(N.story(self.ds, N.period(self.ds, "this_week"), ids, label).paragraphs)
                     .replace("\n\n", " "))
        return "\n".join(lines)

    def availability(self, period=None, stations=None, group_by="total") -> str:
        ids, label = self._ids(stations)
        start, end, plabel = self._period(period)
        head = f"Availability for {label}, {plabel}."
        if group_by in (None, "total"):
            a = M.availability(self.ds, start, end, ids)
            return f"{head}\n{_num(a.value)}% of observed time could print; {_num(a.extra.get('down_h', 0))} printer-hours down."
        if group_by == "hour_of_day":
            h = M.availability_by_hour(self.ds, start, end, ids)
            return _table(h, {"hour": "hour (0-23, Eastern)", "daytype": "day type", "availability": "availability %",
                              "down_h": "printer-hours down"}, n=48, title=head)
        if group_by == "day":
            d = M.availability_daily(self.ds, start, end, ids)
            d = d.assign(local_date=pd.to_datetime(d["local_date"]).dt.strftime("%a %b %-d %Y"))
            return _table(d, {"local_date": "date", "availability": "availability %", "observed_h": "hours observed"},
                          n=62, title=head)
        sc = M.station_scorecard(self.ds, start, end, ids)
        if group_by == "station":
            return _table(sc.sort_values("availability"), {
                "label": "station", "building": "building", "availability": "availability %",
                "red_incidents": "outages", "observed_h": "hours observed"}, title=head + " Worst first.")
        sc = sc.assign(up_h=sc["availability"] / 100 * sc["observed_h"])
        g = sc.groupby(group_by).agg(up_h=("up_h", "sum"), observed_h=("observed_h", "sum"),
                                     stations=("station_id", "count"), outages=("red_incidents", "sum")).reset_index()
        g["availability"] = g["up_h"] / g["observed_h"] * 100
        return _table(g.sort_values("availability"), {group_by: group_by, "availability": "availability %",
                                                      "stations": "stations", "outages": "outages"}, title=head)

    def _incidents(self, kind, start, end, ids) -> pd.DataFrame:
        ds = self.ds
        if kind == "fault":
            df = M._in(ds.fault_inc, "start", start, end, ids).assign(cause=lambda d: d["label"])
        elif kind == "paper":
            df = M._in(ds.tray_inc, "start", start, end, ids).assign(cause=lambda d: "tray " + d["tray"].astype(str))
        else:
            sev = "red" if kind in (None, "outage") else "yellow"
            df = M._in(ds.sev_inc, "start", start, end, ids)
            df = df[df["severity"] == sev]
            from . import narrative as N, rules
            if sev == "red" and len(df):
                df = df.assign(cause=[", ".join(rules.issue_label(c) + (f" ({d})" if d else "")
                                                for c, d in N.outage_causes(ds, sid, t)[:2]) or "not reported"
                                      for sid, t in zip(df["station_id"], df["start"])])
            else:
                df = df.assign(cause="")
        st = ds.stations.set_index("station_id")
        return df.assign(station=df["station_id"].map(st["label"]), building=df["station_id"].map(st["building"]),
                         hours=df["duration_s"].map(_hours))

    def incidents(self, kind="outage", period=None, stations=None, group_by="none", limit=25) -> str:
        ids, label = self._ids(stations)
        start, end, plabel = self._period(period)
        df = self._incidents(kind, start, end, ids)
        name = {"outage": "Outages", "warning": "Warnings", "fault": "Printer faults",
                "paper": "Paper-tray problems"}.get(kind, kind)
        head = (f"{name} for {label}, {plabel}: {len(df)} in total, {_num(df['hours'].sum())} hours "
                f"combined; median {_num(df['hours'].median() * 60 if len(df) else None)} minutes.")
        if group_by in (None, "none"):
            df = df.sort_values("start", ascending=False)
            return _table(df.assign(ongoing=df["end"].isna()), {
                "start": "started", "station": "station", "cause": "cause", "hours": "hours",
                "ongoing": "still open", "censored_start": "began before monitoring"},
                n=min(int(limit or 25), MAX_ROWS), title=head + " Newest first.")
        local = df["start"].dt.tz_convert(TZ)
        key = {"hour_of_day": local.dt.hour, "weekday": local.dt.day_name(), "day": local.dt.date}.get(group_by)
        g = df.assign(k=key if key is not None else df[group_by]).groupby("k").agg(
            count=("hours", "size"), total_h=("hours", "sum"), median_h=("hours", "median")).reset_index()
        return _table(g.sort_values("count", ascending=False) if group_by not in ("hour_of_day", "day") else g,
                      {"k": group_by, "count": "count", "total_h": "total hours", "median_h": "median hours"},
                      n=40, title=head)

    def repair_times(self, period=None, stations=None) -> str:
        from . import support
        ids, label = self._ids(stations)
        start, end, plabel = self._period(period)
        lines = [f"Repair times for {label}, {plabel}."]
        for sev, name in (("red", "Outages"), ("yellow", "Warnings")):
            m = M.mttr(self.ds, sev, start, end, ids)
            lines.append(f"{name}: {m.n} resolved; mean {_num(m.value)} min, median {_num(m.extra.get('median'))} min, "
                         f"90th percentile {_num(m.extra.get('p90'))} min.")
        mb = M.mtbf(self.ds, start, end, ids)
        lines.append(f"Mean time between outages: {_num(mb.value)} hours of uptime.")
        inc = M._in(self.ds.sev_inc, "start", start, end, ids)
        inc = support.annotate(inc[inc["severity"] == "red"], self.ds.stations, self.ds.as_of)
        s = support.summary(inc)
        if len(s):
            s = s.assign(med_fix_h=s["median_fix_s"].map(_hours), med_wait_h=s["median_wait_s"].map(_hours),
                         after_pct=s["after_share"] * 100)
            lines.append(_table(s, {"owner": "desk", "hours": "desk hours", "outages": "outages",
                                    "after_pct": "% starting after hours", "med_fix_h": "median hours to fix",
                                    "med_wait_h": "median hours waiting for the desk to open"}, title="By support desk:"))
        return "\n".join(lines)

    def supplies(self, stations=None, component=None, period=None) -> str:
        from . import advanced
        ids, label = self._ids(stations)
        start, end, plabel = self._period(period)
        fc = M.forecast(self.ds, ids)
        if component:
            fc = fc[fc["component"] == component]
        out = [_table(fc.sort_values("days_to_replace"), {
            "station": "station", "component": "part", "level": "level now %", "burn_per_day": "points used per day",
            "days_to_replace": "forecast days until replacement"}, n=25,
            title=f"Supplies for {label}, soonest replacement first (forecast from recent use):")]
        br = M.burn_rates(self.ds, start, end, ids)
        out.append(_table(br, {"label": "part", "used_units": f"units used, {plabel}", "per_month": "per month",
                               "per_year": "per year (projected)", "stations": "stations"}, title="Use rates:"))
        rep = M.replacements(self.ds, start, end, ids)
        if component:
            rep = rep[rep["component"] == component]
        out.append(_table(rep.sort_values("ts", ascending=False), {
            "ts": "replaced", "station": "station", "component": "part", "level_before": "level before %"},
            n=15, title=f"Replacements, {plabel}: {len(rep)}"))
        mc = advanced.supplies_monte_carlo(self.ds, 30, ids, sims=1000)
        if mc and isinstance(mc.get("table"), pd.DataFrame):
            out.append(_table(mc["table"], {"label": "part", "p50": "likely (median)", "p90": "90% chance at most",
                                            "max": "worst case seen"},
                              title=f"Next 30 days, Monte Carlo from {mc.get('history_days')} days of use:"))
        return "\n\n".join(out)

    def usage(self, period=None, stations=None, group_by="station") -> str:
        ids, label = self._ids(stations)
        start, end, plabel = self._period(period)
        head = (f"Usage for {label}, {plabel}, measured by black toner used (points; 100 = one cartridge). "
                "'relative' is vs the typical (median) printer.")
        if group_by in ("day", "weekday"):
            u = M.usage_in(self.ds, start, end, ids)
            u = u[u["component"] == "toner_k"]
            local = u["scrape_ts"].dt.tz_convert(TZ)
            k = local.dt.date if group_by == "day" else local.dt.day_name()
            g = u.assign(k=k).groupby("k")["used"].sum().reset_index()
            return _table(g, {"k": group_by, "used": "black toner points used"}, n=62, title=head)
        u = M.usage_by_station(self.ds, start, end, ids)
        if group_by == "station":
            return _table(u.sort_values("usage_per_day", ascending=False), {
                "label": "station", "building": "building", "usage_per_day": "toner points per day",
                "relative": "relative", "cartridges_per_month": "black cartridges per month"}, title=head)
        g = u.groupby(group_by).agg(per_day=("usage_per_day", "sum"), stations=("station_id", "count")).reset_index()
        return _table(g.sort_values("per_day", ascending=False), {group_by: group_by, "per_day": "toner points per day",
                                                                  "stations": "stations"}, title=head)

    def report_card(self, period=None, stations=None) -> str:
        from . import report_card
        ids, label = self._ids(stations)
        start, end, plabel = self._period(period)
        c = report_card.build(self.ds, start, end, ids)
        s = report_card.summary(c)
        head = (f"Report card for {label}, {plabel}: {s['graded']} graded; grades " +
                ", ".join(f"{k} {v}" for k, v in sorted(s["counts"].items())) + f"; median score {_num(s['median'])}. "
                f"Stations with under {report_card.MIN_DAYS} days of data get no grade. Worst first.")
        return _table(c, {"label": "station", "grade": "grade", "score": "score /100", "availability": "availability %",
                          "outages_per_week": "outages per week", "faults_ratio": "faults vs campus (for workload)",
                          "wear_ratio": "parts wear vs campus (for workload)", "why": "main reason"}, title=head)

    def compare_periods(self, a, b, stations=None) -> str:
        ids, label = self._ids(stations)
        rows = []
        for spec in (a, b):
            s, e, pl = self._period(spec)
            av = M.availability(self.ds, s, e, ids)
            days = max((e - s).total_seconds() / 86400, 1e-9)
            inc = self._incidents("outage", s, e, ids)
            f = self._incidents("fault", s, e, ids)
            u = M.usage_in(self.ds, s, e, ids)
            rows.append({"period": pl, "days": days, "availability": av.value, "outages": len(inc),
                         "outages_per_day": len(inc) / days, "down_h": av.extra.get("down_h", np.nan),
                         "faults": len(f), "toner": u[u["component"] == "toner_k"]["used"].sum() / days})
        return _table(pd.DataFrame(rows), {"period": "period", "days": "days", "availability": "availability %",
                                           "outages": "outages", "outages_per_day": "outages per day",
                                           "down_h": "printer-hours down", "faults": "faults",
                                           "toner": "black toner points per day"},
                      title=f"Comparison for {label}. Compare per-day rates when the periods differ in length.")

    def campus_context(self, period=None) -> str:
        from . import campus, courses, support
        start, end, plabel = self._period(period)
        c = campus.load()
        today = self.ds.as_of.tz_convert(TZ).date()
        lines = []
        if not c.empty:
            d = c.on(today)
            if d is not None:
                lines.append(f"Today ({today:%a %b %-d}): {d['label']}; residence halls "
                             f"{'open' if d['halls_open'] else 'closed'}; library "
                             + (f"{_clock(d['library_open'])}-{_clock(d['library_close'])}" if d.get("library_known")
                                else "hours not published"))
            lines.append(f"{plabel.capitalize()}: {campus.describe(c, start.tz_convert(TZ).date(), end.tz_convert(TZ).date())}.")
            up = c.upcoming(today, 45)
            if len(up):
                lines.append(_table(up, {"date": "date", "event": "upcoming (next 45 days)"}, n=12))
            if len(c.halls):
                lines.append(_table(c.halls, {"building": "residence hall", "residents": "residents (approx.)",
                                              "who": "who lives there"}, title="Residence halls:"))
        for o in support.TEAMS:
            lines.append(f"{o} desk at {support.team(o)['base']}: {support.hours_text(o)}; now "
                         f"{support.desk_status(o, self.ds.as_of)[1]}.")
        try:
            cvp = courses.class_vs_printing(self.ds, start, end, self.ids)
        except Exception:  # noqa: BLE001
            cvp = None
        if cvp:
            lines.append(f"Class schedules ({cvp['term']}, {cvp['sections']} sections): printing by hour tracks "
                         f"class meetings with Spearman rho {_num(cvp['rho'])} (1 = moves together exactly).")
        return "\n".join(lines) or "No campus calendar loaded."

    def statistics(self, kind, period=None, stations=None) -> str:
        from . import advanced, insights
        ids, label = self._ids(stations)
        start, end, plabel = self._period(period)
        head = f"{kind.replace('_', ' ').replace('whatif', 'what-if').capitalize()} for {label}, {plabel}."
        if kind == "failure_rates":
            b = advanced.bayes_rates(self.ds, start, end, ids)
            if b is None or "mean" not in b:
                return head + " Not enough history yet to estimate failure rates."
            return _table(b.sort_values("mean", ascending=False), {
                "label": "station", "n": "outages", "weeks": "weeks observed", "raw": "raw per week",
                "mean": "estimated per week (shrunk to campus)", "lo": "90% low", "hi": "90% high",
                "above_campus": "clearly above campus"}, title=head + " Empirical Bayes (Gamma-Poisson).")
        if kind == "warning_to_outage":
            w = advanced.warning_to_outage(self.ds, start, end, ids)
            if not w or w.get("share") is None:
                return head + " Not enough warnings yet."
            return "\n".join([head, f"{w['warnings']} warnings; {_num(w['share'] * 100)}% were followed by an outage "
                              f"within 24 hours (95% interval {_num(w['ci'][0] * 100)}-{_num(w['ci'][1] * 100)}%); "
                              f"median lag {_num(w['median_lag_h'])} hours.",
                              _table(w["by_cause"], {"cause": "warning", "warnings": "count",
                                                     "led_to_outage": "led to outage", "share": "share"})])
        if kind == "recent_changes":
            r = advanced.recent_changes(self.ds, ids)
            return _table(r, {c: c for c in r.columns}, title=head + " Exact Poisson rate-ratio test, last 14 days "
                          "vs before.") if len(r) else head + " No statistically clear changes (or not enough history)."
        if kind == "coverage_gaps":
            cg = advanced.coverage(self.ds, start, end, ids)
            if cg is None or "stranded_min" not in cg:
                return head + " Not enough data yet."
            return _table(cg.sort_values("stranded_min", ascending=False), {
                "label": "station", "nearest": "nearest backup", "walk_min": "walk minutes", "down_h": "hours down",
                "stranded_min": "minutes with no backup in the building", "gap": "coverage gap"}, title=head)
        if kind == "staffing_whatif":
            s = advanced.staffing_whatif(self.ds, start, end, ids)
            if not s or s.get("saved_h") is None:
                return head + " Not enough outages yet to simulate staffing changes."
            lo, mid, hi = s["saved_h"]
            return (f"{head} Scenario: {s['name']}. Of {s['outages']} outages, {s['after_hours']} started after hours; "
                    f"{s['affected']} would be caught. Printer-hours of downtime saved: about {_num(mid)} "
                    f"(range {_num(lo)}-{_num(hi)}), of {_num(s['total_h'])} total; about {_num(s['saved_h_per_week'])} "
                    "per week. Simulation from past outages; an estimate, not a promise.")
        if kind == "by_phase":
            p = insights.by_phase(self.ds, start, end, ids)
            return _table(p, {"label": "academic phase", "days": "days", "availability": "availability %",
                              "outages_per_day": "outages per day", "faults_per_day": "faults per day",
                              "toner_k_per_day": "black cartridges used per day"}, title=head)
        if kind == "weather_jams":
            from . import weather as W
            r = W.jams_vs_humidity(self.ds, start, end, ids)
            out = head + " " + W.sentence(r) + " (Outdoor humidity; an association, not proof of a cause.)"
            if r and r.get("status") == "ok":
                out += "\n" + _table(r["bands"], {"band": "humidity band", "humidity": "average humidity %",
                                                  "hours": "hours", "jams": "jams", "per100": "jams per 100 toner points"})
            return out
        if kind == "busy_downtime":
            from . import impact
            t = impact.by_station(self.ds, start, end, ids)
            return head + " " + impact.summary(t) + " (Weighted by usual printing at that hour; not a count of " \
                "people.)\n" + _table(t, {"label": "station", "down_h": "hours down", "weighted_h": "busy-weighted hours",
                                          "rank_plain": "rank by hours", "rank_busy": "rank weighted"}, n=15)
        if kind == "shared_outages":
            from . import correlated as C
            c = C.clusters(self.ds, start, end, ids)
            return head + " " + C.summary(c) + "\n" + _table(c, {
                "start": "started", "stations": "printers", "building_list": "buildings", "causes": "reported",
                "kind": "likely cause", "together": "recovered together", "chance_windows":
                "windows this size expected by chance in the period", "verdict": "verdict"}, n=15)
        if kind == "downtime_drivers":
            d = insights.downtime_drivers(self.ds, start, end, ids)
            return "\n\n".join(_table(v.assign(pct=v["share"] * 100), {"label": k, "down_h": "hours down",
                                                                      "outages": "outages", "pct": "% of downtime"},
                                      n=8, title=f"Downtime by {k}:") for k, v in d.items())
        return f"Unknown kind '{kind}'."

    def outage_risk(self, stations=None) -> str:
        from . import risk
        card = risk.load_card(self.ds)
        if not card:
            return "The outage-risk model hasn't been trained yet."
        champ = card.get("champion")
        m = card.get("models", {}).get(champ or "", {})
        lines = [f"Status: {card['status'].upper()}" + (" (not reliable enough to use yet: " + " ".join(card.get(
                     "reasons", [])) + ")" if card["status"] != "live" else ""),
                 f"Trained {card['trained_at'][:16]} UTC on {card.get('rows', 0):,} printer-hours with "
                 f"{card.get('positives', 0):,} outage starts" + (" (DEMO data)" if card.get("demo") else "") + "."]
        if m:
            lines.append(f"Best model: {risk.MODEL_NAMES.get(champ, champ)}; on held-out weeks: Brier {_num(m.get('brier'))}, "
                         f"skill vs baseline {_num((m.get('skill') or 0) * 100)}%, AUC {_num(m.get('auc'))}, "
                         f"top-3 hit rate {_num((m.get('top3_hit') or 0) * 100)}% vs base rate "
                         f"{_num((card.get('base_rate') or 0) * 100)}%.")
        tr = risk.track_record(self.ds)
        if tr:
            lines.append(f"Live track record: {tr['predictions']} predictions checked; riskiest fifth went down "
                         f"{_num(tr['top20_rate'] * 100)}% of the time vs {_num(tr['base_rate'] * 100)}% overall.")
        if card["status"] != "live":
            lines.append("Do not quote individual printer risks while the model is learning.")
            return "\n".join(lines)
        ids, _ = self._ids(stations)
        f = risk.predict_now(self.ds, ids)
        t = f.table.assign(pct=f.table["p"] * 100) if len(f.table) else f.table
        lines.append(_table(t, {"label": "station", "pct": "chance of an outage in 24 h %", "relative": "vs typical (x)",
                                "band": "band", "reasons": "mostly because of"}, n=15,
                            title="Current risk (printers that are up):"))
        return "\n".join(lines)

    def investigations(self, ref=None) -> str:
        from . import investigations as INV
        if ref:
            r = INV.get(self.ds.data_dir, str(ref).strip().upper())
            if not r:
                return f"No investigation {ref}."
            keys = ["title", "state", "location", "category", "first_occurrence", "assignee", "root_cause",
                    "root_cause_status", "action_taken", "itsm_ref", "wepa_case", "impact_override", "linked"]
            lines = [f"{r['ref']}: " + "; ".join(f"{k}: {r.get(k)}" for k in keys if r.get(k))]
            lines += [f"Note ({_when(pd.Timestamp(e['ts'], unit='s', tz='UTC'))}, {e.get('user_name')}): {e['note']}"
                      for e in r["notes"]]
            return "\n".join(lines)
        t = INV.table(self.ds.data_dir)
        if t.empty:
            return "No investigations have been opened."
        return _table(t, {"ref": "reference", "title": "name", "state": "state", "location": "kiosk location",
                          "assignee": "assigned to", "archived": "archived"}, n=40,
                      title=f"{len(t)} investigation(s), newest first.")

    def self_check(self) -> str:
        from . import __version__, selfcheck, updates, vulns
        checks = selfcheck.run(self.ds)
        _, sentence = selfcheck.summary(checks)
        lines = [f"Self-check: {sentence}"]
        lines += [f"- [{c.status.upper()}] {c.area} / {c.name}: {c.detail}" + (f" (To fix: {c.fix})" if c.fix else "")
                  for c in checks]
        rel = updates.release(__version__)
        if rel:
            lines.append(f"Version {__version__} ({rel.date}) change summary: {rel.summary}")
        for f in vulns.findings(self.ds.data_dir)[:10]:
            lines.append(f"- Vulnerability {f.headline_id} ({', '.join(f.ids[1:3])}) in {f.package} {f.installed}, "
                         f"{f.severity}, priority {f.priority or 'not ranked'}: {f.summary} Fixed in: "
                         f"{f.fixed or 'no fix yet'}. Why this priority: {' '.join(f.reasons) or 'not ranked'}"
                         + (" (install tool, not used by the running app)" if f.tooling else ""))
        from . import owasp
        for it in owasp.checklist(self.ds.data_dir):
            lines.append(f"- OWASP {it.code}:2025 {it.name}: {it.status}"
                         + (f"; gaps: {' '.join(it.gaps)}" if it.gaps else "")
                         + "".join(f"; live check failed: {w}" for ok, w in it.live if not ok))
        return "\n".join(lines)

    def ask_dashboard(self, question) -> str:
        from . import ask, narrative as N
        a = ask.answer(self.ds, str(question)[:300], self.ids)
        out = [f"({a.understood}) " + N.to_text(a.paragraphs)]
        if a.table is not None and len(a.table):
            out.append(_table(a.table, dict(a.table_cols), n=15))
        return "\n".join(out)


def _clock(h) -> str:
    if h is None or pd.isna(h):
        return "?"
    hh, mm = int(h), int(round((h % 1) * 60))
    return f"{(hh - 1) % 12 + 1}:{mm:02d} {'AM' if hh < 12 or hh == 24 else 'PM'}"
