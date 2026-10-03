"""Stations: every printer, searchable, grouped by area. Each card opens the station page."""
from __future__ import annotations

from dash import html

from ... import metrics as M, ops
from ..components import headline, icon
from .common import area_key, empty, scope_ids, station_card

STATUS_FILTERS = {"all": "All", "red": "Out of service", "yellow": "Degraded", "green": "Operational", "attention": "Needs attention"}


def render(ds: M.Dataset, scope, query: str = "", status: str = "all"):
    if ds.empty:
        return empty("No snapshots yet.")
    ids = scope_ids(ds, scope)
    cur = ops.current_status(ds, ids)
    if cur.empty:
        return empty("No stations in this scope.")
    total = len(cur)

    q = (query or "").strip().lower()
    if q:
        hay = (cur["description"] + " " + cur["building"] + " " + cur["station_id"] + " " + cur["area"]).str.lower()
        cur = cur[hay.str.contains(q, regex=False)]
    if status in ("red", "yellow", "green"):
        cur = cur[cur["state"] == status]
    elif status == "attention":
        flagged = set(ops.work_queue(ds, ids)["station_id"])
        cur = cur[cur["station_id"].isin(flagged)]

    if cur.empty:
        return [headline("info", "No stations match", "Try a different search or status filter.")]

    lv = ds.levels.pivot_table(index="station_id", columns="component", values="level", aggfunc="last")
    levels = lambda sid: lv.loc[sid].dropna().to_dict() if sid in lv.index else {}  # noqa: E731
    groups = []
    for area in sorted(cur["area"].unique(), key=area_key):
        sub = cur[cur["area"] == area].sort_values(["building", "station_id"])
        n_red = int((sub["state"] == "red").sum())
        n_yel = int((sub["state"] == "yellow").sum())
        bits = [f"{len(sub)} station{'s' if len(sub) != 1 else ''}"]
        if n_red:
            bits.append(f"{n_red} out of service")
        if n_yel:
            bits.append(f"{n_yel} degraded")
        tiles = []
        for building, b in sub.groupby("building", sort=False):
            if len(b) == 1:
                r = b.iloc[0]
                tiles.append(station_card(r, levels(r["station_id"])))
                continue
            # Several printers in one building: one bucket, every card intact inside it.
            up = int((~b["state"].isin(["red", "stale"])).sum())
            tone = "good" if up == len(b) else ("critical" if up == 0 else "warning")
            tiles.append(html.Section(className=f"bucket bucket--{tone} bucket--n{min(len(b), 3)}",
                                      **{"aria-label": f"{building}: {len(b)} printers"}, children=[
                html.Header(className="bucket__head", children=[
                    html.Span(icon("building"), className="bucket__icon"),
                    html.Div([html.Div(building, className="bucket__name"),
                              html.Div(f"{up} of {len(b)} printers working", className="bucket__meta")]),
                ]),
                html.Div([station_card(r, levels(r["station_id"]), in_group=True) for _, r in b.iterrows()],
                         className="bucket__cards"),
            ]))
        groups.append(html.Section(className="board__group", children=[
            html.H3([area, html.Span(" · ".join(bits), className="board__count")]),
            html.Div(className="board__grid", children=tiles),
        ]))
    shown = f"Showing {len(cur)} of {total} stations" if len(cur) != total else f"{total} stations"
    signs = html.A([icon("printer"), html.Span("Printable QR signs for students")], href="/status/signs",
                   target="_blank", className="link result-signs",
                   title="One sign per printer: scanning it opens that printer's live status and the nearest "
                         "working printers")
    return [html.Div([html.P(shown, className="result-count"), signs], className="result-bar"),
            html.Div(groups, className="board")]
