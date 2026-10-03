"""Analytics: reliability, faults, consumables and data quality over a period, one tab each."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dash import html

from ... import campus, config, insights, metrics as M, models, narrative as N, support
from .. import charts
from ..components import (chart_card, data_table, explore_hint, fmt_hours, fmt_minutes, fmt_num, headline, metric_tile,
                          segmented, tile)
from .common import empty, needs_days, period_label, period_window, scope_ids

TABS = [{"label": "Reliability", "value": "reliability"}, {"label": "Faults", "value": "faults"},
        {"label": "Consumables", "value": "consumables"}, {"label": "Forecasts & statistics", "value": "stats"},
        {"label": "Data quality", "value": "quality"}]
BURN_UNITS = {"per_day": "day", "per_week": "week", "per_month": "month", "per_year": "year"}


def owners_in(ds, ids) -> list[str]:
    st = ds.stations if ids is None else ds.stations[ds.stations["station_id"].isin(ids)]
    return [o for o in support.TEAMS if (st["owner"] == o).any()]


def _sentence(text: str) -> str:
    """Capitalize the first letter only (str.capitalize would lowercase station names)."""
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _hour(h: int) -> str:
    return f"{(h % 12) or 12} {'am' if h < 12 else 'pm'}"


def render(ds: M.Dataset, theme: str, sections, areas, period, tab: str, burn_unit: str = "per_week"):
    if ds.empty:
        return empty("No data yet.")
    ids = scope_ids(ds, sections, areas)
    if ids is not None and not ids:
        return empty("No stations in this scope.")
    start, end = period_window(ds, period)
    plabel = period_label(period)
    fn = {"reliability": _reliability, "faults": _faults, "consumables": _consumables,
          "stats": _stats, "quality": _quality}.get(tab, _reliability)
    return fn(ds, theme, ids, start, end, plabel, burn_unit)


def _n_note(m, word="incidents"):
    return f"n = {m.n:,} {word}" + (f" · median {fmt_minutes(m.extra['median'])}" if "median" in m.extra else "")


# --- Reliability -------------------------------------------------------------------------------

def _reliability(ds, theme, ids, start, end, plabel, _):
    avail = M.availability(ds, start, end, ids)
    mttr_r = M.mttr(ds, "red", start, end, ids)
    mttr_y = M.mttr(ds, "yellow", start, end, ids)
    mtbf = M.mtbf(ds, start, end, ids)
    paper = M.paper_refill_time(ds, start, end, ids)
    tray = M.tray_empty_time(ds, start, end, ids)
    sc = M.station_scorecard(ds, start, end, ids).sort_values("availability")

    if avail.value is not None:
        worst = sc[sc["observed_h"] >= 12].head(1)
        detail = []
        if mttr_r.value is not None:
            detail.append(f"red incidents took a median {fmt_minutes(mttr_r.extra['median'])} to clear")
        if len(worst) and avail.value - worst.iloc[0]["availability"] >= 2:
            detail.append(f"lowest was {worst.iloc[0]['label']} at {worst.iloc[0]['availability']:.1f}%")
        hl = headline(N.availability_tone(avail.value),
                      f"Printers were available {avail.value:.1f}% of the time over the last {plabel}",
                      _sentence("; ".join(detail)) if detail else "")
    else:
        hl = headline("info", "Not enough data yet for reliability figures", avail.note)

    tiles = html.Div(className="tiles", children=[
        metric_tile("Availability", avail, lambda v: f"{v:.2f}%",
                    "Observed printer-minutes not in red ÷ all observed printer-minutes. Unobserved time is excluded.",
                    unit_note=f"{avail.extra.get('down_h', 0):,.0f} printer-hours down" if avail.value else ""),
        metric_tile("Mean time to repair (MTTR) · down", mttr_r, fmt_minutes, "Mean time from a red status appearing to clearing.",
                    _n_note(mttr_r)),
        metric_tile("Mean time to clear · warnings", mttr_y, fmt_minutes,
                    "Mean time from a yellow status appearing to clearing.", _n_note(mttr_y)),
        metric_tile("Time between failures", mtbf, fmt_hours, "Printer-hours of uptime per red incident.",
                    f"{mtbf.n:,} failures"),
        metric_tile("Paper refill time", paper, fmt_minutes, "Mean duration of PAPER OUT (station can't print).",
                    _n_note(paper, "paper-outs")),
        metric_tile("Tray empty time", tray, fmt_minutes,
                    "Mean duration of a single tray reporting empty, even while another tray keeps printing.",
                    f"{tray.extra.get('total_h', 0):,.0f} tray-hours in total"),
    ])

    fleet = M.availability_daily(ds, start, end, ids)
    by_sec = M.availability_daily(ds, start, end, ids, by="section")
    b = M.building_availability(ds, start, end, ids)
    inc = M._resolved(M._in(ds.sev_inc, "start", start, end, ids)).copy()
    inc["week"] = inc["start"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None).dt.to_period("W").dt.start_time
    weekly = (inc.groupby(["week", "severity"])["duration_s"]
              .agg(median_min=lambda s: s.median() / 60, mean_min=lambda s: s.mean() / 60, n="size").reset_index())

    red_all = M._in(ds.sev_inc, "start", start, end, ids)
    red_all = red_all[red_all["severity"] == "red"]
    summ = support.summary(red_all)
    if len(summ):
        worst = summ.sort_values("after_share", ascending=False).iloc[0]
        tot_after = summ["after_h"].sum() / max(1e-9, (summ["after_h"] + summ["staffed_h"]).sum())
        desk_note = (f"{tot_after:.0%} of all downtime fell outside desk hours. {worst['owner']}: "
                     f"{worst['after_hours']} of {worst['outages']} outages began after hours.")
    desk_card = chart_card(
        "Downtime vs. support desk hours", "Printer-hours down, split by whether the owning desk was staffed. "
        + " · ".join(f"{o}: {support.hours_text(o)}" for o in summ["owner"]) if len(summ) else "",
        charts.owner_hours(theme, summ) if len(summ) else None, graph_id={"type": "xg", "chart": "owner_hours"},
        body=None if len(summ) else empty("No outages in this period.", big=False),
        note=desk_note if len(summ) else "",
        explain=["ResNet (based at East Campus Commons) looks after the residence-hall stations; the IT Service "
                 "Center (Maxwell Library) looks after labs and the satellite campus. Each desk is only staffed "
                 "part of the week.",
                 "Gray is downtime nobody was scheduled to fix. When it dominates, coverage (an evening or weekend "
                 "check) buys more uptime than faster repairs. Click a bar for the numbers."],
        table=data_table(summ.assign(after=summ["after_share"].map(lambda v: f"{v:.0%}")), [
            ("owner", "Owner", None), ("hours", "Desk hours", None), ("outages", "Outages", None),
            ("after", "Began after hours", None),
            ("median_wait_s", "Median wait for desk", lambda v: fmt_minutes(v / 60)),
            ("median_desk_fix_s", "Desk time to fix", lambda v: fmt_minutes(v / 60)),
            ("staffed_h", "Down in desk hours", lambda v: f"{v:,.0f} h"),
            ("after_h", "Down after hours", lambda v: f"{v:,.0f} h")]) if len(summ) else None)

    cal = campus.load()
    phases = insights.by_phase(ds, start, end, ids)
    phase_card = chart_card(
        "Across the academic year", "Outages per day in each part of the calendar, from BSU's registrar "
        "and Residence Life schedules.",
        charts.phase_bars(theme, phases) if len(phases) > 1 else None, graph_id={"type": "xg", "chart": "phases"},
        body=None if len(phases) > 1 else empty("This period sits inside one part of the calendar; widen the "
                                                 "period to compare.", big=False),
        explain=["Each bar is the average number of outages a day during that part of the year. Finals and the "
                 "first weeks of classes are the busiest printing; breaks and summer are quiet.",
                 "Compare like with like: a quiet summer week isn't 'good performance', and a rough finals week "
                 "matters more than a rough week in July."],
        table=data_table(phases, [("label", "Part of the year", None), ("days", "Days", None),
                                  ("availability", "Available", lambda v: f"{v:.1f}%"),
                                  ("outages_per_day", "Outages a day", lambda v: f"{v:.1f}"),
                                  ("faults_per_day", "Faults a day", lambda v: f"{v:.1f}"),
                                  ("toner_k_per_day", "Black toners a day", lambda v: f"{v:.2f}")])
        if len(phases) else None)
    return [hl, explore_hint(), tiles, html.Div(className="grid", children=[
        chart_card("Daily availability", "Share of each day with the station able to print.",
                   *needs_days(fleet, lambda: charts.add_calendar(charts.availability_daily(theme, fleet, by_sec),
                                                                  theme, cal.days, start, end)), wide=True,
                   graph_id={"type": "xg", "chart": "avail_daily"},
                   explain=["Each line is the share of a day that stations could print. The black line is all BSU "
                            "stations; colored lines split it by section.",
                            "Dips are days with outages. Click a point to see which stations caused it. Shaded "
                            "bands come from BSU's academic calendar (finals, breaks, move-in, summer); dotted "
                            "lines are holidays."],
                   table=data_table(fleet, [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}"),
                                            ("availability", "Availability", lambda v: f"{v:.2f}%"),
                                            ("observed_h", "Observed printer-h", lambda v: f"{v:,.1f}")])),
        chart_card("Building coverage", "Share of time at least one printer in the building could print.",
                   charts.building_bars(theme, b) if len(b) else None, graph_id={"type": "xg", "chart": "building_cov"},
                   explain="Longer bars are better. A building with two printers can stay covered while one is "
                           "down; a single-printer building can't. Click a bar for the details.",
                   body=None if len(b) else empty("No data.", big=False),
                   table=data_table(b, [("building", "Building", None), ("stations", "Stations", None),
                                        ("any_up", "Any printer up", lambda v: f"{v:.2f}%"),
                                        ("all_up", "All printers up", lambda v: f"{v:.2f}%")])),
        chart_card("Time to fix, by week", "Median minutes from alert to clear.",
                   charts.mttr_weekly(theme, weekly) if len(weekly) else None,
                   body=None if len(weekly) else empty("No resolved incidents yet.", big=False),
                   table=data_table(weekly, [("week", "Week of", lambda d: f"{d:%Y-%m-%d}"),
                                             ("severity", "Severity", None), ("median_min", "Median", fmt_minutes),
                                             ("mean_min", "Mean", fmt_minutes), ("n", "Incidents", None)])),
        desk_card,
        phase_card,
        chart_card("Station scorecard", "Worst availability first. Click a station for its full history.",
                   wide=True, body=data_table(sc, [
                       ("label", "Station", None), ("area", "Area", None), ("owner", "Supported by", None),
                       ("availability", "Availability", lambda v: fmt_num(v, 2, "%")),
                       ("red_incidents", "Times down", None), ("mttr_red_min", "MTTR", fmt_minutes),
                       ("mtbf_h", "Between failures", fmt_hours), ("top_fault", "Most common fault", None)],
                       link_col=("label", "station_id"))),
    ])]


# --- Faults -------------------------------------------------------------------------------------

def _faults(ds, theme, ids, start, end, plabel, _):
    faults = M.faults_in(ds, start, end, ids)
    if faults.empty:
        return [headline("good", f"No faults in the last {plabel}"), empty("Nothing to chart.", big=False)]
    counts = faults["label"].value_counts()
    by_building = faults.groupby("building").size().sort_values(ascending=False).head(12)
    by_hour = faults["hour"].value_counts().reindex(range(24), fill_value=0).to_numpy()
    window = np.array([by_hour[[h, (h + 1) % 24, (h + 2) % 24]].sum() for h in range(24)])
    h0 = int(window.argmax())
    hl = headline("info", f"{counts.index[0]} is the most common fault: {counts.iloc[0]} of {len(faults)} "
                          f"in the last {plabel}",
                  f"Faults cluster between {_hour(h0)} and {_hour((h0 + 3) % 24)} "
                  f"({window[h0] / len(faults):.0%} of them). {by_building.index[0]} had the most "
                  f"({by_building.iloc[0]}).")
    by_station = faults.groupby(["station_id", "station"]).size().rename("n").reset_index() \
        .sort_values("n", ascending=False)
    return [hl, html.Div(className="grid", children=[
        chart_card("Fault types", f"{len(faults):,} fault incidents.", charts.pareto(theme, counts), graph_id={"type": "xg", "chart": "fault_types"},
                   explain="Each bar counts how often a problem started. Click one to see what it means on the "
                           "floor, where it happens most, and how to fix it.",
                   table=data_table(counts.rename("n").reset_index(), [("label", "Fault", None),
                                                                       ("n", "Incidents", None)])),
        chart_card("When faults start", "By weekday and local hour. Outlines mark support desk hours.",
                   charts.heatmap(theme, faults, owners_in(ds, ids)),
                   graph_id={"type": "xg", "chart": "heatmap"}, explain="Darker squares mean more faults started in that hour. The outlined "
                   "blocks are when ResNet and the IT Service Center are staffed; dark squares outside them wait for "
                   "the next shift. Click a square for details."),
        chart_card("Faults by building", "Top 12 buildings.", charts.pareto(theme, by_building),
                   graph_id={"type": "xg", "chart": "faults_building"}),
        chart_card("Faults by station", "Click a station for its incident history.", body=data_table(
            by_station, [("station", "Station", None), ("n", "Fault incidents", None)], max_rows=40,
            link_col=("station", "station_id"))),
    ])]


# --- Consumables ---------------------------------------------------------------------------------

def _consumables(ds, theme, ids, start, end, plabel, burn_unit):
    burn = M.burn_rates(ds, start, end, ids)
    unit = BURN_UNITS.get(burn_unit, "week")
    burn["rate_pts"] = burn[burn_unit]
    burn["rate_units"] = burn[burn_unit] / 100
    cum = M.cumulative_usage(ds, start, end, ids)
    repl = M.replacements(ds, start, end, ids)
    k = burn.set_index("component")
    hl = headline("info", f"BSU print stations used {k.loc['toner_k', 'used_units']:.1f} black toner cartridges' worth "
                          f"in the last {plabel}",
                  f"At the current rate that's about {k.loc['toner_k', 'per_month'] / 100:.1f} a month. "
                  f"{len(repl)} parts were replaced in this period.")
    toner_repl = repl[repl["component"].str.startswith("toner")]
    repl_note = (f"Toner was replaced at an average of {toner_repl['level_before'].mean():.1f}% remaining "
                 f"across {len(toner_repl)} swaps." if len(toner_repl) else "")
    return [hl, html.Div(className="grid", children=[
        chart_card(f"Use per {unit}", "Percentage points used, summed across stations; 100 pts = one part. "
                   "Refills never count as negative use.", wide=True,
                   action=segmented("burn-unit", [{"label": v.capitalize(), "value": k2}
                                                  for k2, v in BURN_UNITS.items()], burn_unit),
                   body=data_table(burn, [
                       ("label", "Part", None), ("used_units", "Used in period (parts)", lambda v: fmt_num(v, 2)),
                       ("rate_pts", f"Per {unit} (pts)", lambda v: fmt_num(v, 1)),
                       ("rate_units", f"Per {unit} (parts)", lambda v: fmt_num(v, 2)),
                       ("stations", "Stations with enough data", None)])),
        chart_card("Cumulative toner use", "Parts' worth used since the start of the period.",
                   *needs_days(cum, lambda: charts.add_calendar(charts.cumulative(theme, cum, ["toner_k", "toner_c", "toner_m", "toner_y"]),
                                                     theme, campus.load().days, start, end)),
                   graph_id={"type": "xg", "chart": "cum_toner"}, explain="Each line climbs as toner is used. The steeper it climbs, the faster "
                   "that color is being consumed; the end value is how many cartridges' worth were used."),
        chart_card("Cumulative drum use", "Parts' worth of drum life used.",
                   *needs_days(cum, lambda: charts.cumulative(theme, cum, ["drum_k", "drum_c", "drum_m", "drum_y"])),
                   graph_id={"type": "xg", "chart": "cum_drum"}),
        chart_card("Cumulative belt and fuser use", "Parts' worth of belt and fuser life used.",
                   *needs_days(cum, lambda: charts.cumulative(theme, cum, ["belt", "fuser"])), graph_id={"type": "xg", "chart": "cum_other"}),
        chart_card("Replacement log", f"A level jump of {config.REPLACEMENT_JUMP_PTS}+ points counts as a "
                   "replacement. 'Level before' is what was left in the old part.",
                   wide=True, note=repl_note, body=data_table(repl, [
                       ("ts", "When", lambda d: f"{d.tz_convert(config.LOCAL_TZ):%b %-d, %-I:%M %p}"),
                       ("station", "Station", None), ("label", "Part", None),
                       ("level_before", "Level before", lambda v: f"{v:.0f}%"),
                       ("level_after", "Level after", lambda v: f"{v:.0f}%")], max_rows=60,
                       empty="No replacements detected in this period.", link_col=("station", "station_id"))),
    ])]


# --- Data quality -----------------------------------------------------------------------------------

def _quality(ds, theme, ids, start, end, plabel, _):
    dq = M.data_quality(ds, start, end)
    q = M.quality_daily(ds, start, end)
    if dq.value is None:
        return [headline("info", "No monitoring history yet")]
    tone = "good" if dq.value >= 95 else ("warning" if dq.value >= 80 else "critical")
    hl = headline(tone, f"Data quality {dq.value:.0f}/100: the monitor captured "
                        f"{dq.extra['completeness']:.1f}% of expected snapshots",
                  f"{dq.extra['failures']:,} of {dq.extra['attempts']:,} refreshes failed "
                  f"({dq.extra['failure_rate']:.2f}%) over the last {plabel}. Metrics exclude unobserved time "
                  "rather than guessing.")
    tiles = html.Div(className="tiles", children=[
        metric_tile("Data quality score", dq, lambda v: f"{v:.0f} / 100",
                    "30% freshness + 40% completeness + 20% validity + 10% station coverage."),
        tile("Completeness", f"{dq.extra['completeness']:.1f}%", "snapshots received ÷ expected"),
        tile("Refresh failures", f"{dq.extra['failure_rate']:.2f}%", f"{dq.extra['failures']:,} failed"),
        tile("Freshness", fmt_minutes(dq.extra["age_min"]), "since the last good snapshot"),
    ])
    return [hl, tiles, html.Div(className="grid", children=[
        chart_card("Snapshot completeness", "Successful scrapes ÷ expected, per day.",
                   *needs_days(q, lambda: charts.daily_quality(theme, q, "completeness", "Completeness", False))),
        chart_card("Refresh failure rate", "Failed scrape attempts per day.",
                   *needs_days(q, lambda: charts.daily_quality(theme, q, "failure_rate", "Failure rate", True)),
                   table=data_table(q, [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}"),
                                        ("attempts", "Attempts", None), ("ok", "Succeeded", None),
                                        ("failure_rate", "Failure rate", lambda v: f"{v:.2f}%")])),
    ])]


# --- Forecasts & statistics ---------------------------------------------------------------------------

def _p(p: float) -> str:
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def eol_window(row) -> str:
    """'now', '3.2 days (2.9-3.6)' or '12 days' for a row of models.eol_forecast."""
    d = row["days"]
    if d is None or not np.isfinite(d):
        return "—"
    if d == 0:
        return "now"
    text = f"{d:.1f} days" if d < 10 else f"{d:.0f} days"
    lo, hi = row.get("days_early"), row.get("days_late")
    if row.get("method") == "regression" and lo is not None and np.isfinite(lo) and hi is not None and np.isfinite(hi) \
            and hi - lo >= 0.1:
        text += f" ({lo:.1f}-{hi:.1f})" if d < 10 else f" ({lo:.0f}-{hi:.0f})"
    return text


def _stats(ds, theme, ids, start, end, plabel, _):
    ttf = models.time_to_fix(ds, start, end, ids)
    cc = models.fault_control_chart(ds, start, end, ids)
    anomalies = models.station_anomalies(ds, start, end, ids)
    uf = models.usage_vs_reliability(ds, start, end, ids)
    eol = models.eol_forecast(ds, ids)

    # Headline: the single most decision-relevant finding.
    findings = []
    if ttf:
        names = list(ttf.medians)
        day, night = ttf.medians[names[0]], ttf.medians[names[1]]
        if ttf.p_value < 0.05 and np.isfinite(day) and np.isfinite(night) and night > day:
            findings.append(f"Outages that start after support-desk hours take {night / day:.1f}× longer to fix "
                            f"(median {night:.1f} h vs {day:.1f} h, {_p(ttf.p_value)})")
    if cc is not None:
        findings.append(f"{len(cc.signals)} control-chart signal{'s' if len(cc.signals) != 1 else ''} "
                        f"in daily faults" if cc.signals else "Daily faults stayed within normal limits")
    if uf:
        findings.append("busier printers fail significantly more" if uf.p_value < 0.05 and uf.slope > 0 else
                        "usage doesn't explain which printers fail")
    if findings:
        first = findings[0]
        rest = "; ".join(findings[1:])
        hl = headline("info", first, (rest[:1].upper() + rest[1:] + ".") if rest else "")
    else:
        hl = headline("info", "Not enough data for statistical models yet",
                      "Each model needs a minimum number of incidents or days; they'll appear as data accumulates.")

    cards = []
    if ttf:
        rows = pd.DataFrame([{"group": g, "n": ttf.n[g], "median": ttf.medians[g] * 60,
                              "w1": ttf.within[g][1], "w4": ttf.within[g][4], "w12": ttf.within[g][12]}
                             for g in ttf.curves])
        verdict = ("The difference is statistically significant" if ttf.p_value < 0.05 else
                   "The difference could be chance") + f" (log-rank test, {_p(ttf.p_value)})."
        cards.append(chart_card(
            "How long outages last", "Kaplan-Meier survival curves: the share of outages still unresolved after "
            "each hour. Outages still open are included as censored data rather than dropped.",
            charts.km_curves(theme, ttf), wide=True, note=verdict, graph_id={"type": "xg", "chart": "km"},
            explain=["Both lines start at 100% (every outage is unresolved when it begins) and fall as outages "
                     "are fixed. A line that drops fast means quick fixes.",
                     "Where a line crosses 50% is the median time to fix. The gap between the lines is the "
                     "difference staffing makes: 'after hours' means outside the owning desk's hours (ResNet or the "
                     "IT Service Center)."],
            table=data_table(rows, [("group", "Group", None), ("n", "Outages", None),
                                    ("median", "Median time to fix", fmt_minutes),
                                    ("w1", "Fixed within 1 h", lambda v: f"{v:.0%}"),
                                    ("w4", "Within 4 h", lambda v: f"{v:.0%}"),
                                    ("w12", "Within 12 h", lambda v: f"{v:.0%}")])))
    else:
        cards.append(chart_card("How long outages last", "", body=empty(
            "Needs at least 10 outages, with some in each group.", big=False), wide=True))

    if cc is not None:
        sig = html.Ul([html.Li(x) for x in cc.signals], className="observations") if cc.signals else \
            html.P("No day broke the limits, and there was no sustained run above or below average: "
                   "day-to-day variation looks like normal noise.", className="card__note")
        cards.append(chart_card(
            "Is today normal? Fault control chart", "A c-chart: daily fault incidents against limits at the average "
            "± 3√average. Days outside the limits, or 8 days in a row on one side, signal a real change.",
            charts.control_chart(theme, cc), body=sig, graph_id={"type": "xg", "chart": "cchart"},
            explain="Points inside the limits are normal day-to-day variation, so don't chase them. A point "
                    "above the red line, or a long run on one side of the average, means something changed.",
            table=data_table(cc.daily, [("local_date", "Date", lambda d: f"{d:%a %b %-d}"),
                                        ("count", "Faults", None)])))
        cards.append(chart_card(
            "Unusual station-days", "Days when a station had far more faults than its own average "
            "(Poisson probability below 0.1%).",
            body=data_table(anomalies, [("local_date", "Date", lambda d: f"{d:%a %b %-d}"),
                                        ("station", "Station", None), ("count", "Faults", None),
                                        ("expected", "Usual per day", lambda v: f"{v:.2f}"),
                                        ("p", "Probability", lambda v: "< 0.001" if v < 0.001 else f"{v:.3f}")],
                            empty="None: no station had an unusually bad day in this period.",
                            link_col=("station", "station_id"))))

    if uf:
        outl = uf.points[uf.points["outlier"]]
        interp = (f"Each extra point of black toner per day goes with {uf.slope:+.2f} failures a week "
                  f"(R² = {uf.r2:.2f}, {_p(uf.p_value)}, {uf.n} stations). ")
        interp += ("Usage explains a real share of the differences between stations." if uf.p_value < 0.05 else
                   "That isn't statistically significant: how busy a printer is doesn't explain how often it fails.")
        if len(outl):
            interp += " Highlighted: stations failing far more than their usage predicts, which suggests a hardware or setup problem."
        cards.append(chart_card(
            "Do busier printers fail more?", "Each dot is a station: usage (black toner burned per day) against "
            "red incidents per week, with an ordinary least squares line and its 95% confidence band.",
            charts.usage_scatter(theme, uf), wide=True, note=interp, graph_id={"type": "xg", "chart": "usage"},
            explain="If busy printers simply failed more, the dots would hug a rising line. Dots far above the "
                    "line fail more than their workload explains: look at the machine, not the traffic.",
            table=data_table(uf.points.sort_values("resid", ascending=False),
                             [("label", "Station", None), ("usage", "Toner/day (pts)", lambda v: f"{v:.2f}"),
                              ("failures_per_week", "Failures/week", lambda v: f"{v:.2f}"),
                              ("fitted", "Predicted", lambda v: f"{v:.2f}"),
                              ("resid", "Difference", lambda v: f"{v:+.2f}")], link_col=("label", "station_id"))))

    soon = eol.sort_values("days").head(20).copy()
    soon["window"] = soon.apply(eol_window, axis=1)
    cards.append(chart_card(
        "Consumable end-of-life forecast", "Regression (Theil-Sen) on each part's readings since its last "
        "replacement, with a 90% window. Open a station to see its fitted line.", wide=True,
        body=data_table(soon, [("station", "Station", None), ("label", "Part", None),
                               ("level", "Level", lambda v: f"{v:.0f}%"), ("window", "Replace in (90% window)", None),
                               ("r2", "Fit R²", lambda v: "—" if pd.isna(v) else f"{v:.2f}"),
                               ("method", "Method", None)], link_col=("station", "station_id"))))

    caveat = html.P("Statistical results describe the selected period and scope. On demo data the models mostly "
                    "rediscover patterns built into the simulator; on live data they become genuine findings as "
                    "history accumulates.", className="footnote")
    return [hl, html.Div(className="grid", children=cards), caveat]
