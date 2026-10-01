"""Page renderers: Operations, Management and Executive summary."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dash import html

from .. import config, geo, insights, metrics as M, ops, rules
from . import charts
from .components import (chart_card, data_table, fmt_hours, fmt_minutes, fmt_num, level_bar, metric_tile,
                         status_pill, tile)

AREA_ORDER = ["Upper Great Hill", "University Park", "West Side", "TBD", "Academic", "Satellite", "Unassigned"]
KIND_LABEL = {"red": "Down", "yellow": "Warning", "tray": "Tray empty", "consumable_now": "Part due now",
              "consumable_soon": "Part due soon", "stale": "No data"}
KIND_TONE = {"red": "critical", "yellow": "warning", "stale": "serious", "tray": "info",
             "consumable_now": "warning", "consumable_soon": "info"}


def _ids(ds: M.Dataset, sections, areas):
    if not sections and not areas:
        return None
    return ds.ids(section=sections or None, area=areas or None)


def _empty(msg: str) -> html.Div:
    return html.Div(msg, className="empty empty--page")


def _needs_days(frame: pd.DataFrame, fig_fn, min_days: int = 2):
    """Daily trend charts only once there are at least `min_days` days to draw a line between."""
    if frame is None or frame["local_date"].nunique() < min_days:
        return None, html.Div(f"Collecting data - this trend appears once {min_days} days are recorded.",
                              className="empty")
    return fig_fn(), None


# ---------------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------------

def _station_card(row: pd.Series, levels: dict) -> html.Div:
    msgs = [rules.code_label(c) for c in str(row["status_codes"]).split(",") if c]
    msgs += [m for m in str(row["printer_text"]).split(" | ") if m]
    drum = [levels.get(c) for c in ("drum_k", "drum_c", "drum_m", "drum_y") if levels.get(c) is not None]
    return html.Div(className=f"station station--{row['state']}", children=[
        html.Div(className="station__head", children=[
            html.Div([html.Div(row["description"], className="station__name"),
                      html.Div(f"#{row['station_id']} · {row['building']}", className="station__meta")]),
            status_pill(row["state"]),
        ]),
        html.Ul([html.Li(m) for m in msgs], className="station__msgs") if msgs else
        html.Div("No alerts", className="station__ok"),
        html.Div(className="station__levels", children=[
            level_bar("K", levels.get("toner_k")), level_bar("C", levels.get("toner_c")),
            level_bar("M", levels.get("toner_m")), level_bar("Y", levels.get("toner_y")),
            level_bar("Drum", min(drum) if drum else None),
            level_bar("Belt", levels.get("belt"), low=5, critical=2),
            level_bar("Fuser", levels.get("fuser"), low=5, critical=2),
        ]),
    ])


def render_ops(ds: M.Dataset, theme: str, sections, areas, basemap: str = "street"):
    if ds.empty:
        return _empty("No snapshots yet. Run `python -m wepa_monitor collect` (live) or "
                      "`python -m wepa_monitor demo` (synthetic) and reload.")
    ids = _ids(ds, sections, areas)
    cur = ops.current_status(ds, ids)
    if cur.empty:
        return _empty("No stations match these filters.")
    n = len(cur)
    red, yel, stale = (cur["state"] == "red").sum(), (cur["state"] == "yellow").sum(), cur["stale"].sum()
    a24 = M.availability(ds, ds.as_of - pd.Timedelta(days=1), ds.as_of, ids)
    queue = ops.work_queue(ds, ids)
    reds = queue[queue["kind"] == "red"]
    parts = queue[queue["kind"].isin(["consumable_now", "consumable_soon"])]["station_id"].nunique()

    tiles = html.Div(className="tiles", children=[
        tile("Printing now", f"{n - red - stale} / {n}", f"{red} down · {yel} warning · {stale} no data",
             tone="good" if red == 0 else None),
        tile("Down now", str(red), "stations in red status", tone="critical" if red else None),
        metric_tile("Availability · last 24 h", a24, lambda v: f"{v:.1f}%",
                    "Share of observed printer-minutes not in red status.",
                    unit_note=f"{a24.extra.get('down_h', 0):.1f} printer-hours down" if a24.value is not None else ""),
        tile("Oldest open red", (fmt_minutes(reds["open_min"].max()) +
                                 ("+" if reds.loc[reds["open_min"].idxmax(), "open_censored"] else ""))
             if len(reds) else "—",
             reds.iloc[reds["open_min"].values.argmax()]["station"] if len(reds) else "no stations down"),
        tile("Stations needing parts", str(parts), "part at or within 2 days of replacement"),
    ])

    q = queue.copy()
    q["rank"] = np.arange(1, len(q) + 1)
    q["type"] = q["kind"].map(KIND_LABEL)
    # "21 min+" = already open when monitoring began, so the true age is longer.
    q["open_for"] = [(fmt_minutes(m) + ("+" if c else "")) if m > 0 else ""
                     for m, c in zip(q["open_min"], q["open_censored"])]
    q["backup_txt"] = np.where(q["backup"], "Yes", "No - only printer")
    queue_table = data_table(q, [
        ("rank", "#", None), ("type", "Type", None), ("station", "Station", None), ("issue", "Issue", None),
        ("fix", "Action", None), ("open_for", "Open for", None), ("backup_txt", "Backup in building", None),
        ("area", "Area", None),
    ], max_rows=40, empty="Nothing needs attention right now.")

    lv = ds.levels.pivot_table(index="station_id", columns="component", values="level", aggfunc="last")
    groups = []
    for area in sorted(cur["area"].unique(), key=lambda a: AREA_ORDER.index(a) if a in AREA_ORDER else 99):
        sub = cur[cur["area"] == area].sort_values(["building", "station_id"])
        n_red = (sub["state"] == "red").sum()
        groups.append(html.Div(className="board__group", children=[
            html.H4([area, html.Span(f"{len(sub)} stations" + (f" · {n_red} down" if n_red else ""),
                                     className="board__count")]),
            html.Div(className="board__grid", children=[
                _station_card(r, lv.loc[r["station_id"]].dropna().to_dict() if r["station_id"] in lv.index else {})
                for _, r in sub.iterrows()]),
        ]))

    fc = M.forecast(ds, ids)
    floor = np.where(fc["component"].str.startswith("toner"), config.CONSUMABLE_REPLACE_PCT, 2)
    soon = fc[(fc["days_to_replace"] <= 7) | (fc["level"] <= floor)].sort_values(["days_to_replace", "level"])
    forecast_table = data_table(soon, [
        ("station", "Station", None), ("label", "Part", None), ("level", "Level", lambda v: f"{v:.0f}%"),
        ("burn_per_day", "Burn / day", lambda v: fmt_num(v, 2, " pts")),
        ("days_to_replace", "Days to replace", lambda v: "now" if v == 0 else fmt_num(v, 1)),
        ("area", "Area", None),
    ], empty="No parts expected to need replacement in the next 7 days.")

    points = geo.building_points(ds, ids)
    off = points[(points["campus"] != "Main") & points["lat"].notna()] if len(points) else points
    on_map = points.drop(off.index) if len(points) else points
    off_note = ""
    if len(off):
        off_note = "Not shown (off campus): " + "; ".join(
            f"{r['building']} - {geo.STATE_LABEL.get(r['state'], r['state'])}" for _, r in off.iterrows()) + ". "
    map_card = chart_card(
        "Campus map", "Each building shows its worst station. Marker size = number of printers. "
        "Hover for every station's status.",
        charts.campus_map(theme, on_map, basemap) if len(on_map) else None,
        body=None if len(on_map) else html.Div("No mapped buildings match these filters.", className="empty"),
        wide=True,
        note=off_note + "Building locations: OpenStreetMap. Aerial imagery: Esri World Imagery. "
             "Download the KML to open the same pins in Google Earth.")

    return [
        tiles,
        html.Div(className="grid grid--ops", children=[
            chart_card("Needs attention", "Ranked by severity, whether the building has a backup printer, "
                       "and how long it has been open.", body=queue_table, wide=True),
            map_card,
            chart_card("Parts due in the next 7 days", "From each part's burn rate over the last "
                       f"{config.BURN_RATE_LOOKBACK_DAYS} days.", body=forecast_table, wide=True),
        ]),
        html.H2("Station board", className="section-title"),
        html.Div(groups, className="board"),
    ]


# ---------------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------------

BURN_UNITS = {"per_day": "day", "per_week": "week", "per_month": "month", "per_year": "year"}


def render_management(ds: M.Dataset, theme: str, sections, areas, period, burn_unit):
    if ds.empty:
        return _empty("No data yet.")
    ids = _ids(ds, sections, areas)
    days = None if period == "all" else int(period)
    start, end = M.window(ds, days)
    if ids is not None and not ids:
        return _empty("No stations match these filters.")

    avail = M.availability(ds, start, end, ids)
    mttr_r = M.mttr(ds, "red", start, end, ids)
    mttr_y = M.mttr(ds, "yellow", start, end, ids)
    mtbf = M.mtbf(ds, start, end, ids)
    paper = M.paper_refill_time(ds, start, end, ids)
    tray = M.tray_empty_time(ds, start, end, ids)
    dq = M.data_quality(ds, start, end)

    def n_note(m, word="incidents"):
        return f"n = {m.n:,} {word}" + (f" · median {fmt_minutes(m.extra['median'])}" if "median" in m.extra else "")

    tiles = html.Div(className="tiles tiles--8", children=[
        metric_tile("Availability", avail, lambda v: f"{v:.2f}%",
                    "Observed printer-minutes not in red ÷ all observed printer-minutes. Unobserved time is excluded.",
                    unit_note=f"{avail.extra.get('coverage', 0):.0%} of window observed" if avail.value else ""),
        metric_tile("MTTR · red", mttr_r, fmt_minutes, "Mean time from a red status appearing to clearing.",
                    n_note(mttr_r)),
        metric_tile("MTTR · yellow", mttr_y, fmt_minutes, "Mean time from a yellow status appearing to clearing.",
                    n_note(mttr_y)),
        metric_tile("MTBF", mtbf, fmt_hours, "Printer-hours of uptime per red incident.",
                    f"{mtbf.n:,} failures over {mtbf.extra.get('up_h', 0):,.0f} printer-h"),
        metric_tile("Paper refill time", paper, fmt_minutes, "Mean duration of PAPER OUT (station cannot print).",
                    n_note(paper, "paper-outs")),
        metric_tile("Tray empty time", tray, fmt_minutes,
                    "Mean duration of a single tray reporting empty, even while another tray keeps printing.",
                    f"{tray.extra.get('total_h', 0):,.0f} tray-hours empty in total"),
        metric_tile("Data quality score", dq, lambda v: f"{v:.0f} / 100",
                    "30% freshness + 40% completeness + 20% validity + 10% station coverage.",
                    f"{dq.extra.get('completeness', 0):.1f}% of expected snapshots" if dq.value else ""),
        tile("Refresh failure rate", f"{dq.extra.get('failure_rate', 0):.2f}%" if dq.value is not None else "—",
             f"{dq.extra.get('failures', 0):,} of {dq.extra.get('attempts', 0):,} scrapes failed",
             ok=dq.value is not None),
    ])

    # Availability
    fleet = M.availability_daily(ds, start, end, ids)
    by_sec = M.availability_daily(ds, start, end, ids, by="section")
    b = M.building_availability(ds, start, end, ids)
    sc = M.station_scorecard(ds, start, end, ids).sort_values("availability")

    # MTTR weekly
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    inc = M._resolved(inc).copy()
    inc["week"] = inc["start"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None).dt.to_period("W").dt.start_time
    weekly = (inc.groupby(["week", "severity"])["duration_s"]
              .agg(median_min=lambda s: s.median() / 60, mean_min=lambda s: s.mean() / 60, n="size").reset_index())

    faults = M.faults_in(ds, start, end, ids)
    counts = faults["label"].value_counts()
    by_building = faults.groupby("building").size().sort_values(ascending=False).head(12)

    burn = M.burn_rates(ds, start, end, ids)
    unit = BURN_UNITS.get(burn_unit, "day")
    burn["rate_pts"] = burn[burn_unit]
    burn["rate_units"] = burn[burn_unit] / 100
    cum = M.cumulative_usage(ds, start, end, ids)
    repl = M.replacements(ds, start, end, ids)
    q = M.quality_daily(ds, start, end)

    toner_repl = repl[repl["component"].str.startswith("toner")]
    repl_note = (f"Toner was replaced at an average of {toner_repl['level_before'].mean():.1f}% remaining "
                 f"across {len(toner_repl)} swaps." if len(toner_repl) else "")

    return [
        tiles,
        html.H2("Availability & reliability", className="section-title"),
        html.Div(className="grid", children=[
            chart_card("Daily availability", "Share of observed time each day with the station able to print.",
                       *_needs_days(fleet, lambda: charts.availability_daily(theme, fleet, by_sec)), wide=True,
                       table=data_table(fleet, [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}"),
                                                ("availability", "Availability", lambda v: f"{v:.2f}%"),
                                                ("observed_h", "Observed printer-h", lambda v: f"{v:,.1f}")])),
            chart_card("Building coverage", "At least one printer in the building able to print. "
                       "Hover for the all-printers-up figure.", charts.building_bars(theme, b),
                       table=data_table(b, [("building", "Building", None), ("stations", "Stations", None),
                                            ("any_up", "Any printer up", lambda v: f"{v:.2f}%"),
                                            ("all_up", "All printers up", lambda v: f"{v:.2f}%")])),
            chart_card("Time to resolve, by week", "Median minutes from alert to clear, by severity.",
                       charts.mttr_weekly(theme, weekly) if len(weekly) else None,
                       body=None if len(weekly) else html.Div("No resolved incidents in this period yet.",
                                                              className="empty"),
                       table=data_table(weekly, [("week", "Week of", lambda d: f"{d:%Y-%m-%d}"),
                                                 ("severity", "Severity", None),
                                                 ("median_min", "Median", fmt_minutes),
                                                 ("mean_min", "Mean", fmt_minutes), ("n", "Incidents", None)])),
            chart_card("Station scorecard", "Sorted by availability, worst first. MTBF = uptime per red incident.",
                       wide=True, body=data_table(sc, [
                           ("label", "Station", None), ("area", "Area", None),
                           ("availability", "Availability", lambda v: fmt_num(v, 2, "%")),
                           ("red_incidents", "Red incidents", None),
                           ("mttr_red_min", "MTTR red", fmt_minutes), ("mttr_yellow_min", "MTTR yellow", fmt_minutes),
                           ("mtbf_h", "MTBF", fmt_hours), ("top_fault", "Most common fault", None)])),
        ]),
        html.H2("Faults", className="section-title"),
        html.Div(className="grid", children=[
            chart_card("Fault types", f"{int(counts.sum()):,} fault incidents in the period.",
                       charts.pareto(theme, counts) if len(counts) else None,
                       body=None if len(counts) else _empty("No faults in this period."),
                       table=data_table(counts.rename("n").reset_index(), [("label", "Fault", None), ("n", "Incidents", None)])),
            chart_card("When faults start", "Fault incidents by weekday and local hour.",
                       charts.heatmap(theme, faults) if len(faults) else None,
                       body=None if len(faults) else html.Div("No faults in this period.", className="empty")),
            chart_card("Faults by building", "Top 12 buildings by fault incidents.",
                       charts.pareto(theme, by_building) if len(by_building) else None,
                       body=None if len(by_building) else html.Div("No faults in this period.", className="empty"),
                       table=data_table(by_building.rename("n").reset_index(),
                                        [("building", "Building", None), ("n", "Incidents", None)])),
        ]),
        html.H2("Consumables", className="section-title"),
        html.Div(className="grid", children=[
            chart_card(f"Burn rate per {unit}", "Percentage points used, summed across stations; "
                       "100 pts = one part's worth. Refills never count as negative usage.", wide=True,
                       body=data_table(burn, [
                           ("label", "Part", None), ("used_units", "Used in period (parts)", lambda v: fmt_num(v, 2)),
                           ("rate_pts", f"Burn / {unit} (pts)", lambda v: fmt_num(v, 1)),
                           ("rate_units", f"Burn / {unit} (parts)", lambda v: fmt_num(v, 2)),
                           ("stations", "Stations with enough data", None)])),
            chart_card("Cumulative toner use", "Parts' worth of toner used since the start of the period.",
                       *_needs_days(cum, lambda: charts.cumulative(theme, cum, ["toner_k", "toner_c", "toner_m", "toner_y"]))),
            chart_card("Cumulative drum use", "Parts' worth of drum life used.",
                       *_needs_days(cum, lambda: charts.cumulative(theme, cum, ["drum_k", "drum_c", "drum_m", "drum_y"]))),
            chart_card("Cumulative belt & fuser use", "Parts' worth of belt and fuser life used.",
                       *_needs_days(cum, lambda: charts.cumulative(theme, cum, ["belt", "fuser"])),
                       table=data_table(cum.tail(60), [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}")] +
                                        [(c, config.COMPONENT_LABELS[c], lambda v: fmt_num(v, 2))
                                         for c in config.COMPONENTS])),
            chart_card("Replacement log", "Detected when a level jumps up by "
                       f"{config.REPLACEMENT_JUMP_PTS}+ points. Level before = what was left in the old part.",
                       wide=True, note=repl_note, body=data_table(repl, [
                           ("ts", "When", lambda d: f"{d.tz_convert(config.LOCAL_TZ):%b %d %H:%M}"),
                           ("station", "Station", None), ("label", "Part", None),
                           ("level_before", "Level before", lambda v: f"{v:.0f}%"),
                           ("level_after", "Level after", lambda v: f"{v:.0f}%")], max_rows=60,
                           empty="No replacements detected in this period.")),
        ]),
        html.H2("Data quality", className="section-title"),
        html.Div(className="grid", children=[
            chart_card("Snapshot completeness", "Successful scrapes ÷ expected (one per minute).",
                       *_needs_days(q, lambda: charts.daily_quality(theme, q, "completeness", "Completeness", False))),
            chart_card("Refresh failure rate", "Failed scrape attempts per day.",
                       *_needs_days(q, lambda: charts.daily_quality(theme, q, "failure_rate", "Failure rate", True)),
                       table=data_table(q, [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}"),
                                            ("attempts", "Attempts", None), ("ok", "Succeeded", None),
                                            ("failure_rate", "Failure rate", lambda v: f"{v:.2f}%")])),
        ]),
    ]


# ---------------------------------------------------------------------------------
# Executive
# ---------------------------------------------------------------------------------

def _delta(cur, prev, higher_is_better: bool | None, pct_points=False):
    """Change vs prior period. higher_is_better=None means neutral (volume, not performance)."""
    if cur is None or prev is None or not np.isfinite(cur) or not np.isfinite(prev):
        return html.Span("—", className="delta")
    d = cur - prev
    if pct_points:
        text, negligible = f"{abs(d):.1f} pts", abs(d) < 0.05
    elif prev:
        ratio = cur / prev
        text = f"{ratio:.1f}×" if ratio >= 2 else f"{abs(ratio - 1):.0%}"
        negligible = abs(ratio - 1) < 0.005
    else:
        text, negligible = f"{abs(d):.1f}", abs(d) < 1e-9
    if negligible:
        return html.Span("no change", className="delta")
    arrow = "▲" if d > 0 else "▼"
    if higher_is_better is None:
        return html.Span([html.Span(arrow, **{"aria-hidden": "true"}), f" {text}"], className="delta")
    good = (d > 0) == higher_is_better
    return html.Span([html.Span(arrow, **{"aria-hidden": "true"}), f" {text}",
                      html.Span(" better" if good else " worse", className="sr")],
                     className=f"delta delta--{'good' if good else 'bad'}")


def month_options(ds: M.Dataset) -> list[dict]:
    return [{"label": p.label.replace(" (to date)", "*"), "value": i} for i, p in enumerate(insights.months(ds))]


def default_month(ds: M.Dataset) -> int | None:
    ms = insights.months(ds)
    if not ms:
        return None
    return len(ms) - 2 if len(ms) > 1 and ms[-1].label.endswith("(to date)") else len(ms) - 1


def render_executive(ds: M.Dataset, theme: str, month_idx, sections, areas):
    if ds.empty:
        return _empty("No data yet.")
    ids = _ids(ds, sections, areas)
    ms = insights.months(ds)
    month_idx = default_month(ds) if month_idx is None or month_idx >= len(ms) else month_idx
    cur = ms[month_idx]
    prev = ms[month_idx - 1] if month_idx > 0 else None
    ytd = insights.ytd(ds)
    sc_cur, sc_ytd = insights.scorecard(ds, cur, ids), insights.scorecard(ds, ytd, ids)
    sc_prev = insights.scorecard(ds, prev, ids) if prev else None

    def val(sc, key):
        if sc is None:
            return None
        v = sc[key]
        return v.value if isinstance(v, M.Metric) else v

    def units(sc, comps):
        return None if sc is None else sum(sc["units"].get(c, 0.0) for c in comps)

    rows = [
        ("Availability", "availability", lambda v: fmt_num(v, 2, "%"), True, True),
        ("Red incidents", "red_incidents", lambda v: fmt_num(v, 0), False, False),
        ("MTTR · red", "mttr_red", fmt_minutes, False, False),
        ("MTTR · yellow", "mttr_yellow", fmt_minutes, False, False),
        ("MTBF", "mtbf", fmt_hours, True, False),
        ("Paper refill time", "paper", fmt_minutes, False, False),
        ("Data quality score", "dq", lambda v: fmt_num(v, 0, " / 100"), True, True),
    ]
    body = []
    for label, key, fmt, hib, pts in rows:
        c, p, y = val(sc_cur, key), val(sc_prev, key), val(sc_ytd, key)
        body.append(html.Tr([html.Th(label, scope="row"), html.Td(fmt(c), className="num"),
                             html.Td(fmt(p) if prev else "—", className="num"),
                             html.Td(_delta(c, p, hib, pts) if prev else "—", className="num"),
                             html.Td(fmt(y), className="num")]))
    for label, comps in (("Black toner used (parts)", ["toner_k"]),
                         ("Color toner used (parts)", ["toner_c", "toner_m", "toner_y"]),
                         ("Drums used (parts)", ["drum_k", "drum_c", "drum_m", "drum_y"]),
                         ("Belts + fusers used (parts)", ["belt", "fuser"])):
        c, p, y = units(sc_cur, comps), units(sc_prev, comps), units(sc_ytd, comps)
        body.append(html.Tr([html.Th(label, scope="row"), html.Td(fmt_num(c, 1), className="num"),
                             html.Td(fmt_num(p, 1) if prev else "—", className="num"),
                             html.Td(_delta(c, p, None) if prev else "—", className="num"),
                             html.Td(fmt_num(y, 1), className="num")]))
    table = html.Div(className="table-wrap", children=html.Table(className="table table--exec", children=[
        html.Thead(html.Tr([html.Th(""), html.Th(cur.label), html.Th(prev.label if prev else "Prior month"),
                            html.Th("Change"), html.Th(ytd.label)])),
        html.Tbody(body)]))

    obs = insights.observations(ds, cur, prev, ids)

    labels, avail_vals, partial, unit_rows = [], [], [], []
    for p in ms:
        a = M.availability(ds, p.start, p.end, ids)
        labels.append(p.start.tz_convert(config.LOCAL_TZ).strftime("%b %Y"))
        avail_vals.append(a.value if a.value is not None else np.nan)
        partial.append(p.label.endswith("(to date)") or p.start == ds.data_start)
        burn = M.burn_rates(ds, p.start, p.end, ids).set_index("component")["used_units"]
        unit_rows.append({"month": labels[-1], **burn.to_dict()})
    units_frame = pd.DataFrame(unit_rows)

    demand = insights.demand_forecast(ds, 30, ids)

    return [
        html.Div(className="grid grid--exec", children=[
            chart_card(f"Scorecard · {cur.label}", "Compared with the prior month and the year to date.",
                       body=table, wide=True),
            chart_card("What stands out", "Generated from the data; each line appears only when the evidence "
                       "behind it is sufficient.", wide=True,
                       body=html.Ul([html.Li(o) for o in obs], className="observations") if obs
                       else _empty("Not enough data for observations yet.")),
            chart_card("Availability by month", "Gray bars are partial months.",
                       charts.monthly_bars(theme, labels, avail_vals, "%", partial)),
            chart_card("Toner used by month", "Parts' worth of toner, stacked by color.",
                       charts.monthly_stacked(theme, units_frame, ["toner_k", "toner_c", "toner_m", "toner_y"]),
                       table=data_table(units_frame, [("month", "Month", None)] +
                                        [(c, config.COMPONENT_LABELS[c], lambda v: fmt_num(v, 2))
                                         for c in config.COMPONENTS])),
            chart_card("Parts needed in the next 30 days", "Projected from the last "
                       f"{config.BURN_RATE_LOOKBACK_DAYS} days' burn rate - a starting point for ordering.",
                       body=data_table(demand, [("label", "Part", None),
                                                ("next_units", "Parts (30 days)", lambda v: fmt_num(v, 1)),
                                                ("per_day", "Burn / day (pts)", lambda v: fmt_num(v, 1))])),
        ]),
        html.P("* month to date. Partial months are not comparable with full months.", className="footnote"),
    ]
