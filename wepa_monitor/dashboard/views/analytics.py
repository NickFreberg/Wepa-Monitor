"""Analytics: reliability, faults, supplies, usage, the report card, and the statistics behind them."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dash import dash_table, html

from ... import campus, config, insights, metrics as M, models, narrative as N, report_card as RC, support
from .. import charts
from ..components import (chart_card, data_table, explore_hint, fmt_hours, fmt_minutes, fmt_num, headline, metric_tile,
                          segmented, station_link, tile)
from ..theme import FONT, TOKENS
from .common import empty, needs_days, period_label, period_window, scope_ids

TABS = [{"label": "Reliability", "value": "reliability"}, {"label": "Faults", "value": "faults"},
        {"label": "Supplies", "value": "consumables"}, {"label": "Usage", "value": "usage"},
        {"label": "Report card", "value": "report"}, {"label": "Planning", "value": "planning"},
        {"label": "Forecasts & statistics", "value": "stats"}]
BURN_UNITS = {"per_day": "day", "per_week": "week", "per_month": "month", "per_year": "year"}
MTTR_UNITS = {"D": "Day", "W": "Week", "M": "Month"}


def owners_in(ds, ids) -> list[str]:
    st = ds.stations if ids is None else ds.stations[ds.stations["station_id"].isin(ids)]
    return [o for o in support.TEAMS if (st["owner"] == o).any()]


def _sentence(text: str) -> str:
    """Capitalize the first letter only (str.capitalize would lowercase station names)."""
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _hour(h: int) -> str:
    return f"{(h % 12) or 12} {'am' if h < 12 else 'pm'}"


def render(ds: M.Dataset, theme: str, scope, period, tab: str, burn_unit: str = "per_week", mttr_unit: str = "W"):
    if ds.empty:
        return empty("No data yet.")
    ids = scope_ids(ds, scope)
    if ids is not None and not ids:
        return empty("No stations in this scope.")
    start, end = period_window(ds, period)
    plabel = period_label(period)
    fn = {"reliability": _reliability, "faults": _faults, "consumables": _consumables, "usage": _usage,
          "report": _report, "planning": _planning, "stats": _stats}.get(tab, _reliability)
    out = fn(ds, theme, ids, start, end, plabel, {"burn": burn_unit, "mttr": mttr_unit})
    if fn is _faults and isinstance(out, list):
        out.append(html.Div(className="grid", children=[_weather_jams(ds, theme, ids, start, end, plabel)]))
    if fn is _reliability and isinstance(out, list):
        out.append(html.Div(className="grid", children=[_busy_downtime(ds, theme, ids, start, end, plabel),
                                                        _shared_outages(ds, ids, start, end, plabel)]))
    return out


def _weather_jams(ds, theme, ids, start, end, plabel):
    from ... import weather as W
    r = W.jams_vs_humidity(ds, start, end, ids)
    ok = r is not None and r.get("status") == "ok"
    return chart_card(
        "Does humid weather mean more paper jams?", f"Jams per unit of printing in dry, middle and humid hours "
        f"(outdoor humidity in Bridgewater), last {plabel}.",
        charts.humidity_bands(theme, r["bands"]) if ok else None,
        body=None if ok else empty(W.sentence(r), big=False), graph_id="weather-jams" if ok else None,
        story=[W.sentence(r)] if ok else None, icon_name=("droplet", "blue"),
        explain=["Damp paper curls and sticks, a common cause of jams. Each hour is sorted by Bridgewater's outdoor "
                 "humidity into thirds (dry, middle, humid); bars are jams per 100 points of toner used, so busy "
                 "hours don't count extra just for being busy.",
                 "Caveats: this is outdoor humidity (heating and air conditioning change it indoors), one reading "
                 "for the whole campus, and a link is not proof of a cause."],
        nerd=["Weather: Open-Meteo hourly relative humidity (ERA5 reanalysis for past days, forecast-model past "
              "days for the last week), CC BY 4.0. Humid vs dry thirds are compared with a Mantel-Haenszel rate "
              "ratio stratified by 6-hour block of the day x weekday/weekend, with toner used as exposure; 95% "
              "interval from the Greenland-Robins variance. Needs 14 days and 30 jams."]
             + ([f"This period: {r['hours']:,} hours, {r['jams']:,} jams; dry ≤ {r['dry_max']:.0f}% humidity, humid "
                 f"≥ {r['humid_min']:.0f}%."] if ok else []))


def _busy_downtime(ds, theme, ids, start, end, plabel):
    """Downtime weighted by how busy each printer usually is at that hour of the week."""
    from ... import impact
    t = impact.by_station(ds, start, end, ids)
    if t.empty:
        return chart_card("Which downtime cost the most printing?", f"Last {plabel}.",
                          body=empty("No downtime to weigh yet.", big=False))
    return chart_card(
        "Which downtime cost the most printing?", f"Hours down, and the same hours weighted by how busy that "
        f"printer usually is at that time of the week, last {plabel}.", charts.impact_bars(theme, t),
        graph_id="busy-downtime", story=[impact.summary(t)], icon_name=("clock", "crimson"),
        table=data_table(t, [("label", "Printer", None), ("down_h", "Hours down", lambda v: f"{v:,.1f}"),
                             ("weighted_h", "Busy-weighted hours", lambda v: f"{v:,.1f}"),
                             ("rank_plain", "Rank (plain)", None), ("rank_busy", "Rank (weighted)", None)],
                         link_col=("label", "station_id")),
        explain=["An hour down at noon on a busy printer costs students more printing than an hour down at 3 AM "
                 "on a quiet one. The colored bar weighs each down hour by how much that printer usually prints at "
                 "that hour of the week; gray is plain hours.",
                 "1 busy-weighted hour = an hour down for a typical printer at an average time. It estimates "
                 "lost printing from usage patterns; it is not a count of people."],
        nerd="weight = campus hour-of-week shape (toner used per printer-hour in each of 168 weekly hours, "
             "divided by the mean) × printer scale (its toner use per observed hour ÷ the median printer's), "
             "from the last 56 days. Down hours come from minute-level availability. A multiplicative model is "
             "used because one printer's hour-of-week cells are too sparse to estimate alone.")


def _shared_outages(ds, ids, start, end, plabel):
    """Several printers going down together: one shared cause (network, Wepa, a building) or chance?"""
    from ... import correlated as C
    c = C.clusters(ds, start, end, ids)
    story = [C.summary(c)]
    body = (data_table(c, [("start", "Started", lambda t: f"{t.tz_convert(config.LOCAL_TZ):%a %b %-d, %-I:%M %p}"), ("stations", "Printers", None),
                           ("building_list", "Buildings", None), ("causes", "What they reported", None),
                           ("kind", "Likely cause", None),
                           ("together", "Came back together", lambda v: "Yes" if v else "No"),
                           ("verdict", "Verdict", None)], max_rows=15)
            if len(c) else empty("Nothing to show: no group of printers went out of service within "
                                 f"{C.WINDOW_MIN} minutes of each other.", big=False))
    return chart_card(
        "Did several printers go out of service together?", f"Groups of {C.MIN_STATIONS}+ printers going down within "
        f"{C.WINDOW_MIN} minutes of each other, last {plabel}.", body=body, story=story, wide=True,
        icon_name=("pulse", "crimson"),
        explain=["When printers in different buildings drop off at the same moment, the cause is usually shared "
                 "(the campus network, Wepa's service, a building's power), not each printer. Those are worth "
                 "reporting to Network Services or Wepa rather than visiting each printer.",
                 "Timing alone isn't proof: a group whose printers reported unrelated problems (one jam, one out "
                 "of paper) is listed as a busy moment, not a shared cause."],
        nerd=[f"Outage starts are grouped when they fall within {C.WINDOW_MIN} minutes of the group's first start. "
              "Chance check: if outages started independently, starts per window would be Poisson at the rate "
              "seen for that hour (weekdays and weekends separately). The expected number of windows in the "
              "period with at least this many starts by coincidence is summed over every window; under 0.05 is "
              "'very unlikely to be coincidence'. Labels come from what the printers reported (mostly 'not "
              f"reachable' = network or Wepa) and whether they recovered within {C.WINDOW_MIN} minutes of each "
              "other."])


def _n_note(m, word="incidents"):
    return f"n = {m.n:,} {word}" + (f" · median {fmt_minutes(m.extra['median'])}" if "median" in m.extra else "")


def mttr_frame(ds, start, end, ids, unit: str) -> pd.DataFrame:
    """Median/mean minutes to clear, per day, week or month (local time), by severity."""
    inc = M._resolved(M._in(ds.sev_inc, "start", start, end, ids)).copy()
    if inc.empty:
        return pd.DataFrame(columns=["period", "severity", "median_min", "mean_min", "n"])
    inc["period"] = inc["start"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None).dt.to_period(unit).dt.start_time
    return (inc.groupby(["period", "severity"])["duration_s"]
            .agg(median_min=lambda s: s.median() / 60, mean_min=lambda s: s.mean() / 60, n="size").reset_index())


# --- Reliability -------------------------------------------------------------------------------

def _reliability(ds, theme, ids, start, end, plabel, opts):
    avail = M.availability(ds, start, end, ids)
    mttr_r = M.mttr(ds, "red", start, end, ids)
    mttr_y = M.mttr(ds, "yellow", start, end, ids)
    mtbf = M.mtbf(ds, start, end, ids)
    paper = M.paper_refill_time(ds, start, end, ids)
    tray = M.tray_empty_time(ds, start, end, ids)
    drivers = insights.downtime_drivers(ds, start, end, ids)

    if avail.value is not None:
        detail = []
        if len(drivers["cause"]):
            c = drivers["cause"].iloc[0]
            detail.append(f"{c['label'].lower()} cost the most printing time ({c['share']:.0%} of it)")
        if len(drivers["station"]):
            st = drivers["station"].iloc[0]
            detail.append(f"{st['label']} lost the most ({st['down_h']:,.0f} printer-hours)")
        if mttr_r.value is not None:
            detail.append(f"a typical outage took {fmt_minutes(mttr_r.extra['median'])} to fix")
        hl = headline(N.availability_tone(avail.value),
                      f"Printers could print {avail.value:.1f}% of the time over the last {plabel}",
                      _sentence("; ".join(detail)) if detail else "")
    else:
        hl = headline("info", "Not enough data yet for reliability figures", avail.note)

    tiles = html.Div(className="tiles", children=[
        metric_tile("Availability", avail, lambda v: f"{v:.2f}%",
                    "Observed printer-minutes not in red ÷ all observed printer-minutes. Unobserved time is excluded.",
                    unit_note=f"{avail.extra.get('down_h', 0):,.0f} printer-hours down" if avail.value else ""),
        metric_tile("Mean time to repair (MTTR) · down", mttr_r, fmt_minutes,
                    "Mean time from a red status appearing to clearing.", _n_note(mttr_r)),
        metric_tile("Mean time to clear · warnings", mttr_y, fmt_minutes,
                    "Mean time from a yellow status appearing to clearing.", _n_note(mttr_y)),
        metric_tile("Mean time between failures (MTBF)", mtbf, fmt_hours, "Printer-hours of uptime per red incident.",
                    f"{mtbf.n:,} failures"),
        metric_tile("Paper refill time", paper, fmt_minutes, "Mean duration of 'out of paper' (station can't print).",
                    _n_note(paper, "paper-outs")),
        metric_tile("Tray empty time", tray, fmt_minutes,
                    "Mean duration of a single tray reporting empty, even while another tray keeps printing.",
                    f"{tray.extra.get('total_h', 0):,.0f} tray-hours in total"),
    ])

    fleet = M.availability_daily(ds, start, end, ids)
    by_sec = M.availability_daily(ds, start, end, ids, by="section")
    prof = M.availability_by_hour(ds, start, end, ids)
    b = M.building_availability(ds, start, end, ids)
    unit = opts.get("mttr") or "W"
    trend = mttr_frame(ds, start, end, ids, unit)
    cal = campus.load()

    # Through the day: the weakest hour, and whether it falls outside desk hours.
    hour_story = None
    wd = prof[prof["daytype"] == "Weekdays"]
    if len(wd) >= 12:
        worst = wd.loc[wd["availability"].idxmin()]
        best = wd.loc[wd["availability"].idxmax()]
        if best["availability"] - worst["availability"] >= 0.5:
            hour_story = ["On weekdays printers are least likely to work around ", ("b", _hour(int(worst["hour"]))),
                          f" ({worst['availability']:.1f}%) and most likely around {_hour(int(best['hour']))} "
                          f"({best['availability']:.1f}%). "]
            owners = owners_in(ds, ids)
            open_any = any(support.team(o)["hours"].get(0, (99, 99))[0] <= worst["hour"] <
                           support.team(o)["hours"].get(0, (0, 0))[1] for o in owners)
            hour_story.append("That low point falls inside desk hours, so it's about load, not coverage."
                              if open_any else "That low point is outside desk hours: problems that start in the "
                              "evening wait until morning.")
        else:
            hour_story = ["Availability is about the same at every hour of the day."]

    d_story = None
    if len(drivers["cause"]):
        c = drivers["cause"]
        d_story = ["Most lost printing time came from ", ("b", c.iloc[0]["label"].lower()),
                   f" ({c.iloc[0]['share']:.0%})" + (f", then {c.iloc[1]['label'].lower()} ({c.iloc[1]['share']:.0%})"
                                                     if len(c) > 1 else "") + ". "]
        desk = drivers["desk"].set_index("key")
        if "Began after desk hours" in desk.index:
            d_story.append(f"{desk.loc['Began after desk hours', 'share']:.0%} of it was outages that began after the "
                           "desk had closed.")

    red_all = M._in(ds.sev_inc, "start", start, end, ids)
    red_all = red_all[red_all["severity"] == "red"]
    summ = support.summary(red_all)
    desk_note = ""
    if len(summ):
        worst_o = summ.sort_values("after_share", ascending=False).iloc[0]
        tot_after = summ["after_h"].sum() / max(1e-9, (summ["after_h"] + summ["staffed_h"]).sum())
        desk_note = (f"{tot_after:.0%} of all downtime fell outside desk hours. {worst_o['owner']}: "
                     f"{worst_o['after_hours']} of {worst_o['outages']} outages began after hours.")
    desk_card = chart_card(
        "Downtime vs. support desk hours", "Printer-hours down, split by whether the owning desk was staffed. "
        + " · ".join(f"{o}: {support.hours_text(o)}" for o in summ["owner"]) if len(summ) else "",
        charts.owner_hours(theme, summ) if len(summ) else None, graph_id={"type": "xg", "chart": "owner_hours"},
        body=None if len(summ) else empty("No outages in this period.", big=False),
        story=[desk_note] if desk_note else None,
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

    mttr_story = None
    reds = trend[trend["severity"] == "red"]
    if len(reds) >= 2:
        a0, a1 = reds.iloc[-2]["median_min"], reds.iloc[-1]["median_min"]
        word = MTTR_UNITS.get(unit, "Week").lower()
        mttr_story = [f"The latest {word}'s typical outage took ", ("b", fmt_minutes(a1)),
                      f" to fix, {'down' if a1 < a0 else 'up'} from {fmt_minutes(a0)} the {word} before."]

    return [hl, explore_hint(), tiles, html.Div(className="grid", children=[
        chart_card("Daily availability", "Share of each day the stations could print.",
                   *needs_days(fleet, lambda: charts.add_calendar(charts.availability_daily(theme, fleet, by_sec),
                                                                  theme, cal.days, start, end)), wide=True,
                   graph_id={"type": "xg", "chart": "avail_daily"},
                   explain=["Each line is the share of a day that stations could print. The dark line is all BSU "
                            "stations; colored lines split it by section.",
                            "Dips are days with outages. Click a point to see which stations caused it. Shaded "
                            "bands come from BSU's academic calendar (finals, breaks, move-in, summer); dotted "
                            "lines are holidays."],
                   table=data_table(fleet, [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}"),
                                            ("availability", "Availability", lambda v: f"{v:.2f}%"),
                                            ("observed_h", "Observed printer-h", lambda v: f"{v:,.1f}")])),
        chart_card("Through the day", "Share of the time printers could print, by hour, weekdays vs weekends.",
                   charts.hourly_availability(theme, prof, owners_in(ds, ids)) if len(prof) else None,
                   body=None if len(prof) else empty("Not enough data yet.", big=False), story=hour_story,
                   graph_id={"type": "xg", "chart": "avail_hourly"},
                   explain=["Each point averages every day in the period at that hour. The shaded band is when the "
                            "support desks are staffed on weekdays.",
                            "A dip that starts at closing time and recovers when the desk opens means problems "
                            "wait overnight: that's a coverage question, not a repair-speed one."],
                   table=data_table(prof, [("daytype", "Days", None), ("hour", "Hour", lambda h: _hour(int(h))),
                                           ("availability", "Availability", lambda v: f"{v:.2f}%"),
                                           ("down_h", "Printer-h lost", lambda v: f"{v:,.1f}")])),
        chart_card("What cost the most printing time", "Printer-hours lost to outages, by main cause.",
                   charts.driver_bars(theme, drivers["cause"]) if len(drivers["cause"]) else None,
                   body=None if len(drivers["cause"]) else empty("No outages in this period.", big=False),
                   story=d_story, graph_id={"type": "xg", "chart": "drivers"},
                   explain="Each outage counts for as long as it lasted (within the period), under the problem that "
                           "caused it. Long outages weigh more than frequent short ones, because that's what "
                           "students feel.",
                   table=data_table(drivers["station"].head(15), [
                       ("label", "Station", None), ("down_h", "Printer-h lost", lambda v: f"{v:,.1f}"),
                       ("outages", "Outages", None), ("share", "Share", lambda v: f"{v:.0%}")])),
        chart_card("Building coverage", "Share of time at least one printer in the building could print.",
                   charts.building_bars(theme, b) if len(b) else None, graph_id={"type": "xg", "chart": "building_cov"},
                   explain="Longer bars are better. A building with two printers can stay covered while one is "
                           "down; a single-printer building can't. Click a bar for the details.",
                   body=None if len(b) else empty("No data.", big=False),
                   table=data_table(b, [("building", "Building", None), ("stations", "Stations", None),
                                        ("any_up", "Any printer up", lambda v: f"{v:.2f}%"),
                                        ("all_up", "All printers up", lambda v: f"{v:.2f}%")])),
        chart_card("Mean time to repair (MTTR), over time", "Typical (median) minutes from alert to fixed.",
                   charts.mttr_weekly(theme, trend) if len(trend) else None, story=mttr_story,
                   action=segmented("mttr-unit", [{"label": v, "value": k} for k, v in MTTR_UNITS.items()], unit),
                   body=None if len(trend) else empty("No resolved incidents yet.", big=False),
                   nerd="Median rather than mean, because a few multi-day outages would otherwise dominate. Only "
                        "outages with a known start and end count (ones already under way when monitoring began, "
                        "or that the monitor lost track of, are excluded).",
                   table=data_table(trend, [("period", "Starting", lambda d: f"{d:%Y-%m-%d}"),
                                            ("severity", "Severity", None), ("median_min", "Median", fmt_minutes),
                                            ("mean_min", "Mean", fmt_minutes), ("n", "Incidents", None)])),
        desk_card,
        phase_card,
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
    hl = headline("info", f"{counts.index[0]} is the most common problem: {counts.iloc[0]} of {len(faults)} "
                          f"in the last {plabel}",
                  f"Problems cluster between {_hour(h0)} and {_hour((h0 + 3) % 24)} "
                  f"({window[h0] / len(faults):.0%} of them). {by_building.index[0]} had the most "
                  f"({by_building.iloc[0]}).")
    by_station = faults.groupby(["station_id", "station"]).size().rename("n").reset_index() \
        .sort_values("n", ascending=False)
    detail = faults.assign(d=faults["detail"].fillna("")).groupby("label")["d"].agg(
        lambda s: ", ".join(pd.Series([x for v in s for x in v.split(", ") if x]).value_counts().head(3).index))
    types = counts.rename("n").reset_index().assign(
        share=lambda d: d["n"] / d["n"].sum(), where=lambda d: d["label"].map(detail).fillna(""),
        fix=lambda d: d["label"].map(faults.drop_duplicates("label").set_index("label")["fix_category"]))
    top_story = [("b", counts.index[0]), f" accounts for {counts.iloc[0] / len(faults):.0%} of problems"]
    if detail.get(counts.index[0]):
        top_story.append(f", most often at the {detail[counts.index[0]]}")
    top_story.append(".")
    return [hl, html.Div(className="grid", children=[
        chart_card("Fault types", f"{len(faults):,} problems, grouped by what actually went wrong.",
                   charts.pareto(theme, counts), graph_id={"type": "xg", "chart": "fault_types"}, story=top_story,
                   explain="Each bar counts how often a problem started. Click one to see what it means on the "
                           "floor, where it happens most, and how to fix it.",
                   nerd="Types combine Wepa's status codes with the printer's own messages: a 'printer down' code "
                        "that arrives with 'Paper Feed Jam' counts as a paper jam, and a generic 'printer down' "
                        "remains only when nothing more specific explains it. One episode = one continuous run of "
                        "that problem at one station.",
                   table=data_table(types, [("label", "Problem", None), ("n", "Times", None),
                                            ("share", "Share", lambda v: f"{v:.0%}"),
                                            ("where", "Most often (detail)", None), ("fix", "Fix", None)])),
        chart_card("When problems start", "By weekday and local hour. Outlines mark support desk hours.",
                   charts.heatmap(theme, faults, owners_in(ds, ids)),
                   graph_id={"type": "xg", "chart": "heatmap"}, explain="Darker squares mean more problems started in "
                   "that hour. The outlined blocks are when ResNet and the IT Service Center are staffed; dark "
                   "squares outside them wait for the next shift. Click a square for details."),
        chart_card("Problems by building", "Top 12 buildings.", charts.pareto(theme, by_building),
                   graph_id={"type": "xg", "chart": "faults_building"}),
        chart_card("Problems by station", "Click a station for its incident history.", body=data_table(
            by_station, [("station", "Station", None), ("n", "Problems", None)], max_rows=40,
            link_col=("station", "station_id"))),
    ])]


# --- Supplies --------------------------------------------------------------------------------

def _consumables(ds, theme, ids, start, end, plabel, opts):
    burn_unit = opts.get("burn") or "per_week"
    burn = M.burn_rates(ds, start, end, ids)
    unit = BURN_UNITS.get(burn_unit, "week")
    burn["rate_pts"] = burn[burn_unit]
    burn["rate_units"] = burn[burn_unit] / 100
    cum = M.cumulative_usage(ds, start, end, ids)
    repl = M.replacements(ds, start, end, ids)
    k = burn.set_index("component")
    hl = headline("info", f"BSU print stations used {k.loc['toner_k', 'used_units']:.1f} black toner cartridges' worth "
                          f"in the last {plabel}",
                  f"At this rate that's about {k.loc['toner_k', 'per_month'] / 100:.1f} black cartridges a month"
                  + f". {len(repl)} parts were replaced in this period.")
    toner_repl = repl[repl["component"].str.startswith("toner")]
    repl_note = (f"Toner was replaced at an average of {toner_repl['level_before'].mean():.1f}% remaining "
                 f"across {len(toner_repl)} swaps." if len(toner_repl) else "")
    ok = burn[np.isfinite(burn["rate_units"])]
    use_story = None
    if len(ok):
        top = ok.sort_values("rate_units", ascending=False).iloc[0]
        use_story = [f"Per {unit}, BSU uses about ", ("b", f"{k.loc['toner_k', 'rate_units']:.1f} black toner"),
                     " cartridges' worth" + (f" and {k.loc[['toner_c', 'toner_m', 'toner_y'], 'rate_units'].sum():.1f} "
                                            "color" if 'toner_c' in k.index else "") +
                     f". Of everything, {top['label']} goes fastest."]
    return [hl, html.Div(className="grid", children=[
        chart_card(f"Supplies used per {unit}", "Parts' worth used across the selected stations, e.g. 1.5 = one and "
                   "a half cartridges.", charts.consumption_bars(theme, burn, unit) if len(ok) else None,
                   body=None if len(ok) else empty("Usage rates need at least "
                                                   f"{config.MIN_DAYS_FOR_BURN_RATE} days of readings.", big=False),
                   story=use_story, wide=True,
                   action=segmented("burn-unit", [{"label": v.capitalize(), "value": k2}
                                                  for k2, v in BURN_UNITS.items()], burn_unit),
                   nerd=["Usage = drops in each part's level percentage between readings, summed; a jump up of "
                         f"{config.REPLACEMENT_JUMP_PTS}+ points is a replacement, never negative use. 100 points = "
                         "one part.", "Rates divide by the days each station was actually observed (at least "
                         f"{config.MIN_DAYS_FOR_BURN_RATE}), then add up across stations."],
                   table=data_table(burn, [
                       ("label", "Part", None), ("used_units", "Used in period (parts)", lambda v: fmt_num(v, 2)),
                       ("rate_units", f"Per {unit} (parts)", lambda v: fmt_num(v, 2)),
                       ("rate_pts", f"Per {unit} (pts)", lambda v: fmt_num(v, 1)),
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
        chart_card("Replacement log", "Every part swap spotted, with how much was left in the old one.",
                   wide=True, note=repl_note,
                   nerd=f"A level jump of {config.REPLACEMENT_JUMP_PTS}+ points between two readings counts as a "
                        "replacement. 'Level before' is the last reading of the old part.",
                   body=data_table(repl, [
                       ("ts", "When", lambda d: f"{d.tz_convert(config.LOCAL_TZ):%b %-d, %-I:%M %p}"),
                       ("station", "Station", None), ("label", "Part", None),
                       ("level_before", "Level before", lambda v: f"{v:.0f}%"),
                       ("level_after", "Level after", lambda v: f"{v:.0f}%")], max_rows=60,
                       empty="No replacements detected in this period.", link_col=("station", "station_id"))),
    ])]


# --- Usage --------------------------------------------------------------------------------------------

def _usage(ds, theme, ids, start, end, plabel, _):
    u = M.usage_by_station(ds, start, end, ids)
    rated = u[u["relative"].notna()]
    if rated.empty:
        return [headline("info", "Not enough history yet to compare usage",
                         f"Each printer needs at least {config.MIN_DAYS_FOR_BURN_RATE} days of toner readings.")]
    top, low = rated.head(3), rated.tail(3).iloc[::-1]

    def card(r, rank, busy):
        return html.Div(className=f"rank rank--{'busy' if busy else 'quiet'}", children=[
            html.Div(f"#{rank}", className="rank__n"),
            html.Div([station_link(r["station_id"], r["description"], "rank__name"),
                      html.Div(r["building"], className="rank__meta"),
                      html.Div([html.B(f"{r['relative']:.1f}×"), " the typical printer · ",
                                f"~{r['cartridges_per_month']:.1f} black cartridges/month"], className="rank__stat"),
                      html.Div("Only printer in the building" if r["printers_in_building"] == 1 else
                               f"{int(r['printers_in_building'])} printers in the building", className="rank__meta")])])

    ideas = []
    for _, r in top.iterrows():
        if r["printers_in_building"] == 1:
            ideas.append([("b", r["building"]), f" is one of the busiest ({r['relative']:.1f}× typical) with a single "
                          "printer: a second printer there would shorten queues and keep students printing during an "
                          "outage."])
    for _, r in low.iterrows():
        if r["printers_in_building"] > 1:
            ideas.append([("b", r["description"]), f" is one of the quietest ({r['relative']:.1f}×) and shares "
                          f"{r['building']} with another printer: a candidate to relocate somewhere busier."])
        elif r["relative"] < 0.35:
            ideas.append([("b", r["description"]), f" sees little use ({r['relative']:.1f}×). Before removing it, "
                          "check whether it's the only printer students in that building can reach."])
    if not ideas:
        ideas.append(["Usage is spread fairly evenly: no printer stands out as overloaded or idle."])
    spread = rated["relative"].max() / max(rated["relative"].min(), 0.01)
    hl = headline("info", f"{top.iloc[0]['description']} is the busiest printer: {top.iloc[0]['relative']:.1f}× "
                          f"the typical one over the last {plabel}",
                  f"The busiest prints about {spread:.0f}× as much as the quietest ({low.iloc[0]['description']}).")
    return [hl, html.Div(className="grid", children=[
        chart_card("Busiest three", "Most toner burned per day.", body=html.Div(
            [card(r, i, True) for i, (_, r) in enumerate(top.iterrows(), start=1)], className="ranks")),
        chart_card("Quietest three", "Least toner burned per day.", body=html.Div(
            [card(r, len(rated) - i + 1, False) for i, (_, r) in enumerate(low.iterrows(), start=1)], className="ranks")),
        chart_card("What it suggests", "Starting points for decisions about adding, moving or removing printers.",
                   wide=True, body=html.Ul([html.Li(prose_line(x)) for x in ideas], className="observations"),
                   nerd="Wepa doesn't publish page counts, so usage is black toner burned per day of observation "
                        "(almost every page uses black). It ranks printers reliably but isn't a page total: a page "
                        "of dense text uses more toner than a page with a few lines."),
        chart_card("Every printer, busiest to quietest", "Usage compared with the typical (median) printer.",
                   charts.usage_bars(theme, u), wide=True, graph_id={"type": "xg", "chart": "usage_bars"},
                   explain="1× is the typical printer. Crimson bars are the three busiest; gold the three quietest. "
                           "Busy isn't bad: the Report card tab judges reliability for each printer's workload.",
                   table=data_table(u, [("label", "Station", None),
                                        ("usage_per_day", "Black toner pts/day", lambda v: fmt_num(v, 2)),
                                        ("color_per_day", "Color pts/day", lambda v: fmt_num(v, 2)),
                                        ("relative", "vs typical", lambda v: fmt_num(v, 1, "×")),
                                        ("printers_in_building", "Printers in building", None)],
                                    link_col=("label", "station_id"))),
    ])]


def _planning(ds, theme, ids, start, end, plabel, _):
    from . import planning
    return planning.render(ds, theme, ids, start, end, plabel)


def prose_line(segments):
    from ..components import prose
    return prose([segments], className="prose prose--inline")


# --- Report card --------------------------------------------------------------------------------------

GRADE_COLORS = {"A": "#0f8c62", "B": "#5a9e3a", "C": "#a87405", "D": "#c4621d", "F": "#b5122b"}


def _report(ds, theme, ids, start, end, plabel, _):
    card = RC.build(ds, start, end, ids)
    if card.empty:
        return empty("No stations to grade.")
    graded = card[card["score"].notna()]
    if graded.empty:
        return [headline("info", "Not enough history to grade printers yet",
                         f"Grades need at least {RC.MIN_DAYS} days of observation per printer, so one bad hour can't "
                         "brand a printer a problem.")]
    counts = graded["grade"].value_counts()
    problems = graded[graded["grade"].isin(["D", "F"])]
    best = graded.sort_values("score", ascending=False).iloc[0]
    hl = headline("warning" if counts.get("F", 0) else "good" if not len(problems) else "info",
                  f"{counts.get('A', 0) + counts.get('B', 0)} of {len(graded)} printers earn an A or B; "
                  f"{len(problems)} need attention",
                  f"Best: {best['label']} ({best['grade']}, {best['score']:.0f}). "
                  + (f"Most urgent: {problems.iloc[0]['label']} ({problems.iloc[0]['grade']}): "
                     f"{problems.iloc[0]['why'][:1].lower() + problems.iloc[0]['why'][1:]}" if len(problems) else ""))
    tiles = html.Div(className="tiles tiles--grades", children=[
        tile(f"Grade {g}", str(counts.get(g, 0)), w, help_text=f"Score {lo}+" if lo else "Below 60")
        for lo, g, w in RC.GRADES])
    t = TOKENS[theme]
    rows = card.assign(
        score_r=card["score"].round(0), avail=card["availability"].round(1), outw=card["outages_per_week"].round(2),
        fr=card["faults_ratio"].round(2), cr=card["wear_ratio"].round(2), use=card["usage_relative"].round(1),
        cost=card["parts_per_month"].round(2), warn=(card["warning_share"] * 100).round(1),
        station=card["label"], link=card["station_id"].map(lambda s: f"[Open](/station/{s})"))
    cols = [{"name": "Station", "id": "station"}, {"name": "Grade", "id": "grade"},
            {"name": "Score", "id": "score_r", "type": "numeric"}, {"name": "Verdict", "id": "verdict"},
            {"name": "Main reason", "id": "why"}, {"name": "Available %", "id": "avail", "type": "numeric"},
            {"name": "Outages / week", "id": "outw", "type": "numeric"},
            {"name": "Faults vs campus*", "id": "fr", "type": "numeric"},
            {"name": "Parts wear vs campus*", "id": "cr", "type": "numeric"},
            {"name": "Usage vs typical", "id": "use", "type": "numeric"},
            {"name": "Drums/belt/fuser per month", "id": "cost", "type": "numeric"},
            {"name": "In warning %", "id": "warn", "type": "numeric"},
            {"name": "Area", "id": "area"}, {"name": "Supported by", "id": "owner"},
            {"name": "", "id": "link", "presentation": "markdown"}]
    table = dash_table.DataTable(
        id="rc-table", data=rows[[c["id"] for c in cols]].to_dict("records"), columns=cols,
        sort_action="native", filter_action="native", page_action="none", markdown_options={"link_target": "_self"},
        sort_by=[{"column_id": "score_r", "direction": "asc"}],
        style_table={"overflowX": "auto", "maxHeight": "640px", "overflowY": "auto"}, fixed_rows={"headers": True},
        style_header={"backgroundColor": t["surface"], "color": t["muted"], "fontWeight": 600, "fontSize": "12px",
                      "border": "none", "borderBottom": f"1px solid {t['border']}", "whiteSpace": "normal"},
        style_filter={"backgroundColor": t["page"], "color": t["ink"]},
        style_cell={"backgroundColor": t["surface"], "color": t["ink"], "border": "none",
                    "borderBottom": f"1px solid {t['border']}", "fontFamily": FONT, "fontSize": "13px", "padding": "8px 10px", "textAlign": "left", "minWidth": "80px",
                    "maxWidth": "320px", "whiteSpace": "normal", "height": "auto"},
        style_cell_conditional=[{"if": {"column_id": c}, "textAlign": "right"} for c in
                                ("score_r", "avail", "outw", "fr", "cr", "use", "cost", "warn")] +
                               [{"if": {"column_id": "station"}, "fontWeight": 650, "minWidth": "170px"},
                                {"if": {"column_id": "why"}, "minWidth": "240px"}],
        style_data_conditional=[{"if": {"filter_query": f'{{grade}} = "{g}"', "column_id": "grade"},
                                 "backgroundColor": c, "color": "#ffffff", "fontWeight": 800, "textAlign": "center"}
                                for g, c in GRADE_COLORS.items()],
        css=[{"selector": ".dash-filter input", "rule": "font-size: 12px;"}])
    weights = ", ".join(f"{RC.PART_LABEL[k].lower()} {w:.0%}" for k, w in RC.WEIGHTS.items())
    return [hl, tiles, html.Div(className="grid", children=[
        chart_card("Station report card", "Every printer, worst first. Click a column to sort; type in the row under "
                   "the headings to filter (e.g. F in Grade, or Weygand in Station).", wide=True, body=table,
                   story=["A busy printer isn't a bad printer: grades reward being able to print and penalize going "
                          "down, faulting, and wearing out parts ", ("b", "more than its workload explains"), "."],
                   explain=["Grades: A 90+ (great), B 80+ (good), C 70+ (fair), D 60+ (needs attention), F below 60 "
                            "(a problem). 'Main reason' names the biggest drag on the grade.",
                            "* 'vs campus' compares each printer with the campus average for the same amount of "
                            "printing: 1.0 is average, 2.0 is twice as often (or as fast)."],
                   nerd=[f"Score = {weights}.",
                         "Availability: 85% scores 0, 99.5% scores 100 (linear). Outages: 4+ a week scores 0, none "
                         "scores 100. Warnings: 25%+ of the time scores 0.",
                         "Workload-adjusted faults and parts wear (drums, belt and fuser used, in parts): each "
                         "printer's rate per black-toner point, shrunk toward "
                         f"the campus rate with the weight of {RC.PRIOR_USE:.0f} points of use (an empirical-Bayes "
                         "style prior), divided by the campus rate. Half the campus rate or better scores 100; equal "
                         "scores 80; double 40; triple 0.",
                         f"Printers observed under {RC.MIN_DAYS} days get no grade. No prices are used: printer "
                         "models and part sources vary, so wear is counted in parts, not dollars."]),
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
                            f"(typically {night:.1f} hours vs {day:.1f})")
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
            "How long outages last", "The share of outages still not fixed after each hour, depending on whether "
            "they began while the desk was open.",
            charts.km_curves(theme, ttf), wide=True, note=verdict, graph_id={"type": "xg", "chart": "km"},
            nerd=["Kaplan-Meier survival curves; outages still open are right-censored rather than dropped.",
                  "Groups compared with a log-rank test. p < 0.05 means a difference this big would rarely happen "
                  "by chance if staffing made no difference."],
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
            "Is today normal?", "Problems per day against the normal range. A day above the red line, or a long "
            "run on one side of the average, means something really changed.",
            charts.control_chart(theme, cc), body=sig, graph_id={"type": "xg", "chart": "cchart"},
            nerd="Shewhart c-chart for counts: center = mean daily faults, limits = mean ± 3√mean (Poisson). "
                 "Western Electric run rule: 8 consecutive days on one side of the center line.",
            explain="Points inside the limits are normal day-to-day variation, so don't chase them. A point "
                    "above the red line, or a long run on one side of the average, means something changed.",
            table=data_table(cc.daily, [("local_date", "Date", lambda d: f"{d:%a %b %-d}"),
                                        ("count", "Faults", None)])))
        cards.append(chart_card(
            "Unusual station-days", "Days when a station had far more problems than its own normal.",
            nerd="Each station's daily count is compared with a Poisson distribution at its own mean rate; days with "
                 "an upper-tail probability below 0.1% are listed.",
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
            "Do busier printers fail more?", "Each dot is a station: how much it prints against how often it "
            "goes down. Dots far above the line fail more than their workload explains.",
            charts.usage_scatter(theme, uf), wide=True,
            nerd="Ordinary least squares fit with a 95% confidence band for the mean; R² and the slope's p-value are "
                 "in the note. Highlighted: residual more than 2 residual standard deviations above the line.", note=interp, graph_id={"type": "xg", "chart": "usage"},
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
        "Consumable end-of-life forecast", "When each part is expected to need replacing, soonest first, with "
        "a likely range. Open a station to see its trend line.", wide=True,
        nerd="Theil-Sen regression (median of pairwise slopes, robust to sensor blips) on each part's readings since "
             "its last replacement; the 90% window comes from the slope's confidence interval.",
        body=data_table(soon, [("station", "Station", None), ("label", "Part", None),
                               ("level", "Level", lambda v: f"{v:.0f}%"), ("window", "Replace in (90% window)", None),
                               ("r2", "Fit R²", lambda v: "—" if pd.isna(v) else f"{v:.2f}"),
                               ("method", "Method", None)], link_col=("station", "station_id"))))

    from . import planning, risk_view
    cards = risk_view.model_card(ds, theme) + cards
    cards += planning.stats_extras(ds, theme, ids, start, end, plabel)
    caveat = html.P("Statistical results describe the selected period and scope. On demo data the models mostly "
                    "rediscover patterns built into the simulator; on live data they become genuine findings as "
                    "history accumulates.", className="footnote")
    return [hl, html.Div(className="grid", children=cards), caveat]
