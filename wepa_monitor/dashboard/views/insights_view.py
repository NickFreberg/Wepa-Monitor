"""Insights: the story of any period, and plain-English questions answered from the data."""
from __future__ import annotations

from dash import dcc, html

from ... import ask, metrics as M, narrative as N
from ..components import data_table, headline, icon, prose, segmented
from .common import empty, scope_ids
from .overview import activity_list


def scope_label(ds: M.Dataset, sections, areas) -> str:
    chosen = (sections or []) + (areas or [])
    if not chosen:
        return "BSU print stations"
    if len(chosen) == 1:
        c = chosen[0]
        return {"ResNet": "residence hall stations", "Student Computer Labs": "lab stations",
                "Satellite Campuses": "satellite stations"}.get(c, f"{c} stations")
    return "the selected stations"


def layout(story_period: str = "yesterday"):
    return [
        html.Section(className="card insight-card", children=[
            html.Header(className="card__head", children=[
                html.Div([html.H3("The story"),
                          html.P("What happened, in plain words, for any stretch of time.", className="card__sub")]),
            ]),
            segmented("story-period", [{"label": t, "value": k} for k, t in N.PERIOD_KEYS], story_period,
                      persistence="session"),
            dcc.Loading(html.Div(id="story-body", className="story"), type="dot", delay_show=400),
        ]),
        html.Section(className="card insight-card", children=[
            html.Header(className="card__head", children=[
                html.Div([html.H3("Ask the data"),
                          html.P("Ask a question in plain English about stations, buildings, times or supplies.",
                                 className="card__sub")]),
            ]),
            html.Div(className="ask", children=[
                html.Div([icon("search"), dcc.Input(id="ask-q", type="text", debounce=True,
                                                    placeholder="e.g. Which station was down the longest last week?",
                                                    className="search__input", autoComplete="off")],
                         className="search search--ask"),
                html.Button("Ask", id="ask-go", className="btn btn--primary", n_clicks=0),
            ]),
            html.Div([html.Span("Try: ", className="ask__try")] + [
                html.Button(q, id={"type": "ask-ex", "q": q}, className="chip-btn", n_clicks=0) for q in ask.EXAMPLES[:6]
            ], className="ask__examples"),
            dcc.Loading(html.Div(id="ask-body", className="ask__answer"), type="dot", delay_show=300),
            html.P([icon("info"), " Answers are computed from the monitoring data with built-in rules. There's no "
                    "AI model, and nothing leaves the app. If an answer misreads your question, rephrase it with a "
                    "station, building or time range."], className="footnote footnote--icon"),
        ]),
    ]


def render_story(ds: M.Dataset, key: str, sections, areas):
    if ds.empty:
        return empty("No data yet.")
    p = N.period(ds, key or "yesterday")
    st = N.story(ds, p, scope_ids(ds, sections, areas), scope_label(ds, sections, areas))
    chips = html.Div([html.Div([html.Span(v, className="stat__value"), html.Span(k, className="stat__label")],
                               className="stat") for k, v in st.stats], className="stats")
    moments = st.moments
    return [
        headline(st.tone, st.headline),
        chips,
        prose(st.paragraphs),
        html.Details([html.Summary(f"Key moments ({len(moments)})"),
                      activity_list(moments, show_date=key not in ("today", "yesterday"))],
                     className="card__data") if moments is not None and len(moments) else None,
    ]


def render_answer(ds: M.Dataset, question: str, sections, areas):
    if not (question or "").strip():
        return html.P("Ask anything about the print stations, or pick an example above.", className="empty")
    a = ask.answer(ds, question, scope_ids(ds, sections, areas))
    table = None
    if a.table is not None and len(a.table):
        table = data_table(a.table, [(c, h, None) for c, h in a.table_cols], max_rows=10,
                           link_col=("station", "station_id") if "station" in [c for c, _ in a.table_cols]
                           and "station_id" in a.table else
                           (("label", "station_id") if "label" in [c for c, _ in a.table_cols]
                            and "station_id" in a.table else None))
    return [
        html.Div([icon("info"), html.Span(a.understood)], className="ask__understood"),
        prose(a.paragraphs, className="prose prose--answer"),
        table,
        html.Div([html.Span("Ask next: ", className="ask__try")] + [
            html.Button(f, id={"type": "ask-ex", "q": f}, className="chip-btn", n_clicks=0) for f in a.followups[:3]
        ], className="ask__examples") if a.followups else None,
    ]
