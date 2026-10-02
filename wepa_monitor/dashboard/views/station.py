"""Station drill-through: everything about one print station."""
from __future__ import annotations

import pandas as pd
from dash import dcc, html

from ... import activity, campus, config, metrics as M, models, ops, support
from .. import charts
from ..components import (chart_card, data_table, desk_line, explore_hint, fmt_hours, fmt_minutes, headline,
                          level_bar, station_link, status_pill, tile)
from .common import empty, period_label, period_window, station_messages, status_segments
from .analytics import eol_window
from .overview import activity_list


def render(ds: M.Dataset, theme: str, station_id: str, period):
    if ds.empty:
        return empty("No snapshots yet.")
    cur_all = ops.current_status(ds)
    if station_id not in set(cur_all["station_id"]):
        return [headline("info", f"Station #{station_id} isn't in the data",
                         "It may have been renamed or removed from the Wepa page."),
                dcc.Link("Back to all stations", href="/stations", className="link")]
    row = cur_all[cur_all["station_id"] == station_id].iloc[0]
    start, end = period_window(ds, period)
    ids = [station_id]
    plabel = period_label(period)

    # --- headline: current state in a sentence --------------------------------------------
    msgs = station_messages(row)
    fc = models.eol_forecast(ds, ids).sort_values("days")
    next_part = fc[fc["days"].notna() & (fc["days"] < 3650)].head(1)
    part_txt = ""
    if len(next_part):
        p = next_part.iloc[0]
        part_txt = (f"{p['label']} is at end of life ({p['level']:.0f}%)" if p["days"] == 0 else
                    f"Next end of life: {p['label']} in about {p['days']:.0f} day"
                    f"{'s' if round(p['days']) != 1 else ''}")
    a = M.availability(ds, start, end, ids)
    fleet = M.availability(ds, start, end)
    avail_txt = (f"Available {a.value:.1f}% of the time over the last {plabel} (all BSU stations: {fleet.value:.1f}%)"
                 if a.value is not None and fleet.value is not None else "")
    state = row["state"]
    if state == "red":
        hl = headline("critical", f"Down: {', '.join(msgs[:2]) or 'not printing'}",
                      ". ".join(x for x in (avail_txt, part_txt) if x) + ".")
    elif state == "yellow":
        hl = headline("warning", f"Printing, with a warning: {', '.join(msgs[:2])}",
                      ". ".join(x for x in (avail_txt, part_txt) if x) + ".")
    elif state == "stale":
        hl = headline("serious", "No recent data from this station", "It hasn't appeared on the Wepa page lately.")
    else:
        hl = headline("good", "Printing normally" + (f" ({msgs[0].lower()})" if msgs else ""),
                      ". ".join(x for x in (avail_txt, part_txt) if x) + ".")

    header = html.Div(className="station-hero", children=[
        html.Div([
            html.Div([html.H1(row["description"]), status_pill(state)], className="station-hero__title"),
            html.Div(f"Station #{station_id} · {row['building']} · {row['area']} · {row['section']} · "
                     f"updated {row['scrape_ts'].tz_convert(config.LOCAL_TZ):%-I:%M %p}",
                     className="station-hero__meta"),
            desk_line(ds, row.get("owner") or support.owner(row["section"])),
            building_line(ds, row),
        ]),
    ])

    # --- KPIs vs fleet ---------------------------------------------------------------------
    mttr = M.mttr(ds, "red", start, end, ids)
    fleet_mttr = M.mttr(ds, "red", start, end)
    mtbf = M.mtbf(ds, start, end, ids)
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    red_n = int((inc["severity"] == "red").sum())
    after_n = int((~inc.loc[inc["severity"] == "red", "in_hours"].astype(bool)).sum())
    paper = M.paper_refill_time(ds, start, end, ids)

    def vs(v, f, fmt, better_low=False):
        if v is None or f is None:
            return ""
        return f"all BSU stations: {fmt(f)}"

    tiles = html.Div(className="tiles", children=[
        tile("Availability", f"{a.value:.1f}%" if a.value is not None else "—", f"last {plabel}",
             compare=vs(a.value, fleet.value, lambda x: f"{x:.1f}%"), ok=a.ok),
        tile("Times down", str(red_n), (f"{after_n} began after desk hours" if red_n else f"red incidents, last {plabel}")),
        tile("Time to fix (MTTR)", fmt_minutes(mttr.value) if mttr.value is not None else "—",
             mttr.note or f"mean of {mttr.n} incidents",
             compare=vs(mttr.value, fleet_mttr.value, fmt_minutes), ok=mttr.ok),
        tile("Time between failures", fmt_hours(mtbf.value) if mtbf.value is not None else "—",
             mtbf.note or "uptime per red incident", ok=mtbf.ok),
        tile("Paper refill time", fmt_minutes(paper.value) if paper.value is not None else "—",
             paper.note or f"mean of {paper.n} paper-outs", ok=paper.ok),
    ])

    # --- timeline ---------------------------------------------------------------------------
    tl_start = max(start, end - pd.Timedelta(days=30))
    segs = status_segments(ds, station_id, tl_start, end)
    timeline = chart_card(
        "Status timeline", f"What this station was doing, minute by minute"
        f"{' (last 30 days)' if tl_start > start else ''}. Hover for exact times.",
        charts.status_timeline(theme, segs, tl_start, end, owner=row.get("owner")) if len(segs) else None, graph_id={"type": "xg", "chart": "timeline"},
        explain="Green is printing, amber a warning, red down, gray no data. The shaded columns behind the band "
                "are the support desk's staffed hours, so you can see whether a problem started while someone was "
                "in. Click any stretch to see what caused it.",
        body=None if len(segs) else empty("No status history in this period.", big=False), wide=True)

    # --- consumables ------------------------------------------------------------------------
    lv = ds.levels[ds.levels["station_id"] == station_id].set_index("component")["level"]
    bars = html.Div(className="levels-big", children=[
        html.Div([html.H4("Toner"), level_bar("K", lv.get("toner_k")), level_bar("C", lv.get("toner_c")),
                  level_bar("M", lv.get("toner_m")), level_bar("Y", lv.get("toner_y"))]),
        html.Div([html.H4("Drums"), level_bar("K", lv.get("drum_k")), level_bar("C", lv.get("drum_c")),
                  level_bar("M", lv.get("drum_m")), level_bar("Y", lv.get("drum_y"))]),
        html.Div([html.H4("Other"), level_bar("Belt", lv.get("belt"), low=5, critical=2),
                  level_bar("Fuser", lv.get("fuser"), low=5, critical=2)]),
    ])
    pts = ds.cons[(ds.cons["station_id"] == station_id)]
    before = pts[pts["scrape_ts"] < start].groupby("component").tail(1).assign(scrape_ts=start)
    pts = pd.concat([before, pts[(pts["scrape_ts"] >= start) & (pts["scrape_ts"] < end)]])
    last = pts.groupby("component").tail(1).assign(scrape_ts=end)
    pts = pd.concat([pts, last]).sort_values(["component", "scrape_ts"])
    repl = M.replacements(ds, start, end, ids)
    fc = fc.assign(window=fc.apply(eol_window, axis=1))
    fc_table = data_table(fc, [
        ("label", "Part", None), ("level", "Level", lambda v: f"{v:.0f}%"),
        ("window", "End of life in (90% window)", None), ("method", "Method", None)])
    # Regression chart for the part closest to end of life that has a fit.
    fitted = fc[(fc["method"] == "regression") & (fc["days"] > 0)].head(1)
    eol_card = None
    if len(fitted):
        f = fitted.iloc[0]
        comp = f["component"]
        pts_fit = models.eol_points(ds, station_id, comp)
        fit = models.fit_series(pts_fit["scrape_ts"], pts_fit["level"], ds.as_of, models.replace_point(comp))
        if fit:
            eol_card = chart_card(
                f"End-of-life projection: {f['label']}",
                "Readings since the last replacement, a Theil-Sen line of best fit (robust to sensor blips), and "
                "where it meets the replacement point.",
                charts.eol_projection(theme, pts_fit, fit, comp, ds.as_of, models.replace_point(comp)),
                graph_id={"type": "xg", "chart": "eol"},
                note=(f"Projected end of life in {eol_window(f)} at {fit['slope_per_day']:.2f} pts/day "
                      f"(R² = {fit['r2']:.2f}, {fit['n']} readings)."))
    consumables = [
        chart_card("Consumable levels now", "Bars turn amber at 10% and red at 5% (belt and fuser: 5% / 2%). "
                   "The table forecasts each part's end of life.", body=bars, table=fc_table),
        *([eol_card] if eol_card else []),
        chart_card("Toner over time", "Triangles mark detected replacements.",
                   charts.levels_over_time(theme, pts, repl, ["toner_k", "toner_c", "toner_m", "toner_y"], start, end),
                   graph_id={"type": "xg", "chart": "levels_toner"}, explain="Lines step down as toner is used and jump back to 100% when "
                   "it's replaced (triangles). Steeper lines mean heavier printing."),
        chart_card("Drums over time", "",
                   charts.levels_over_time(theme, pts, repl, ["drum_k", "drum_c", "drum_m", "drum_y"], start, end),
                   graph_id={"type": "xg", "chart": "levels_drum"}),
        chart_card("Belt and fuser over time", "",
                   charts.levels_over_time(theme, pts, repl, ["belt", "fuser"], start, end), graph_id={"type": "xg", "chart": "levels_other"}),
    ]

    # --- faults & incidents -----------------------------------------------------------------
    faults = M.faults_in(ds, start, end, ids)
    counts = faults["label"].value_counts()
    cause = ds.fault_inc[ds.fault_inc["station_id"] == station_id].groupby("start")["label"].agg(
        lambda s: ", ".join(sorted(set(s))))
    hist = inc.sort_values("start", ascending=False).assign(
        when=lambda d: d["start"].dt.tz_convert(config.LOCAL_TZ).dt.strftime("%a %b %-d, %-I:%M %p"),
        cause=lambda d: d["start"].map(cause).fillna(""),
        sev=lambda d: d["severity"].map({"red": "Down", "yellow": "Warning"}),
        lasted=lambda d: [("still open" if s == "open" else ("unknown" if s == "unknown_end" else fmt_minutes(x / 60)))
                          for s, x in zip(d["status"], d["duration_s"])])
    fault_cards = [
        chart_card("What goes wrong", f"Fault incidents, last {plabel}.",
                   charts.pareto(theme, counts) if len(counts) else None, graph_id={"type": "xg", "chart": "station_faults"},
                   body=None if len(counts) else empty("No faults in this period.", big=False)),
        chart_card("When it goes wrong", "Fault incidents by local hour of day.",
                   charts.hour_bars(theme, faults["hour"], "faults") if len(faults) else None, graph_id={"type": "xg", "chart": "station_hours"},
                   body=None if len(faults) else empty("No faults in this period.", big=False)),
        chart_card("Incident history", f"Every red and yellow incident, last {plabel}.", wide=True,
                   body=data_table(hist, [("when", "Started", None), ("sev", "Type", None), ("cause", "Cause", None),
                                          ("lasted", "Lasted", None)], max_rows=100,
                                   empty="No incidents in this period.")),
    ]

    # --- neighbours & activity ----------------------------------------------------------------
    same = cur_all[(cur_all["building"] == row["building"]) & (cur_all["station_id"] != station_id)]
    neighbours = html.Ul([html.Li([station_link(r["station_id"], f"{r['description']} #{r['station_id']}"),
                                   status_pill(r["state"])], className="neighbour")
                          for _, r in same.iterrows()], className="neighbours") if len(same) else \
        html.P("This is the only print station in the building, so when it's down students there have no "
               "nearby backup.", className="card__note")
    ev_all = activity.events(ds, since=start, ids=ids)
    ev = ev_all.head(10)

    return [
        html.Nav([dcc.Link("Stations", href="/stations", className="link link--quiet"), " / ",
                  dcc.Link(row["building"], href=f"/stations?q={row['building']}", className="link link--quiet")],
                 className="breadcrumb", **{"aria-label": "Breadcrumb"}),
        header, hl, explore_hint(), tiles,
        html.Div(className="grid", children=[timeline]),
        html.H2("Consumables", className="section-title"),
        html.Div(className="grid", children=consumables),
        html.H2("Faults and incidents", className="section-title"),
        html.Div(className="grid", children=fault_cards),
        html.Div(className="grid", children=[
            chart_card(f"Other printers in {row['building']}", "Backups for students if this one is down.",
                       body=neighbours),
            chart_card("Recent activity", f"The latest {len(ev)} of {len(ev_all)} events in the last {plabel}.",
                       body=activity_list(ev, show_date=True) if len(ev) else empty("No activity.", big=False),
                       action=dcc.Link("View all", href=f"/activity?q={station_id}", className="link link--quiet")),
        ]),
    ]


