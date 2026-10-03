"""Overview: what is happening right now, in one screen."""
from __future__ import annotations

import pandas as pd
from dash import dcc, html

from ... import activity, config, geo, metrics as M, models, ops, support, vocab
from .. import charts
from ..components import campus_line, chart_card, data_table, desk_line, explore_hint, fmt_minutes, headline, icon, station_link, tile
from .common import empty, scope_ids

KIND_LABEL = {"red": "Out of service", "yellow": "Degraded", "tray": "Tray empty", "consumable_now": "End of life",
              "consumable_soon": "Nearing end", "stale": "No signal"}
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
        extras.append(f"{len(yel)} station{'s' if len(yel) != 1 else ''} degraded")
    if not ds.is_demo and age_min > 10:
        return headline("serious", f"Status may be out of date: the last snapshot is {fmt_minutes(age_min)} old",
                        "Check that the collector is running (Activity shows monitoring gaps).")
    if len(red):
        reds = queue[queue["kind"] == "red"].sort_values("open_min", ascending=False)
        lead = reds.iloc[0] if len(reds) else None
        oldest = (f"Longest ongoing: {lead['station']}, out of service {fmt_minutes(lead['open_min'])}"
                  f"{'+' if lead['open_censored'] else ''} ({lead['issue'].lower()})") if lead is not None else ""
        title = f"{len(red)} of {n} stations out of service"
        return headline("critical", title, ". ".join([oldest] + extras) + ".")
    if len(yel):
        return headline("warning", f"All stations operational; {len(yel)} degraded",
                        ". ".join(extras) + "." if extras else "")
    detail = ". ".join(extras)
    return headline("good", f"All {n} stations operational",
                    (detail[:1].upper() + detail[1:] + ".") if extras else "Nothing needs attention right now.")


def render(ds: M.Dataset, theme: str, scope, basemap: str = "street"):
    if ds.empty:
        return empty("No snapshots yet. Start the collector (python -m wepa_monitor start) or generate demo "
                     "data (python -m wepa_monitor demo).")
    ids = scope_ids(ds, scope)
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
        tile("Operational", f"{n - red - stale} / {n}", f"{stale} with no signal" if stale else "stations able to print",
             tone="good" if red == 0 else None, href="/stations"),
        tile("Out of service", str(red), "stations that can't print", tone="critical" if red else None,
             href="/stations?status=red"),
        tile("Degraded", str(yel), "printing, but need a look", tone="warning" if yel else None,
             href="/stations?status=yellow"),
        tile("Availability, last 24 h", f"{a24.value:.1f}%" if a24.value is not None else "—",
             f"{a24.extra.get('down_h', 0):.1f} printer-hours down" if a24.value is not None else a24.note,
             compare=compare, ok=a24.ok),
        tile("Near end of life", str(due), "stations with a consumable due within 2 days"),
    ])

    # Needs attention: compact rows, each linking to the station.
    rows = []
    owner_of = ds.stations.set_index("station_id")["owner"].to_dict()
    for r in queue.head(10).itertuples(index=False):
        tone = KIND_TONE.get(r.kind, "info")
        age = (fmt_minutes(r.open_min) + ("+" if r.open_censored else "")) if r.open_min > 0 else ""
        owner = owner_of.get(r.station_id, config.DEFAULT_OWNER)
        waits = ""
        if r.kind in ("red", "yellow", "tray") and not support.is_open(owner, ds.as_of):
            waits = vocab.support_substate(owner, ds.as_of)
        rows.append(html.Li(className=f"todo todo--{tone}", children=[
            html.Span(KIND_LABEL.get(r.kind, r.kind), className=f"todo__tag tag tag--{tone}"),
            html.Div([station_link(r.station_id, r.station, "todo__station"),
                      html.Div(f"{r.issue} · {r.fix}", className="todo__issue")], className="todo__main"),
            html.Div([html.Div(age, className="todo__age"),
                      html.Div("only printer in building" if not r.backup else "", className="todo__note"),
                      html.Div(waits, className="todo__note") if waits else None],
                     className="todo__side"),
        ]))
    more = len(queue) - 10
    attention = chart_card(
        "Needs attention", "Ranked by severity, whether the building has a backup printer, and how long it's been open.",
        explain=["The list is ordered by a simple score: down beats warnings, warnings beat supplies, and items "
                 "in buildings with no backup printer get a 1.5× boost. Older items rise over time.",
                 "Work from the top. Click a station for its full history."],
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

    fc = models.eol_forecast(ds, ids)
    floor = fc["component"].map(models.replace_point)
    soon = fc[(fc["days"] <= 7) | (fc["level"] <= floor)].sort_values(["days", "level"])
    from .analytics import eol_window
    soon = soon.assign(window=soon.apply(eol_window, axis=1))
    parts = chart_card(
        "Consumable End-of-Life Watch",
        "Parts at, or projected to reach, their replacement point within 7 days. Projections are a regression on "
        "each part's readings since its last replacement (90% window in brackets).",
        wide=True, body=data_table(soon, [
            ("station", "Station", None), ("label", "Part", None), ("level", "Level", lambda v: f"{v:.0f}%"),
            ("window", "End of life in", None), ("area", "Area", None)],
            empty="No consumables are projected to reach end of life in the next 7 days.",
            link_col=("station", "station_id")))

    owners = [o for o in support.TEAMS if (cur["owner"] == o).any()]
    desks = html.Div([desk_line(ds, o, prefix="") for o in owners] + [campus_line(ds)], className="desks",
                     **{"aria-label": "Support desks"})
    return [
        status_headline(ds, cur, queue),
        desks,
        explore_hint(),
        tiles,
        html.Div(className="grid grid--2-1", children=[attention, recent]),
        _risk(ds, scope),
        html.Div(className="grid", children=[map_card, parts]),
    ]


def _risk(ds, scope):
    from .common import scope_ids
    from .risk_view import overview_card
    try:
        c = overview_card(ds, scope_ids(ds, scope))
    except Exception:  # noqa: BLE001 - the model must never break the overview
        return None
    return html.Div(className="grid", children=[c]) if c is not None else None


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
