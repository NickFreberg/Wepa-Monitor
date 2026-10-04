"""Station drill-through: everything about one print station."""
from __future__ import annotations

from ... import durations

import pandas as pd
from dash import dcc, html

from ... import activity, campus, config, metrics as M, models, nearby, ops, support
from .. import charts
from ..components import (chart_card, data_table, desk_line, explore_hint, fmt_hours, fmt_minutes, headline,
                          level_bar, station_link, status_pill, tile)
from .common import _unstaffed, empty, period_label, period_window, station_messages, status_segments
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
                    f"Next end of life: {p['label']} in about {durations.days(p['days'])}")
    a = M.availability(ds, start, end, ids)
    fleet = M.availability(ds, start, end)
    avail_txt = (f"Available {a.value:.1f}% of the time over the last {plabel} (all BSU stations: {fleet.value:.1f}%)"
                 if a.value is not None and fleet.value is not None else "")
    state = row["state"]
    if state == "red":
        hl = headline("critical", f"Out of service: {', '.join(msgs[:2]) or 'not printing'}",
                      ". ".join(x for x in (avail_txt, part_txt) if x) + ".")
    elif state == "yellow":
        hl = headline("warning", f"Degraded: {', '.join(msgs[:2])}",
                      ". ".join(x for x in (avail_txt, part_txt) if x) + ".")
    elif state == "stale":
        hl = headline("serious", "No recent data from this station", "It hasn't appeared on the Wepa page lately.")
    else:
        hl = headline("good", "Operational" + (f" ({msgs[0].lower()})" if msgs else ""),
                      ". ".join(x for x in (avail_txt, part_txt) if x) + ".")

    header = html.Div(className="station-hero", children=[
        html.Div([
            html.Div([html.H1(row["description"]), status_pill(state, _unstaffed(row))],
                     className="station-hero__title"),
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
        tile("Outages", str(red_n), (f"{after_n} began after desk hours" if red_n else f"red incidents, last {plabel}")),
        tile("Mean time to repair (MTTR)", fmt_minutes(mttr.value) if mttr.value is not None else "—",
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
    # End-of-life projection, for any part (the one closest to end of life by default).
    fc_sorted = fc.sort_values(["days", "level"], na_position="last")
    fitted = fc_sorted[(fc_sorted["method"] == "regression") & (fc_sorted["days"] > 0)]
    default = fitted.iloc[0]["component"] if len(fitted) else fc_sorted.iloc[0]["component"] if len(fc_sorted) else None
    part_opts = [{"label": f"{r['label']} · {r['level']:.0f}%" + (f" · {r['window']}" if r["window"] not in ("—", "") else ""),
                  "value": r["component"]} for _, r in fc_sorted.iterrows()]
    fig0, note0 = eol_figure(ds, theme, station_id, default) if default else (None, "")
    eol_card = chart_card(
        "End-of-life projection", "Each part's readings since it was last replaced, and when the trend reaches the "
        "replacement point. Pick a part:",
        fig0 if fig0 is not None else charts.blank(), graph_id={"type": "xg", "chart": "eol"},
        action=dcc.Dropdown(id="sd-eol-part", options=part_opts, value=default, clearable=False, searchable=False,
                            className="dropdown dropdown--sm", style={"minWidth": "230px"}),
        note=html.Span(note0, id="sd-eol-note"),
        nerd="Theil-Sen line (the median of all pairwise slopes, so a sensor blip can't drag it) through the readings "
             "since the last replacement; the window is the 90% range of the slope.") if default else None
    consumables = [
        chart_card("Consumable levels now", "Bars turn amber at 10% and red at 5% (belt and fuser: 5% / 2%). "
                   "The table forecasts each part's end of life.", body=bars, table=fc_table),
        *([eol_card] if eol_card is not None else []),
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
        sev=lambda d: d["severity"].map({"red": "Outage", "yellow": "Degraded"}),
        lasted=lambda d: [("still open" if s == "open" else ("unknown" if s == "unknown_end" else fmt_minutes(x / 60)))
                          for s, x in zip(d["status"], d["duration_s"])])
    fault_cards = [
        chart_card("What goes wrong", f"Fault incidents, last {plabel}.",
                   charts.pareto(theme, counts) if len(counts) else None, graph_id={"type": "xg", "chart": "station_faults"},
                   body=None if len(counts) else empty("No faults in this period.", big=False)),
        chart_card("When it goes wrong", "Fault incidents by local hour of day.",
                   charts.hour_bars(theme, faults["hour"], "faults") if len(faults) else None, graph_id={"type": "xg", "chart": "station_hours"},
                   body=None if len(faults) else empty("No faults in this period.", big=False)),
        chart_card("Incident history", f"Every outage and degraded period, last {plabel}. Outages carry a "
                   "permanent reference number; degraded periods (warnings) don't.", wide=True,
                   body=data_table(hist.assign(ref=hist["ref"].fillna("") if "ref" in hist else ""),
                                   [("ref", "Reference", None), ("when", "Started", None), ("sev", "Type", None),
                                    ("cause", "Cause", None), ("lasted", "Lasted", None)], max_rows=100,
                                   empty="No incidents in this period.")),
    ]

    # --- neighbours & activity ----------------------------------------------------------------
    same = cur_all[(cur_all["building"] == row["building"]) & (cur_all["station_id"] != station_id)]
    alt = nearby.backups(cur_all, station_id, limit=3)
    alt_public = alt[alt["kind"] == "open to everyone"] if len(alt) else alt
    items = [html.Li([station_link(r["station_id"], f"{r['description']} #{r['station_id']}"),
                      html.Span("same building", className="neighbour__dist"), status_pill(r["state"])],
                     className="neighbour") for _, r in same.iterrows()]
    items += [html.Li([station_link(r["station_id"], f"{r['description']}"),
                       html.Span(f"{nearby.fmt_distance(r['meters'])} · {nearby.fmt_walk(r['seconds'])}",
                                 className="neighbour__dist"), status_pill(r["state"])], className="neighbour")
              for _, r in alt_public.iterrows()]
    if len(same):
        backup_sub = (f"{row['building']} has {len(same)} other printer{'s' if len(same) != 1 else ''}; below "
                      "them, the nearest working printers anyone can walk into.")
    else:
        backup_sub = ("The nearest working printers anyone can walk into. Residence halls are card access, so "
                      "they're left out; East Campus Commons is included, since every student uses that building. "
                      "Distances follow campus walkways.")
    neighbours = html.Ul(items, className="neighbours") if items else html.P(
        "No working printer nearby right now.", className="card__note")
    ev_all = activity.events(ds, since=start, ids=ids)
    ev = ev_all.head(10)

    return [
        html.Nav([dcc.Link("Stations", href="/stations", className="link link--quiet"), " / ",
                  dcc.Link(row["building"], href=f"/stations?q={row['building']}", className="link link--quiet")],
                 className="breadcrumb", **{"aria-label": "Breadcrumb"}),
        header, hl, _risk_line(ds, station_id), explore_hint(), tiles,
        html.Div(className="grid", children=[timeline]),
        html.H2("Consumables", className="section-title"),
        html.Div(className="grid", children=consumables),
        html.H2("Faults and incidents", className="section-title"),
        html.Div(className="grid", children=fault_cards),
        html.Div(className="grid", children=[
            chart_card("If this printer is out of service", backup_sub, body=neighbours),
            chart_card("Recent activity", f"The latest {len(ev)} of {len(ev_all)} events in the last {plabel}.",
                       body=activity_list(ev, show_date=True) if len(ev) else empty("No activity.", big=False),
                       action=dcc.Link("View all", href=f"/activity?q={station_id}", className="link link--quiet")),
        ]),
    ]


def building_line(ds: M.Dataset, row) -> html.Div | None:
    """Library opening hours, for the library printers (outages while it's closed strand no one)."""
    c = campus.load()
    if c.empty:
        return None
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


def eol_figure(ds: M.Dataset, theme: str, station_id: str, comp: str):
    """(figure or None, note) for one part's end-of-life projection."""
    pts_fit = models.eol_points(ds, station_id, comp)
    if len(pts_fit) < models.MIN_POINTS_FOR_FIT:
        return None, "Not enough readings since its last replacement to project a trend yet."
    floor = models.replace_point(comp)
    fit = models.fit_series(pts_fit["scrape_ts"], pts_fit["level"], ds.as_of, floor)
    if not fit:
        return None, "Its level hasn't been falling, so there's no end of life in sight."
    row = models.eol_forecast(ds, [station_id]).set_index("component").loc[comp]
    label = config.COMPONENT_LABELS[comp]
    fig = charts.eol_projection(theme, pts_fit, fit, comp, ds.as_of, floor)
    when = eol_window(row)
    note = (f"{label}: {when} until it needs replacing, using {abs(fit['slope_per_day']):.2f} points a day "
            f"(the trend fits {fit['r2']:.0%} of the ups and downs; {fit['n']} readings)." if when not in ("—", "now")
            else f"{label} is at its replacement point now.")
    return fig, note


def _risk_line(ds, station_id):
    from .risk_view import station_line
    try:
        return station_line(ds, station_id)
    except Exception:  # noqa: BLE001 - the model must never break the page
        return None
