"""Activity: a searchable, filterable timeline of everything that happened."""
from __future__ import annotations

import pandas as pd
from dash import html

from ... import activity, config, metrics as M
from ..components import headline
from .common import empty, period_label, period_window, scope_ids
from .overview import activity_list

GROUPS = ["Status", "Paper", "Parts", "Monitoring"]
MAX_ROWS = 400


def render(ds: M.Dataset, sections, areas, period, groups, query):
    if ds.empty:
        return empty("No data yet.")
    ids = scope_ids(ds, sections, areas)
    start, end = period_window(ds, period)
    ev = activity.events(ds, since=start, ids=ids)
    groups = groups or GROUPS
    ev = ev[ev["kind"].map(activity.KIND_GROUP).isin(groups)]
    q = (query or "").strip().lower()
    if q:
        hay = (ev["title"] + " " + ev["detail"] + " " + ev["building"] + " " + ev["station_id"]).str.lower()
        ev = ev[hay.str.contains(q, regex=False)]
    if ev.empty:
        return [headline("info", "No activity matches", "Widen the period or clear the filters.")]

    counts = ev["kind"].value_counts()
    parts = [f"{counts[k]:,} {text}" for k, text in (
        ("down", "times a station went down"), ("warning", "warnings"), ("replaced", "parts replaced"),
        ("tray_empty", "trays ran empty"), ("data_gap", "monitoring gaps")) if counts.get(k, 0)]
    summary = f"{len(ev):,} events in the last {period_label(period)}" + (": " + ", ".join(parts) if parts else "") + "."
    shown = ev.head(MAX_ROWS)
    local_day = shown["ts"].dt.tz_convert(config.LOCAL_TZ).dt.date
    today = ds.as_of.tz_convert(config.LOCAL_TZ).date()
    blocks = []
    for day, chunk in shown.groupby(local_day, sort=False):
        label = "Today" if day == today else ("Yesterday" if (today - day).days == 1 else
                                              pd.Timestamp(day).strftime("%A, %B %-d"))
        blocks.append(html.Section([html.H3(label, className="feed-day"), activity_list(chunk)],
                                   className="feed-block"))
    note = (html.P(f"Showing the most recent {MAX_ROWS} of {len(ev):,}. Narrow the period or search to see older ones.",
                   className="result-count") if len(ev) > MAX_ROWS else None)
    return [html.P(summary, className="result-count"), html.Div(blocks, className="card card--flush"), note]
