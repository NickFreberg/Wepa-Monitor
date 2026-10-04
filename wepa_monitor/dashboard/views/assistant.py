"""The assistant: a chat pane docked to the right of every page, opened from the header.

The built-in rules always compute an answer from the monitoring data. When an AI model is connected
it works as an analyst on top of that: it gets the fact sheet, the built-in answer and read-only
tools over all of the data (analyst.Toolkit), digs in as far as the question needs, and every figure
in its reply is checked against what it was shown. The conversation lives in the browser tab
(session storage) and only the last few turns are sent along as context.
"""
from __future__ import annotations

import hashlib

import pandas as pd
from dash import dcc, html

from ... import ai, analyst, ask, metrics as M, narrative as N
from ..components import data_table, icon
from .common import scope_ids, scope_label

LOGO = "/assets/assistant.svg"
NAME = "Assistant"
HISTORY_TURNS = 6           # earlier messages sent along so follow-ups like "and last week?" make sense
MAX_MESSAGES = 40           # kept while the pane is open; older ones drop off


def button():
    return html.Button([html.Img(src=LOGO, alt="", className="assist-btn__logo"),
                        html.Span(NAME, className="assist-btn__label")],
                       id="assist-open", className="assist-btn", title=f"Open the {NAME} (Ctrl+I)",
                       **{"aria-label": f"Open the {NAME}", "aria-controls": "assist"})


def pane():
    return html.Aside(id="assist", className="assist", role="complementary", **{"aria-label": NAME}, children=[
        html.Div(className="assist__head", children=[
            html.Img(src=LOGO, alt="", className="assist__logo"),
            html.Div([html.Div(NAME, className="assist__title"),
                      html.Div(id="assist-by", className="assist__by")], className="assist__titles"),
            html.Button(icon("compose", "New chat"), id="assist-new", className="topbar__icon-btn", title="New chat"),
            html.Button(icon("x", "Close"), id="assist-close", className="topbar__icon-btn", title="Close"),
        ]),
        html.Div(id="assist-log", className="assist__log", **{"aria-live": "polite"}),
        html.Div(className="assist__compose", children=[
            html.Div(className="assist__box", children=[
                dcc.Input(id="assist-q", type="text", debounce=True, autoComplete="off", maxLength=400,
                          placeholder="Ask about outages, supplies, buildings…", className="assist__input"),
                html.Button(icon("send", "Send"), id="assist-send", className="assist__send", n_clicks=0),
            ]),
            html.P("AI-generated replies can be wrong. Check the numbers before acting on them.",
                   id="assist-disclaimer", className="assist__disclaimer"),
        ]),
        # Every opening starts a new chat, and nothing is written to browser storage: the conversation lives in
        # memory only. assist-session numbers the chat; replies carry it, so an answer that arrives after the
        # pane was reopened is dropped instead of bringing the old conversation back.
        dcc.Store(id="assist-chat", storage_type="memory", data=[]),
        dcc.Store(id="assist-session", storage_type="memory", data=0),
        dcc.Store(id="assist-reply", storage_type="memory"),
    ])


def provider_line() -> str:
    return f"Powered by {ai.label()}" if ai.enabled() else "Built-in answers · no AI connected"


def disclaimer() -> str:
    if ai.enabled():
        return f"Replies are written by {ai.label()} from this app's numbers and can be wrong. Printer data only."
    return "Replies come from built-in rules over the monitoring data. Nothing leaves the app."


# --- suggestions ----------------------------------------------------------------------------------

def suggestions(ds: M.Dataset, path: str | None) -> list[str]:
    path = (path or "/").rstrip("/") or "/"
    if path.startswith("/station/"):
        sid = path.rsplit("/", 1)[-1]
        row = ds.stations[ds.stations["station_id"] == sid]
        if len(row):
            b = row.iloc[0]["building"] or row.iloc[0]["short_name"]
            return [f"How is {b} doing this month?", f"Who supports {b}?", "What's down right now?"]
    by_page = {
        "/": ["What's down right now?", "How was yesterday?", "What toner will run out next?"],
        "/insights": ["Compare this week to last week", "When do jams happen most?", "How was yesterday?"],
        "/analytics": ["Which station was down the longest last week?", "Which printers get used the most?",
                       "When do jams happen most?"],
        "/rounds": ["What toner will run out next?", "Which buildings ran out of paper the most?",
                    "What's down right now?"],
        "/executive": ["How was yesterday?", "Compare this week to last week",
                       "How many outages start after hours?"],
        "/outcomes": ["Compare this week to last week", "Which printers get used the most?",
                      "How many outages start after hours?"],
    }
    return by_page.get(path, ask.EXAMPLES[:3])


# --- answering ------------------------------------------------------------------------------------

