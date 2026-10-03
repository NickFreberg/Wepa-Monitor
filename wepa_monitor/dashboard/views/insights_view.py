"""Insights: the story of any period, and plain-English questions answered from the data."""
from __future__ import annotations

from dash import dcc, html

from ... import ai, analyst, ask, metrics as M, narrative as N
from ..components import data_table, headline, icon, prose, segmented
from .common import empty, scope_ids, scope_label
from .overview import activity_list


def layout(story_period: str = "yesterday"):
    return [
        html.Section(className="card insight-card", children=[
            html.Header(className="card__head", children=[
                html.Div([html.H3("The story"),
                          html.P("What happened, in plain words, for any stretch of time.", className="card__sub")]),
            ]),
            segmented("story-period", [{"label": t, "value": k} for k, t in N.PERIOD_KEYS], story_period,
                      persistence="session"),
            html.Div(id="story-ai", className="ai-note-slot"),
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
            html.P([icon("info"), footnote()], className="footnote footnote--icon"),
        ]),
    ]


def render_story(ds: M.Dataset, key: str, scope):
    if ds.empty:
        return empty("No data yet.")
    p = N.period(ds, key or "yesterday")
    st = N.story(ds, p, scope_ids(ds, scope), scope_label(ds, scope))
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


def render_answer(ds: M.Dataset, question: str, scope):
    if not (question or "").strip():
        return html.P("Ask anything about the print stations, or pick an example above.", className="empty")
    ids = scope_ids(ds, scope)
    a = ask.answer(ds, question, ids)
    table = None
    if a.table is not None and len(a.table):
        table = data_table(a.table, [(c, h, None) for c, h in a.table_cols], max_rows=10,
                           link_col=("station", "station_id") if "station" in [c for c, _ in a.table_cols]
                           and "station_id" in a.table else
                           (("label", "station_id") if "label" in [c for c, _ in a.table_cols]
                            and "station_id" in a.table else None))
    rules_part = [html.Div([icon("info"), html.Span(a.understood)], className="ask__understood"),
                  prose(a.paragraphs, className="prose prose--answer"), table]
    nxt = html.Div([html.Span("Ask next: ", className="ask__try")] + [
        html.Button(f, id={"type": "ask-ex", "q": f}, className="chip-btn", n_clicks=0) for f in a.followups[:3]
    ], className="ask__examples") if a.followups else None
    if ai.enabled():
        label_ = scope_label(ds, scope)
        computed = N.to_text(a.paragraphs)
        try:
            r = ai.ask(f"Question from a staff member: {question.strip()}\n\nThe dashboard's built-in answer "
                       f"({a.understood}): {computed}\n\nAnswer them as an expert analyst. Use the tools to check and "
                       "go deeper where it helps (the built-in answer can misread a question).",
                       ai.facts(ds, ids, label_), toolkit=analyst.Toolkit(ds, ids), effort="medium", timeout=120,
                       cache_key=f"ask|{question.strip().lower()}|{ids}|{ds.as_of.floor('15min').isoformat()}")
            return [ai_card(r.text, r.provider, "Answer", r),
                    html.Details([html.Summary("The numbers behind it")] + rules_part, className="card__data ask__numbers"),
                    nxt]
        except ai.AIError:
            rules_part.insert(0, html.P("The AI assistant isn't available right now, so this answer uses the "
                                        "built-in rules.", className="footnote"))
    return rules_part + [nxt]


def footnote() -> str:
    if ai.enabled():
        return (f" Answers are written by {ai.label()} from numbers this app computes; the AI never sees raw data "
                "and can't change anything. Open 'The numbers behind it' under any answer to check them. Printer "
                "data only: nothing about students or visitors is sent.")
    return (" Answers are computed from the monitoring data with built-in rules; no AI model is connected, and "
            "nothing leaves the app. If an answer misreads your question, rephrase it with a station, building or "
            "time range.")


def ai_card(text: str, provider: str, title: str = "In a nutshell", reply=None) -> html.Div:
    return html.Div(className="ai-note", role="note", children=[
        html.Div([icon("sparkle"), html.Span(title), html.Span(f"written by {provider} from the monitoring data",
                                                               className="ai-note__by")], className="ai-note__head"),
        dcc.Markdown(text, className="ai-note__text"),
        check_line(reply) if reply is not None else None,
    ])


def check_line(r) -> html.Div:
    """How the reply was grounded: data lookups made, and the fact check's result."""
    looked = f"Looked at the data {r.tools_used} time{'s' if r.tools_used != 1 else ''}. " if r.tools_used else ""
    if r.unverified:
        return html.Div([icon("alert"), html.Span(f"{looked}Couldn't verify: {', '.join(r.unverified[:6])}. "
                                                  "Check those figures before relying on them.")],
                        className="fact-check fact-check--warn")
    return html.Div([icon("check"), html.Span(f"{looked}Every figure checked against the data.")],
                    className="fact-check")


def render_story_ai(ds: M.Dataset, key: str, scope):
    """An AI summary of the period's story: what it meant and what to do. Empty when AI is off or fails."""
    if ds.empty or not ai.enabled():
        return None
    p = N.period(ds, key or "yesterday")
    ids, label_ = scope_ids(ds, scope), scope_label(ds, scope)
    stamp = ds.as_of.floor("15min").isoformat()
    try:
        r = ai.ask(f"In 2-4 sentences, tell a ResNet supervisor what {p.label} was like for {label_}: the one or two "
                   "things that mattered most for students, and one practical next step. Don't repeat every number. "
                   "Check the tools if something in the facts needs a reason or context.",
                   ai.facts(ds, ids, label_, p), cache_key=f"story|{key}|{scope}|{stamp}",
                   toolkit=analyst.Toolkit(ds, ids), timeout=90)
    except ai.AIError:
        return None
    return ai_card(r.text, r.provider, reply=r)