def building_line(ds: M.Dataset, row) -> html.Div | None:
    """Who depends on this printer: hall residents per printer, or library opening hours."""
    c = campus.load()
    if c.empty:
        return None
    if row.get("station_type") == "residence":
        h = campus.residents_per_printer(c, ds.stations)
        h = h[h["building"] == row["building"]]
        if h.empty:
            return None
        r = h.iloc[0]
        today = c.on(ds.as_of.tz_convert(config.LOCAL_TZ).date())
        closed = today is not None and not today["halls_open"]
        est = " (estimated)" if str(r["estimated"]).lower() == "true" else ""
        return html.Div([html.Span(className="desk__cal", **{"aria-hidden": "true"}),
                         html.Span([f"About {r['per_printer']:.0f} residents per printer{est} ",
                                    html.Span(f"{r['residents']} {str(r['who']).split(',')[0].lower()} residents, "
                                              f"{r['printers']} printer{'s' if r['printers'] != 1 else ''}",
                                              className="desk__status")]),
                         html.Span("Residence halls are closed right now", className="desk__status") if closed
                         else None], className="desk desk--campus")
    if row.get("building") == campus.LIBRARY_BUILDING:
        today = c.on(ds.as_of.tz_convert(config.LOCAL_TZ).date())
        if today is None or not today.get("library_known", False):
            return None
        o, cl = today["library_open"], today["library_close"]
        txt = "Maxwell Library is closed today" if pd.isna(o) else \
            f"Maxwell Library open {support._clock(o)}–{support._clock(float(cl) % 24)} today"
        return html.Div([html.Span(className="desk__cal", **{"aria-hidden": "true"}), html.Span(txt),
                         html.Span("outages while it's closed don't strand anyone", className="desk__status")],
                        className="desk desk--campus")
    return None
