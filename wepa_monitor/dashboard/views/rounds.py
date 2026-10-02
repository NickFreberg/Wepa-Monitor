"""Rounds: plan the fastest route to every printer that needs a visit."""
from __future__ import annotations

from dash import dcc, html

from ... import metrics as M, ops, reference, routing, support
from .. import charts
from ..components import chart_card, headline, segmented, station_link
from .common import empty

INCLUDE = {"red": "Down", "yellow": "Warnings", "tray": "Empty trays", "consumable_now": "Parts at end of life",
           "consumable_soon": "Parts due soon"}


def layout(team: str = "ResNet"):
    team = team if team in support.TEAMS else "ResNet"
    return [html.Section(className="card rounds-controls", children=[
        html.Div(className="rounds-grid", children=[
            html.Div([html.Label("Team", className="field__label"),
                      segmented("rd-team", [{"label": t, "value": t} for t in support.TEAMS], team,
                                persistence="local")], className="field"),
            html.Div([html.Label("Starting from", className="field__label", htmlFor="rd-start"),
                      dcc.Dropdown(id="rd-start", clearable=False, className="dropdown",
                                   persistence=True, persistence_type="local")], className="field"),
            html.Div([html.Label("Getting around", className="field__label"),
                      segmented("rd-mode", [{"label": "On foot", "value": "walk"},
                                            {"label": "Transit van", "value": "van"}], "walk",
                                persistence="local")], className="field"),
            html.Div([html.Label("Visit", className="field__label"),
                      segmented("rd-include", [{"label": v, "value": k} for k, v in INCLUDE.items()],
                                ["red", "yellow", "tray", "consumable_now"], multi=True, persistence="local")],
                     className="field field--wide"),
            html.Div([html.Label("Order", className="field__label"),
                      segmented("rd-order", [{"label": "Down printers first", "value": "urgent"},
                                             {"label": "Shortest overall", "value": "shortest"}], "urgent",
                                persistence="local")], className="field"),
            html.Div([html.Label("Finish", className="field__label"),
                      segmented("rd-return", [{"label": "Back at start", "value": "loop"},
                                              {"label": "At the last stop", "value": "open"}], "loop",
                                persistence="local")], className="field"),
        ]),
    ]), dcc.Loading(html.Div(id="rd-body"), type="dot", delay_show=300)]


def start_options(team: str) -> list[dict]:
    named = routing.TEAM_STARTS.get(team, [])
    opts = [{"label": n, "value": routing.STARTS[n]} for n in named]
    taken = {o["value"] for o in opts}
    b = reference.load_buildings()
    b = b[(b["campus"] == "Main") & ~b["building"].isin(taken)].sort_values("building")
    return opts + [{"label": x, "value": x} for x in b["building"]]


def _fmt_m(m: float) -> str:
    return f"{m:,.0f} m" if m < 1000 else f"{m / 1609.34:.1f} mi"


