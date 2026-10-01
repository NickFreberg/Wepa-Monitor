"""Analytics: reliability, faults, consumables and data quality over a period, one tab each."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dash import html

from ... import config, metrics as M
from .. import charts
from ..components import (chart_card, data_table, fmt_hours, fmt_minutes, fmt_num, headline, metric_tile,
                          segmented, tile)
from .common import empty, needs_days, period_label, period_window, scope_ids

TABS = [{"label": "Reliability", "value": "reliability"}, {"label": "Faults", "value": "faults"},
        {"label": "Consumables", "value": "consumables"}, {"label": "Data quality", "value": "quality"}]
BURN_UNITS = {"per_day": "day", "per_week": "week", "per_month": "month", "per_year": "year"}


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
          "quality": _quality}.get(tab, _reliability)
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
        hl = headline("good" if avail.value >= 97 else "warning",
                      f"Printers were available {avail.value:.1f}% of the time over the last {plabel}",
                      _sentence("; ".join(detail)) if detail else "")
    else:
        hl = headline("info", "Not enough data yet for reliability figures", avail.note)

    tiles = html.Div(className="tiles", children=[
        metric_tile("Availability", avail, lambda v: f"{v:.2f}%",
                    "Observed printer-minutes not in red ÷ all observed printer-minutes. Unobserved time is excluded.",
                    unit_note=f"{avail.extra.get('down_h', 0):,.0f} printer-hours down" if avail.value else ""),
        metric_tile("Time to fix · down", mttr_r, fmt_minutes, "Mean time from a red status appearing to clearing.",
                    _n_note(mttr_r)),
        metric_tile("Time to fix · warning", mttr_y, fmt_minutes,
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

    return [hl, tiles, html.Div(className="grid", children=[
        chart_card("Daily availability", "Share of each day with the station able to print.",
                   *needs_days(fleet, lambda: charts.availability_daily(theme, fleet, by_sec)), wide=True,
                   table=data_table(fleet, [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}"),
                                            ("availability", "Availability", lambda v: f"{v:.2f}%"),
                                            ("observed_h", "Observed printer-h", lambda v: f"{v:,.1f}")])),
        chart_card("Building coverage", "Share of time at least one printer in the building could print.",
                   charts.building_bars(theme, b) if len(b) else None,
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
        chart_card("Station scorecard", "Worst availability first. Click a station for its full history.",
                   wide=True, body=data_table(sc, [
                       ("label", "Station", None), ("area", "Area", None),
                       ("availability", "Availability", lambda v: fmt_num(v, 2, "%")),
                       ("red_incidents", "Times down", None), ("mttr_red_min", "Time to fix", fmt_minutes),
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
        chart_card("Fault types", f"{len(faults):,} fault incidents.", charts.pareto(theme, counts),
                   table=data_table(counts.rename("n").reset_index(), [("label", "Fault", None),
                                                                       ("n", "Incidents", None)])),
        chart_card("When faults start", "By weekday and local hour.", charts.heatmap(theme, faults)),
        chart_card("Faults by building", "Top 12 buildings.", charts.pareto(theme, by_building)),
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
    hl = headline("info", f"The fleet used {k.loc['toner_k', 'used_units']:.1f} black toner cartridges' worth "
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
                   *needs_days(cum, lambda: charts.cumulative(theme, cum, ["toner_k", "toner_c", "toner_m", "toner_y"]))),
        chart_card("Cumulative drum use", "Parts' worth of drum life used.",
                   *needs_days(cum, lambda: charts.cumulative(theme, cum, ["drum_k", "drum_c", "drum_m", "drum_y"]))),
        chart_card("Cumulative belt and fuser use", "Parts' worth of belt and fuser life used.",
                   *needs_days(cum, lambda: charts.cumulative(theme, cum, ["belt", "fuser"]))),
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
