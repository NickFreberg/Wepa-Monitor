"""Stations: every printer, searchable, grouped by area. Each card opens the station page."""
from __future__ import annotations

from dash import html

from ... import metrics as M, ops
from ..components import headline
from .common import area_key, empty, scope_ids, station_card

STATUS_FILTERS = {"all": "All", "red": "Down", "yellow": "Warning", "green": "Printing", "attention": "Needs attention"}


def render(ds: M.Dataset, sections, areas, query: str = "", status: str = "all"):
    if ds.empty:
        return empty("No snapshots yet.")
    ids = scope_ids(ds, sections, areas)
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
    groups = []
    for area in sorted(cur["area"].unique(), key=area_key):
        sub = cur[cur["area"] == area].sort_values(["building", "station_id"])
        n_red = int((sub["state"] == "red").sum())
        n_yel = int((sub["state"] == "yellow").sum())
        bits = [f"{len(sub)} station{'s' if len(sub) != 1 else ''}"]
        if n_red:
            bits.append(f"{n_red} down")
        if n_yel:
            bits.append(f"{n_yel} warning")
        groups.append(html.Section(className="board__group", children=[
            html.H3([area, html.Span(" · ".join(bits), className="board__count")]),
            html.Div(className="board__grid", children=[
                station_card(r, lv.loc[r["station_id"]].dropna().to_dict() if r["station_id"] in lv.index else {})
                for _, r in sub.iterrows()]),
        ]))
    shown = f"Showing {len(cur)} of {total} stations" if len(cur) != total else f"{total} stations"
    return [html.P(shown, className="result-count"), html.Div(groups, className="board")]