def _fmt_s(s: float) -> str:
    m = round(s / 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def render(ds: M.Dataset, theme: str, team: str, start: str, mode: str, include, order: str, finish: str):
    if ds.empty:
        return empty("No data yet.")
    if routing.network() is None:
        return empty("The campus map network isn't built yet. Run: python -m wepa_monitor network")
    ids = ds.ids(owner=team)
    q = ops.work_queue(ds, ids)
    q = q[q["kind"].isin(include or [])]
    b = reference.load_buildings()
    if not start or start not in set(b["building"]):
        start = routing.STARTS[routing.TEAM_STARTS[team][0]]
    if q.empty:
        return [headline("good", f"Nothing on {team}'s list right now",
                         "No printer matches what you chose to visit. Widen 'Visit' to include parts due soon.")]
    p = routing.plan(q, b, start, mode, round_trip=finish == "loop", urgent_first=order == "urgent")
    if p is None or not p.order:
        return [headline("info", "Nothing to route", "The only items are off the main campus.")]
    alt = routing.plan(q, b, start, "van" if mode == "walk" else "walk", round_trip=finish == "loop",
                       urgent_first=order == "urgent")
    total = p.travel_s + p.service_s
    hint = ""
    if alt and alt.travel_s + 60 < p.travel_s:
        hint = (f" {'Walking' if mode == 'van' else 'The van'} would be faster for this round: "
                f"{_fmt_s(alt.travel_s)} of travel instead of {_fmt_s(p.travel_s)}.")
    n_down = sum(1 for s in p.order if s.priority == 0)
    hl = headline("critical" if n_down else "info",
                  f"{len(p.order)} stop{'s' if len(p.order) != 1 else ''} · about {_fmt_s(total)} "
                  f"({_fmt_s(p.travel_s)} {'walking' if mode == 'walk' else 'driving and walking'}, "
                  f"{_fmt_s(p.service_s)} on site)",
                  f"{_fmt_m(p.meters)} in all, starting from {start}."
                  + (f" {n_down} building{'s have' if n_down != 1 else ' has'} a down printer"
                     f"{', visited first' if order == 'urgent' else ''}." if n_down else "") + hint)

    steps = []
    for i, (s, legs) in enumerate(zip(p.order, p.legs), start=1):
        how = " → ".join(("walk " if l.mode == "walk" else "drive ") + _fmt_m(l.meters)
                         + (" to the van" if l.to == "the van" else "") for l in legs) or "same building"
        steps.append(html.Li(className=f"route__stop route__stop--{'down' if s.priority == 0 else 'other'}", children=[
            html.Span(str(i), className="route__num"),
            html.Div([html.Div([html.B(s.building), html.Span(f" · {how} · {_fmt_s(sum(l.seconds for l in legs))}",
                                                             className="route__how")]),
                      html.Ul([html.Li([station_link(it["station_id"], it["station"]), f": {it['issue']}",
                                        html.Span(f" · {it['fix']}", className="route__fix")])
                               for it in s.items], className="route__items")], className="route__body"),
        ]))
    if p.back:
        steps.append(html.Li(className="route__stop route__stop--home", children=[
            html.Span("↩", className="route__num"),
            html.Div(html.Div([html.B(f"Back to {p.start}"),
                               html.Span(f" · {_fmt_m(sum(l.meters for l in p.back))} · "
                                         f"{_fmt_s(sum(l.seconds for l in p.back))}", className="route__how")]),
                     className="route__body")]))
    bring = routing.supplies(p)
    st = b.set_index("building").loc[start]
    side = [
        chart_card("The route", "Stops in order. Times include walking in from the van in van mode.",
                   body=html.Ol(steps, className="route")),
        chart_card("What to bring", "From the issues on this round.",
                   body=html.Ul([html.Li(x) for x in bring], className="observations") if bring else
                   html.P("Nothing special: no paper-outs or part swaps on this round.", className="card__note")),
    ]
    note = ("Paths and roads: © OpenStreetMap contributors. Shortest paths by Dijkstra's algorithm; stop order "
            "optimized for total time" + (", down printers first" if order == "urgent" else "") + ".")
    if mode == "van":
        park = routing.parking_spots(b[b["building"].isin([s.building for s in p.order])])
        mine = (park["park_source"] == "your parking list").sum()
        note += (f" Parking: {mine} of {len(park)} stops use your parking list; the rest use the nearest lot on "
                 "OpenStreetMap (reference/parking.csv).")
    if p.skipped:
        note += f" Not routed (off the main campus): {', '.join(p.skipped)}."
    return [hl, html.Div(className="grid grid--2-1", children=[
        chart_card("Map", "Solid: on foot. Orange: transit van. Numbers are the order of stops.",
                   charts.route_map(theme, p, (float(st["lat"]), float(st["lon"]))), note=note),
        html.Div(side, className="stack"),
    ])]
