"""Activity: a searchable timeline of everything that happened, on any dates you pick."""
from __future__ import annotations

import pandas as pd
from dash import dcc, html

from ... import activity, config, metrics as M
from ..components import headline, icon, segmented
from .common import empty, scope_ids
from .overview import activity_list

GROUPS = ["Status", "Paper", "Parts", "Monitoring", "System"]
MAX_ROWS = 400
QUICK = [("Today", 0), ("Last 7 days", 7), ("Last 30 days", 30), ("Everything", -1)]
TZ = config.LOCAL_TZ


def _today(ds: M.Dataset):
    return ds.as_of.tz_convert(TZ).date()


def _first(ds: M.Dataset):
    return (ds.data_start or ds.as_of).tz_convert(TZ).date()


def quick_range(ds: M.Dataset, days: int) -> tuple[str, str]:
    today = _today(ds)
    start = _first(ds) if days < 0 else today - pd.Timedelta(days=days)
    return str(max(start, _first(ds))), str(today)


def layout(ds: M.Dataset, params: dict):
    d0, d1 = quick_range(ds, 7)
    return [html.Div(className="toolbar toolbar--activity", children=[
        html.Div([icon("calendar"),
                  dcc.DatePickerRange(id="ac-dates", start_date=params.get("from", d0), end_date=params.get("to", d1),
                                      min_date_allowed=str(_first(ds)), max_date_allowed=str(_today(ds)),
                                      display_format="MMM D, YYYY", first_day_of_week=0, clearable=False,
                                      minimum_nights=0, updatemode="bothdates", className="dates",
                                      start_date_placeholder_text="From", end_date_placeholder_text="To",
                                      persistence=True, persistence_type="session")],
                 className="dates-wrap", title="Choose dates"),
        html.Div([html.Button(t, id={"type": "ac-quick", "days": d}, className="chip-btn", n_clicks=0)
                  for t, d in QUICK], className="quick"),
        html.Div([icon("search"), dcc.Input(id="ac-q", type="search", placeholder="Search activity",
                                            value=params.get("q", ""), debounce=0.25, className="search__input")], className="search"),
        segmented("ac-groups", [{"label": g, "value": g} for g in GROUPS], ["Status", "Parts", "Monitoring", "System"],
                  multi=True),
    ]), dcc.Loading(html.Div(id="ac-body"), type="dot", delay_show=500)]


def render(ds: M.Dataset, scope, d0, d1, groups, query):
    if ds.empty:
        return empty("No data yet.")
    ids = scope_ids(ds, scope)
    first, today = _first(ds), _today(ds)
    a = pd.Timestamp(d0 or first).date()
    b = pd.Timestamp(d1 or today).date()
    if b < a:
        a, b = b, a
    start = pd.Timestamp(a).tz_localize(TZ).tz_convert("UTC")
    end = (pd.Timestamp(b) + pd.Timedelta(days=1)).tz_localize(TZ).tz_convert("UTC")
    ev = activity.events(ds, since=start, ids=ids)
    ev = ev[ev["ts"] < end]
    groups = groups or GROUPS
    ev = ev[ev["kind"].map(activity.KIND_GROUP).isin(groups)]
    q = (query or "").strip().lower()
    if q:
        hay = (ev["title"] + " " + ev["detail"] + " " + ev["building"] + " " + ev["station_id"]).str.lower()
        ev = ev[hay.str.contains(q, regex=False)]
    span = (f"{pd.Timestamp(a):%a %b %-d}" if a == b else
            f"{pd.Timestamp(a):%b %-d} – {pd.Timestamp(b):%b %-d, %Y}")
    if ev.empty:
        return [headline("info", f"No activity matches for {span}", "Pick other dates, or clear the search and filters.")]

    counts = ev["kind"].value_counts()
    parts = [f"{counts[k]:,} {text}" for k, text in (
        ("down", "outages"), ("warning", "warnings"), ("replaced", "parts replaced"),
        ("tray_empty", "trays ran empty"), ("data_gap", "monitoring gaps"),
        ("software_updated", "software updates")) if counts.get(k, 0)]
    summary = f"{len(ev):,} events, {span}" + (": " + ", ".join(parts) if parts else "") + "."
    shown = ev.head(MAX_ROWS)
    local_day = shown["ts"].dt.tz_convert(TZ).dt.date
    blocks = []
    for day, chunk in shown.groupby(local_day, sort=False):
        label = "Today" if day == today else ("Yesterday" if (today - day).days == 1 else
                                              pd.Timestamp(day).strftime("%A, %B %-d"))
        blocks.append(html.Section([html.H3(label, className="feed-day"), activity_list(chunk)],
                                   className="feed-block"))
    note = (html.P(f"Showing the most recent {MAX_ROWS} of {len(ev):,}. Narrow the dates or search to see older ones.",
                   className="result-count") if len(ev) > MAX_ROWS else None)
    return [html.P(summary, className="result-count"), html.Div(blocks, className="card card--flush"), note]
