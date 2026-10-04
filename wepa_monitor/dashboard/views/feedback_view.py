"""Suggest a feature: a short form that files a GitHub issue, and the person's requests with their status."""
from __future__ import annotations

import pandas as pd
from dash import dcc, html

from ... import accounts, auth, config, feedback
from ..components import chart_card, icon, segmented

STATE_TONE = {"Queued": "neutral", "Sent": "info", "open": "info", "closed": "good"}


def layout(params: dict):
    page = params.get("from", "")
    return [
        dcc.Store(id="fb-page", data=page),
        html.Section(className="card", children=[
            html.H3("Suggest a feature or report a problem"),
            html.P(["Your note goes to the people who build this app, as an issue in its GitHub repository, with a "
                    "reference number so you can follow it here. ",
                    html.B("Don't include passwords or personal information about anyone"),
                    ": issues can be read by anyone with access to the repository."], className="muted"),
            html.Div(className="fb-form", children=[
                html.Label("What kind of feedback?", htmlFor="fb-kind"),
                segmented("fb-kind", [{"label": label, "value": k} for k, (label, _) in feedback.KINDS.items()],
                          "feature"),
                html.Label("Title", htmlFor="fb-title"),
                dcc.Input(id="fb-title", type="text", maxLength=feedback.TITLE_MAX,
                          placeholder="e.g. Show toner levels on the student status page"),
                html.Label("Details", htmlFor="fb-body"),
                dcc.Textarea(id="fb-body", maxLength=feedback.BODY_MAX,
                             placeholder="What would you like, and what would it help you do? For a problem: what "
                                         "you did, what happened, and what you expected."),
                html.Div([html.Button([icon("send"), html.Span("Send")], id="fb-send", className="btn btn--primary",
                                      n_clicks=0),
                          html.Span(f"Up to {feedback.PER_DAY} a day.", className="inv-hint")],
                         className="acct-row"),
                html.Div(id="fb-msg", className="inv-msg", role="status"),
            ]),
        ]),
        dcc.Loading(html.Div(id="fb-list"), type="dot", delay_show=600),
    ]


def _when(ts) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(config.LOCAL_TZ).strftime("%b %-d, %Y %-I:%M %p") \
        if ts else "—"


def render_list(data_dir):
    me = accounts.current()
    admin = me.get("role") == "admin" or not auth.enabled()
    rows_ = feedback.all_requests(data_dir) if admin else feedback.mine(data_dir, me.get("username", ""))
    if not rows_:
        return chart_card("Your requests", "Nothing sent yet.", wide=True,
                          body=html.P("Requests you send appear here with their GitHub status.", className="card__note"))
    rows = []
    for r in rows_[:100]:
        gh = feedback.issue_status(r)
        if r.get("state") == "Sent" and gh.get("state"):
            word = "Done" if gh["state"] == "closed" and gh.get("reason") == "completed" else \
                "Closed" if gh["state"] == "closed" else "Open"
            tone = "good" if word == "Done" else "neutral" if word == "Closed" else "info"
        else:
            word, tone = ("Sent", "info") if r.get("state") == "Sent" else ("Waiting to send", "neutral")
        rows.append(html.Tr([
            html.Td(html.Span(r["ref"], className="mono")),
            html.Td([html.B(r["title"]), html.Div(feedback.KINDS[r["kind"]][0], className="muted")]),
            html.Td(html.Span(word, className=f"inv-state inv-state--{tone}"),
                    title=r.get("error") or ""),
            html.Td(html.A(f"#{r['issue']}", href=r["url"], target="_blank", rel="noopener noreferrer",
                           className="link") if r.get("url") else "—"),
            html.Td(_when(r.get("created"))),
            html.Td(r.get("name", "")) if admin else None,
        ]))
    head = ["Reference", "Request", "Status", "GitHub issue", "Sent"] + (["From"] if admin else [])
    note = ("Everyone's requests (you're the administrator)." if admin else "Your requests, newest first.")
    if not feedback.token():
        note += " GitHub sending isn't set up on this copy yet (WEPA_GITHUB_ISSUES_TOKEN), so requests are kept here."
    return chart_card("Requests", note, wide=True, body=html.Div(html.Table(
        [html.Thead(html.Tr([html.Th(h) for h in head])), html.Tbody(rows)], className="table"),
        className="table-wrap"))


def act(data_dir, kind, title, body, page):
    me = accounts.current()
    try:
        r = feedback.submit(data_dir, me, kind or "feature", title or "", body or "", page or "")
    except feedback.FeedbackError as exc:
        return str(exc), "err", False
    if r.get("issue"):
        return f"Thank you. {r['ref']} is GitHub issue #{r['issue']}.", "ok", True
    return f"Thank you. {r['ref']} is saved and will be sent to GitHub when it can be.", "ok", True
