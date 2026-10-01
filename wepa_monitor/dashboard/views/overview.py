"""Overview: what is happening right now, in one screen."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dash import dcc, html

from ... import activity, config, geo, metrics as M, ops
from .. import charts
from ..components import chart_card, data_table, fmt_minutes, fmt_num, headline, icon, station_link, tile
from .common import empty, scope_ids

KIND_LABEL = {"red": "Down", "yellow": "Warning", "tray": "Tray empty", "consumable_now": "End of life",
              "consumable_soon": "Nearing end", "stale": "No data"}
KIND_TONE = {"red": "critical", "yellow": "warning", "stale": "serious", "tray": "info",
             "consumable_now": "warning", "consumable_soon": "info"}


def status_headline(ds: M.Dataset, cur: pd.DataFrame, queue: pd.DataFrame) -> html.Div:
    n = len(cur)
    red = cur[cur["state"] == "red"]
    yel = cur[cur["state"] == "yellow"]
    parts = queue[queue["kind"] == "consumable_now"]["station_id"].nunique()
    age_min = (ds.as_of - cur["scrape_ts"].max()).total_seconds() / 60 if n else 0
    extras = []
    if parts:
        extras.append(f"{parts} station{'s' if parts != 1 else ''} {'has' if parts == 1 else 'have'} a consumable at end of life")
    if len(yel) and len(red):
        extras.append(f"{len(yel)} warning{'s' if len(yel) != 1 else ''}")
    if not ds.is_demo and age_min > 10:
        return headline("serious", f"Status may be out of date: the last snapshot is {fmt_minutes(age_min)} old",
                        "Check that the collector is running (Activity shows monitoring gaps).")
    if len(red):
        reds = queue[queue["kind"] == "red"].sort_values("open_min", ascending=False)
        lead = reds.iloc[0] if len(reds) else None
        oldest = (f"Longest: {lead['station']}, down {fmt_minutes(lead['open_min'])}"
                  f"{'+' if lead['open_censored'] else ''} ({lead['issue'].lower()})") if lead is not None else ""
        title = f"{len(red)} of {n} stations {'is' if len(red) == 1 else 'are'} down"
        return headline("critical", title, ". ".join([oldest] + extras) + ".")
    if len(yel):
        return headline("warning", f"All stations can print; {len(yel)} {'has' if len(yel) == 1 else 'have'} a warning",
                        ". ".join(extras) + "." if extras else "")
    detail = ". ".join(extras)
    return headline("good", f"All {n} stations are printing",
                    (detail[:1].upper() + detail[1:] + ".") if extras else "Nothing needs attention right now.")


def render(ds: M.Dataset, theme: str, sections, areas, basemap: str = "street"):
    if ds.empty:
        return empty("No snapshots yet. Start the collector (python -m wepa_monitor start) or generate demo "
                     "data (python -m wepa_monitor demo).")
    ids = scope_ids(ds, sections, areas)
    cur = ops.current_status(ds, ids)
    if cur.empty:
        return empty("No stations in this scope.")
    queue = ops.work_queue(ds, ids)
    n = len(cur)
    red = int((cur["state"] == "red").sum())
    yel = int((cur["state"] == "yellow").sum())
    stale = int(cur["stale"].sum())
    a24 = M.availability(ds, ds.as_of - pd.Timedelta(days=1), ds.as_of, ids)
    a7 = M.availability(ds, ds.as_of - pd.Timedelta(days=8), ds.as_of - pd.Timedelta(days=1), ids)
    due = queue[queue["kind"].isin(["consumable_now", "consumable_soon"])]["station_id"].nunique()
    compare = ""
    if a24.value is not None and a7.value is not None:
        d = a24.value - a7.value
        compare = f"{'▲' if d >= 0 else '▼'} {abs(d):.1f} pts vs prior 7 days"

    tiles = html.Div(className="tiles", children=[
        tile("Printing now", f"{n - red - stale} / {n}", f"{stale} without recent data" if stale else "stations able to print",
             tone="good" if red == 0 else None, href="/stations"),
        tile("Down", str(red), "stations that can't print", tone="critical" if red else None,
             href="/stations?status=red"),
        tile("Warnings", str(yel), "printing, but need a look", tone="warning" if yel else None,
             href="/stations?status=yellow"),
        tile("Availability, last 24 h", f"{a24.value:.1f}%" if a24.value is not None else "—",
             f"{a24.extra.get('down_h', 0):.1f} printer-hours down" if a24.value is not None else a24.note,
             compare=compare, ok=a24.ok),
        tile("Near end of life", str(due), "stations with a consumable due within 2 days"),
    ])

    # Needs attention: compact rows, each linking to the station.
    rows = []
    for r in queue.head(10).itertuples(index=False):
        tone = KIND_TONE.get(r.kind, "info")
        age = (fmt_minutes(r.open_min) + ("+" if r.open_censored else "")) if r.open_min > 0 else ""
        rows.append(html.Li(className=f"todo todo--{tone}", children=[
            html.Span(KIND_LABEL.get(r.kind, r.kind), className=f"todo__tag tag tag--{tone}"),
            html.Div([station_link(r.station_id, r.station, "todo__station"),
                      html.Div(f"{r.issue} · {r.fix}", className="todo__issue")], className="todo__main"),
            html.Div([html.Div(age, className="todo__age"),
                      html.Div("only printer in building" if not r.backup else "", className="todo__note")],
                     className="todo__side"),
        ]))
    more = len(queue) - 10
    attention = chart_card(
        "Needs attention", "Ranked by severity, whether the building has a backup printer, and how long it's been open.",
        body=html.Ul(rows, className="todo-list") if rows else empty("Nothing needs attention right now.", big=False),
        note=f"+ {more} more lower-priority items on the Stations page." if more > 0 else "")

    ev = activity.events(ds, since=ds.as_of - pd.Timedelta(hours=48), ids=ids).head(8)
    recent = chart_card("Recent activity", "The last 48 hours.",
                        body=activity_list(ev) if len(ev) else empty("No activity in the last 48 hours.", big=False),
                        action=dcc.Link("View all", href="/activity", className="link link--quiet"))

    points = geo.building_points(ds, ids)
    off = points[(points["campus"] != "Main") & points["lat"].notna()] if len(points) else points
    on_map = points.drop(off.index) if len(points) else points
    off_note = ("Off campus: " + "; ".join(f"{r['building']} ({geo.STATE_LABEL.get(r['state'], r['state']).lower()})"
                                          for _, r in off.iterrows()) + ". ") if len(off) else ""
    map_card = chart_card(
        "Campus map", "Each building shows its worst station; larger markers have more printers. Click a building to open it.",
        charts.campus_map(theme, on_map, basemap) if len(on_map) else None, graph_id="campus-map",
        body=None if len(on_map) else empty("No mapped buildings in this scope.", big=False), wide=True,
        action=html.Div([
            dcc.RadioItems(id="basemap", options=[{"label": "Street", "value": "street"},
                                                  {"label": "Aerial", "value": "satellite"}],
                           value=basemap, inline=True, className="seg seg--sm", labelClassName="seg__opt",
                           inputClassName="seg__input", persistence=True, persistence_type="local"),
            html.Button([icon("download"), "Google Earth"], id="kml-btn", className="btn btn--sm",
                        title="Download these pins as a KML file for Google Earth"),
        ], className="card__tools"),
        note=off_note + "Locations: OpenStreetMap. Aerial imagery: Esri World Imagery.")

    fc = M.forecast(ds, ids)
    floor = np.where(fc["component"].str.startswith("toner"), config.CONSUMABLE_REPLACE_PCT, 2)
    soon = fc[(fc["days_to_replace"] <= 7) | (fc["level"] <= floor)].sort_values(["days_to_replace", "level"])
    parts = chart_card(
        "Consumable End-of-Life Watch",
        f"Parts at, or projected to reach, their replacement point within 7 days, from each part's burn rate "
        f"over the last {config.BURN_RATE_LOOKBACK_DAYS} days.",
        wide=True, body=data_table(soon, [
            ("station", "Station", None), ("label", "Part", None), ("level", "Level", lambda v: f"{v:.0f}%"),
            ("days_to_replace", "Replace in", lambda v: "now" if v == 0 else f"{fmt_num(v, 1)} days"),
            ("burn_per_day", "Use per day", lambda v: fmt_num(v, 2, " pts")),
            ("area", "Area", None)], empty="No consumables are projected to reach end of life in the next 7 days.",
            link_col=("station", "station_id")))

    return [
        status_headline(ds, cur, queue),
        tiles,
        html.Div(className="grid grid--2-1", children=[attention, recent]),
        html.Div(className="grid", children=[map_card, parts]),
    ]


SEV_ICON = {"critical": "x", "warning": "alert", "good": "check", "info": "info"}


def activity_list(ev: pd.DataFrame, show_date: bool = False) -> html.Ul:
    items = []
    for r in ev.itertuples(index=False):
        local = r.ts.tz_convert(config.LOCAL_TZ)
        when = local.strftime("%b %-d, %-I:%M %p") if show_date else local.strftime("%-I:%M %p")
        title = station_link(r.station_id, r.title, "feed__title") if r.station_id else \
            html.Span(r.title, className="feed__title")
        items.append(html.Li(className=f"feed__item feed__item--{r.severity}", children=[
            html.Span(icon(SEV_ICON.get(r.severity, "info")), className="feed__icon"),
            html.Div([title, html.Div(r.detail, className="feed__detail") if r.detail else None],
                     className="feed__body"),
            html.Time(when, className="feed__time", dateTime=r.ts.isoformat()),
        ]))
    return html.Ul(items, className="feed")
