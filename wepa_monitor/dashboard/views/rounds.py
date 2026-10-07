"""Rounds: plan the fastest route to every printer that needs a visit."""
from __future__ import annotations

from dash import dcc, html

import pandas as pd

from ... import accounts, auth, metrics as M, nearby, ops, reference, roundsmap, routing, support
from .. import charts
from ..components import chart_card, headline, segmented, station_link
from .common import empty

INCLUDE = {"red": "Out of service", "yellow": "Degraded", "tray": "Empty trays", "consumable_now": "Parts at end of life",
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
                                              {"label": "At the last stop", "value": "open"}]
                                + [{"label": f"At {roundsmap.place_name(p)}", "value": "end:" + roundsmap.place_name(p)}
                                   for p in roundsmap.points("home")], "loop",
                                persistence="local")], className="field"),
            html.Div([html.Label("Entrances", className="field__label"),
                      segmented("rd-access", [{"label": "Any door", "value": "any"},
                                              {"label": "Accessible doors", "value": "accessible"}], "any",
                                persistence="local")], className="field"),
        ]),
        html.A("Edit the Rounds map: Home Bases, doors, printers, parking, the vans, paths", href="/rounds/map",
               className="rounds-edit", hidden=not can_edit_map()),
    ]), dcc.Loading(html.Div(id="rd-body"), type="dot", delay_show=300)]


def can_edit_map() -> bool:
    return not auth.enabled() or accounts.current().get("role") == "admin"


def places() -> pd.DataFrame:
    """The campus buildings plus the Home Bases and supply closets drawn on the Rounds map, as rows Rounds can
    route to."""
    b = reference.load_buildings()
    drawn = [{"building": roundsmap.place_name(p), "short_name": roundsmap.place_name(p), "lat": p["lat"],
              "lon": p["lon"], "campus": "Main"} for p in roundsmap.points("home") + roundsmap.points("closet")]
    if not drawn:
        return b
    extra = pd.DataFrame(drawn)
    extra = extra[~extra["building"].isin(b["building"])].drop_duplicates("building")
    return pd.concat([b, extra], ignore_index=True)


def start_options(team: str) -> list[dict]:
    named = routing.TEAM_STARTS.get(team, [])
    opts = [{"label": n, "value": routing.STARTS[n]} for n in named]
    opts += [{"label": f"Home Base: {roundsmap.place_name(p)}", "value": roundsmap.place_name(p)}
             for p in roundsmap.points("home") if p.get("team") in ("", team)]
    opts += [{"label": f"Supply closet: {roundsmap.place_name(p)}", "value": roundsmap.place_name(p)}
             for p in roundsmap.points("closet")]
    taken = {o["value"] for o in opts}
    b = reference.load_buildings()
    b = b[(b["campus"] == "Main") & ~b["building"].isin(taken)].sort_values("building")
    return opts + [{"label": x, "value": x} for x in b["building"]]


def _fmt_m(m: float) -> str:
    return nearby.fmt_distance(m)


def _fmt_s(s: float) -> str:
    m = round(s / 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def map_extras() -> list[dict]:
    """Pins from the Rounds map worth seeing on a route: the vans, the fuel station and the supply closets."""
    out = [{"lat": p["lat"], "lon": p["lon"], "label": f"{p['team']} van (now)", "kind": "van"}
           for p in roundsmap.points("van")]
    out += [{"lat": p["lat"], "lon": p["lon"], "label": roundsmap.place_name(p), "kind": "closet"}
            for p in roundsmap.points("closet")]
    fuel = roundsmap.fuel_station()
    if fuel:
        out.append({"lat": fuel["lat"], "lon": fuel["lon"], "label": fuel["name"] or "Fuel station", "kind": "fuel"})
    return out


def render(ds: M.Dataset, theme: str, team: str, start: str, mode: str, include, order: str, finish: str,
           access: str = "any"):
    if ds.empty:
        return empty("No data yet.")
    if routing.network() is None:
        return empty("The campus map network isn't built yet. Run: python -m wepa_monitor network")
    ids = ds.ids(owner=team)
    q = ops.work_queue(ds, ids)
    q = q[q["kind"].isin(include or [])]
    b = places()
    if not start or start not in set(b["building"]):
        start = routing.STARTS[routing.TEAM_STARTS[team][0]]
    if q.empty:
        return [headline("good", f"Nothing on {team}'s list right now",
                         "No printer matches what you chose to visit. Widen 'Visit' to include parts due soon.")]
    end = finish[4:] if (finish or "").startswith("end:") else None
    accessible = access == "accessible"
    p = routing.plan(q, b, start, mode, round_trip=finish == "loop", urgent_first=order == "urgent", end=end,
                     team=team, accessible=accessible)
    if p is None or not p.order:
        return [headline("info", "Nothing to route", "The only items are off the main campus.")]
    alt = routing.plan(q, b, start, "van" if mode == "walk" else "walk", round_trip=finish == "loop",
                       urgent_first=order == "urgent", end=end, team=team, accessible=accessible)
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
            html.Div(html.Div([html.B(f"On to {p.end}" if p.end else f"Back to {p.start}"),
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
        drawn = int((park["park_source"] == "your Rounds map").sum())
        mine = int((park["park_source"] == "your parking list").sum())
        note += (f" Parking: {drawn} of {len(park)} stops use a van spot on your Rounds map, {mine} your parking "
                 "list (reference/parking.csv); the rest the nearest lot on OpenStreetMap.")
    van = roundsmap.van_location(team)
    if mode == "van" and van:
        note += f" {team}'s van is where your Rounds map says it is now, so the round starts by walking to it."
    elif mode == "van":
        note += f" {team}'s van has no location on the Rounds map, so the round starts from the nearest parking."
    doors = sum(1 for s in p.order if roundsmap.door_for(s.building))
    ada = sum(1 for s in p.order if any(d["building"] == s.building and d.get("accessible")
                                        for d in roundsmap.points("door")))
    if doors:
        note += f" {doors} stop{'s use' if doors != 1 else ' uses'} a door from your Rounds map"
        note += (f" ({ada} accessible)." if accessible else ".")
    if accessible and ada < len(p.order):
        note += (f" {len(p.order) - ada} stop{'s have' if len(p.order) - ada != 1 else ' has'} no accessible door "
                 "marked yet.")
    if p.skipped:
        note += f" Not routed (off the main campus): {', '.join(p.skipped)}."
    return [hl, html.Div(className="grid grid--2-1", children=[
        chart_card("Map", "Solid: on foot. Orange: transit van. Numbers are the order of stops.",
                   charts.route_map(theme, p, (float(st["lat"]), float(st["lon"])), map_extras()), note=note),
        html.Div(side, className="stack"),
    ])]