def reply(ds: M.Dataset, question: str, scope, path: str | None, history: list[dict], voice: str | None = None) -> dict:
    """One assistant message (a plain dict, so it can live in the browser's session store)."""
    ids = scope_ids(ds, scope)
    a = ask.answer(ds, question, ids)
    msg = {"role": "assistant", "understood": a.understood, "followups": a.followups[:3],
           "text": N.to_text(a.paragraphs), "ai": False, "by": "Built-in answer"}
    if a.table is not None and len(a.table):
        cols = [(c, h) for c, h in a.table_cols if c in a.table.columns]
        keep = [c for c, _ in cols] + (["station_id"] if "station_id" in a.table.columns else [])
        t = a.table[list(dict.fromkeys(keep))].head(10)
        msg["table"] = t.astype(object).where(pd.notna(t), None).to_dict("records")
        msg["cols"] = cols
    if not ai.enabled():
        return msg
    earlier = [m for m in history if m.get("role") in ("user", "assistant")][-HISTORY_TURNS:]
    convo = "\n".join(f"{'Staff member' if m['role'] == 'user' else 'You'}: {m.get('text', '')[:600]}"
                      for m in earlier)
    page = (path or "/").rstrip("/") or "/"
    label_ = scope_label(ds, scope)
    prompt = (f"You are the chat assistant docked beside the dashboard. They are on the '{page}' page, "
              f"looking at {label_}.\n"
              + (f"\nThe conversation so far:\n{convo}\n" if convo else "")
              + f"\nTheir new message: {question.strip()}\n\nThe dashboard's built-in answer ({a.understood}): "
              f"{msg['text']}\n\nReply as the expert analyst. Use the tools to check and go as deep as the "
              "question needs (the built-in answer can misread follow-ups). If it's outside BSU's printers, this "
              "data or the university context, say so kindly and offer what you can help with.")
    convo_key = hashlib.sha256(convo.encode()).hexdigest()[:16]
    try:
        r = ai.ask(prompt, ai.facts(ds, ids, label_), toolkit=analyst.Toolkit(ds, ids), effort="medium",
                   timeout=120, cache_key=f"assist|{question.strip().lower()}|{ids}|{convo_key}|"
                                         f"{ds.as_of.floor('15min').isoformat()}", voice=voice)
    except ai.AIError:
        msg["note"] = "The AI model isn't available right now, so this is the built-in answer."
        return msg
    msg.update(text=r.text, ai=True, by=r.provider, lookups=r.tools_used, unverified=list(r.unverified))
    return msg


# --- rendering -----------------------------------------------------------------------------------

def render(ds: M.Dataset, messages: list[dict], path: str | None):
    if not messages:
        return _welcome(ds, path)
    out = []
    for i, m in enumerate(messages):
        if m["role"] == "user":
            out.append(html.Div(html.Div(m["text"], className="bubble bubble--user"), className="turn turn--user"))
        elif m["role"] == "pending":
            out.append(html.Div([_avatar(), html.Div([html.Span(className="dot") for _ in range(3)],
                                                     className="bubble bubble--typing",
                                                     **{"aria-label": "Thinking"})],
                                className="turn turn--bot"))
        else:
            out.append(_bot(m, last=i == len(messages) - 1))
    return out


def _avatar():
    return html.Img(src=LOGO, alt="", className="turn__avatar")


def _bot(m: dict, last: bool):
    body = [dcc.Markdown(ai.safe_markdown(m.get("text") or ""), className="bubble__text")]
    if m.get("note"):
        body.insert(0, html.P(m["note"], className="bubble__note"))
    details = []
    if m.get("understood"):
        details.append(html.P([icon("info"), html.Span(m["understood"])], className="bubble__understood"))
    if m.get("table"):
        df = pd.DataFrame(m["table"])
        names = [c for c, _ in m["cols"]]
        link = None
        if "station_id" in df:
            link = ("station", "station_id") if "station" in names else (("label", "station_id")
                                                                         if "label" in names else None)
        details.append(data_table(df, [(c, h, None) for c, h in m["cols"]], max_rows=10, link_col=link))
    if details:
        body.append(html.Details([html.Summary("The numbers behind it")] + details, className="bubble__sources"))
    meta = html.Div([html.Span("AI-generated" if m.get("ai") else "Built-in answer", className="bubble__tag"),
                     html.Span(m.get("by", ""), className="bubble__by") if m.get("ai") else None,
                     _checked(m) if m.get("ai") else None], className="bubble__meta")
    chips = html.Div([html.Button(f, id={"type": "assist-ex", "q": f}, className="assist-chip", n_clicks=0)
                      for f in m.get("followups") or []], className="assist__chips") if last and m.get("followups") else None
    return html.Div([_avatar(), html.Div([html.Div(body, className="bubble bubble--bot"), meta, chips],
                                         className="turn__stack")], className="turn turn--bot")


def _checked(m: dict):
    if m.get("unverified"):
        return html.Span([icon("alert"), f"Couldn't verify {', '.join(m['unverified'][:4])}"],
                         className="bubble__check bubble__check--warn",
                         title="These figures weren't in the data the assistant looked at. Check them before acting.")
    n = m.get("lookups") or 0
    return html.Span([icon("check"), f"Fact-checked{f' · {n} lookups' if n > 1 else ' · 1 lookup' if n else ''}"],
                     className="bubble__check", title="Every figure in this reply was found in the monitoring data.")


def _welcome(ds: M.Dataset, path: str | None):
    return html.Div(className="assist__welcome", children=[
        html.Img(src=LOGO, alt="", className="assist__hero"),
        html.H2("Hi! How can I help?", className="assist__hello"),
        html.P("I know the live data for BSU's print stations: what's down, outages and their causes, "
               "supplies running low, how busy each printer is, and the report card.", className="assist__intro"),
        html.Div([html.Button([icon("sparkle"), html.Span(q)], id={"type": "assist-ex", "q": q},
                              className="assist-card", n_clicks=0) for q in suggestions(ds, path)],
                 className="assist__cards"),
    ])
