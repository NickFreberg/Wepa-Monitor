"""Investigations page: the list (open, closed, archived) and one investigation's record."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from dash import dcc, html

from ... import accounts, config, investigations as I, metrics as M, refs, rules
from ..components import chart_card, data_table, icon, segmented
from .common import empty

FILTERS = [("open", "Open"), ("closed", "Closed"), ("archived", "Archived"), ("all", "All"), ("metrics", "Metrics")]
STATE_TONE = {I.NEW: "info", I.ANALYZE: "warning", I.RESPOND: "warning", I.REVIEW: "serious",
              I.CLOSED_COMPLETE: "good", I.CLOSED_CANCELLED: "neutral", I.CLOSED_INCOMPLETE: "neutral"}
ACTION_LABEL = {I.ANALYZE: "Escalate to Analyze", I.RESPOND: "Move to Respond", I.REVIEW: "Submit for Review",
                I.CLOSED_COMPLETE: "Close: Complete", I.CLOSED_CANCELLED: "Close: Cancelled",
                I.CLOSED_INCOMPLETE: "Close: Incomplete"}


def _when(ts) -> str:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)) or ts == "":
        return "—"
    t = pd.Timestamp(ts, unit="s", tz="UTC") if isinstance(ts, (int, float)) else pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert(config.LOCAL_TZ).strftime("%a %b %-d, %Y %-I:%M %p")


def state_pill(state: str) -> html.Span:
    return html.Span(state, className=f"inv-state inv-state--{STATE_TONE.get(state, 'neutral')}")


def _who() -> html.Div:
    u = accounts.current()
    if u["can_edit"]:
        txt = [icon("users"), html.Span(["Signed in as ", html.B(u["name"]), f" ({u['username']}): your changes are "
                                         "recorded under your name."])]
    elif u.get("shared"):
        txt = [icon("lock"), html.Span("You're using the shared administrator account: read only. Changes need a named "
                                       "staff account (`python -m wepa_monitor users add`).")]
    else:
        txt = [icon("lock"), html.Span(f"Signed in as {u['name']}: read only.")]
    return html.Div(txt, className="inv-who")


# --- list ---------------------------------------------------------------------------------------------

def layout(ds: M.Dataset, params: dict):
    st = ds.stations.sort_values(["building", "station_id"])
    return [
        html.Div(className="inv-intro", children=[
            html.P(["Documented root-cause work on recurring or unusual printer problems (network drop-offs and "
                    "hardware or system faults, not paper or supplies): the evidence to take to Wepa. It doesn't "
                    "replace BSU's ITSM ticketing; record that ticket number on the investigation. Every change is "
                    "kept permanently with who made it and why."], className="muted"),
            _who()]),
        html.Div(className="toolbar", children=[
            html.Div([icon("search"), dcc.Input(id="inv-q", type="search", value=params.get("q", ""), debounce=0.3,
                                                placeholder="Search by reference, printer, building or text",
                                                className="search__input")], className="search"),
            segmented("inv-filter", [{"label": t, "value": k} for k, t in FILTERS], params.get("show", "open")),
        ]),
        dcc.Loading(html.Div(id="inv-list"), type="dot", delay_show=400),
        html.Details(className="card inv-new", children=[
            html.Summary([icon("file"), html.Span("Open an investigation by hand")]),
            html.Div(className="inv-form", children=[
                html.Label(["Printer", dcc.Dropdown(id="inv-new-station", options=[
                    {"label": f"{r.description} (#{r.station_id}) · {r.building}", "value": r.station_id}
                    for r in st.itertuples()], placeholder="Choose the printer")]),
                html.Label(["Category", dcc.Dropdown(id="inv-new-cat", value="ERR", clearable=False, options=[
                    {"label": "ERR · hardware or system", "value": "ERR"},
                    {"label": "OUT · unreachable or unresponsive", "value": "OUT"}])]),
                html.Label(["Name", dcc.Input(id="inv-new-title", type="text", maxLength=160,
                                              placeholder="Leave empty to name it automatically")]),
                html.Label(["What's been seen", dcc.Textarea(id="inv-new-desc", placeholder="Facts: what happened, "
                                                             "when, and the outage references involved.")]),
                html.Label(["Outages to link", dcc.Dropdown(id="inv-new-link", multi=True,
                                                            placeholder="Choose a printer first")]),
                html.Button("Open investigation", id="inv-new-go", className="btn btn--primary", n_clicks=0),
                html.Div(id="inv-new-msg", className="inv-msg"),
            ]),
        ]),
    ]


def render_list(ds: M.Dataset, show: str, q: str):
    if show == "metrics":
        return render_metrics(ds)
    t = I.table(ds.data_dir)
    ok, chain = I.verify(ds.data_dir)
    counts = t[~t["archived"].fillna(False).astype(bool)]["state"].value_counts() if len(t) else pd.Series(dtype=int)
    chips = html.Div([html.Div([html.Span(str(int(counts.get(s, 0))), className="stat__value"),
                                html.Span(s, className="stat__label")], className="stat") for s in I.STATES],
                     className="stats inv-stats")
    integrity = html.P([icon("shield" if ok else "alert"), html.Span(("Audit trail verified. " if ok else
                                                                      "AUDIT TRAIL PROBLEM: ") + chain)],
                       className="footnote footnote--icon" + ("" if ok else " inv-bad"))
    if t.empty:
        return [chips, empty("No investigations yet. They open automatically when a printer has "
                             f"{I.RECUR_MIN}+ network or hardware outages within {I.RECUR_WINDOW_D} days, or you can "
                             "open one by hand below.", big=False), integrity]
    arch = t["archived"].fillna(False).astype(bool)
    closed = t["state"].isin(I.CLOSED)
    t = {"open": t[~closed], "closed": t[closed & ~arch], "archived": t[arch]}.get(show, t)
    if q:
        ql = q.strip().lower()
        hay = (t["ref"].fillna("") + " " + t["title"].fillna("") + " " + t["location"].fillna("") + " "
               + t["station_id"].fillna("")).str.lower()
        t = t[hay.str.contains(ql, regex=False)]
    if t.empty:
        return [chips, empty("Nothing matches.", big=False), integrity]
    impacts = {}
    for sid in t["station_id"].dropna().unique():
        try:
            impacts[sid] = I.impact(ds, sid)["level"]
        except Exception:  # noqa: BLE001
            impacts[sid] = "Moderate"
    t = t.astype(object).where(pd.notna(t), None)
    rows = []
    for _, r in t.iterrows():
        rows.append(html.Tr([
            html.Td(dcc.Link(r["ref"], href=f"/investigations/{r['ref']}", className="link mono")),
            html.Td(dcc.Link(r["title"], href=f"/investigations/{r['ref']}", className="link")),
            html.Td(state_pill(r["state"])),
            html.Td(r["location"] or r["station_id"]),
            html.Td(r["impact_override"] or impacts.get(r["station_id"], "Moderate")),
            html.Td(_when(r["first_occurrence"])), html.Td(_when(r["escalated_at"])),
            html.Td(r["assignee"] or "—")]))
    head = html.Thead(html.Tr([html.Th(h) for h in ("Reference", "Name", "State", "Kiosk location", "Impact",
                                                     "First occurrence", "Escalated", "Assigned to")]))
    return [chips, html.Div(html.Table([head, html.Tbody(rows)], className="table"), className="table-wrap"), integrity]


def link_options(ds: M.Dataset, sid: str | None) -> list[dict]:
    if not sid or "ref" not in ds.sev_inc:
        return []
    inc = ds.sev_inc[(ds.sev_inc["station_id"] == sid) & (ds.sev_inc["ref"].astype(str) != "")]
    inc = inc.sort_values("start", ascending=False).head(60)
    return [{"label": f"{r.ref} · {_when(r.start)}", "value": r.ref} for r in inc.itertuples()]


def create_manual(ds: M.Dataset, sid, cat, title, desc, linked) -> str:
    user = accounts.current()
    if not sid:
        raise I.InvestigationError("Choose the printer.")
    name = ds.stations.set_index("station_id")["label"].get(sid, sid)
    if not (title or "").strip():
        title = f"{'Network or connection drop-offs' if cat == 'OUT' else 'Hardware or system faults'}: {name}"
    first = None
    if linked:
        starts = [refs.lookup(ds.data_dir, x) for x in linked]
        starts = [s["start"] for s in starts if s and s.get("start")]
        first = min(starts) if starts else None
    return I.create(ds.data_dir, user, sid, title, desc or "", cat or "ERR", origin="manual", linked=linked or [],
                    first_occurrence=first, location=I._location(ds, sid))


# --- one investigation --------------------------------------------------------------------------------

def detail_layout(ref: str):
    return [dcc.Store(id="inv-ref", data=ref), html.Div(id="inv-msg", className="inv-msg"),
            dcc.Loading(html.Div(id="inv-body"), type="dot", delay_show=400)]


def _field(fid: str, label: str, value, locked: bool, kind: str = "text", options=None, hint: str = ""):
    common = {"id": {"type": "inv-field", "name": fid}, "disabled": locked}
    if kind == "area":
        ctl = dcc.Textarea(value=value or "", **common)
    elif kind == "select":
        ctl = dcc.Dropdown(options=options, value=value, clearable=True, **common)
    else:
        ctl = dcc.Input(type="text", value=value or "", maxLength=200, **common)
    return html.Label([html.Span(label, className="inv-label"), ctl,
                       html.Span(hint, className="inv-hint") if hint else None], className="inv-field")


def render_detail(ds: M.Dataset, ref: str):
    r = I.get(ds.data_dir, ref)
    if r is None:
        return [empty(f"No investigation {ref}."), _hidden_controls()]
    user = accounts.current()
    state = r["state"]
    locked = (state not in I.EDITABLE_IN) or r.get("archived") or not user["can_edit"]
    imp = I.impact(ds, r["station_id"]) if r.get("station_id") else {"level": "Moderate", "factors": [], "note": ""}
    level = r.get("impact_override") or imp["level"]
    staff = [u for u in accounts.users() if u["role"] == "staff" and not u.get("disabled")]
    assignee_opts = [{"label": f"{u['name']} ({u['username']})", "value": u["username"]} for u in staff]
    if r.get("assignee") and r["assignee"] not in {o["value"] for o in assignee_opts}:
        assignee_opts.append({"label": r["assignee"], "value": r["assignee"]})
    if not assignee_opts and not accounts.admin_name():
        assignee_opts = [{"label": "Local user", "value": "local"}]

    head = html.Div(className="inv-head", children=[
        html.Div([html.Div(ref, className="inv-ref mono"), html.H2(r.get("title") or ref),
                  html.Div([state_pill(state), html.Span(f"Impact: {level}", className=f"inv-impact inv-impact--{level.lower()}"),
                            html.Span("Archived", className="inv-state inv-state--neutral") if r.get("archived") else None],
                           className="inv-pills")]),
        html.A([icon("download"), html.Span("Evidence packet (PDF)")], href=f"/investigations/{ref}/evidence.pdf",
               className="btn", target="_blank"),
    ])
    facts = html.Dl(className="inv-facts", children=sum([[html.Dt(k), html.Dd(v)] for k, v in [
        ("Kiosk location", r.get("location") or r.get("station_id")),
        ("Category", f"{r.get('category')} · {refs.KINDS.get(r.get('category'), '')}"),
        ("First occurrence", _when(r.get("first_occurrence"))),
        ("Escalated", _when(r.get("escalated_at"))),
        ("Opened", f"{_when(r.get('created_at'))} · " + ("automatically (" + (r.get("detector") or "") + ")"
                                                        if r.get("origin") == "auto" else f"by {r.get('created_by')}")),
        ("Last change", _when(r.get("updated_at"))),
    ]], []))

    # Workflow: where it is, and what can happen next.
    steps = html.Ol([html.Li(s, className="inv-step" + (" is-current" if s == state else "")
                             + (" is-done" if I.STATES.index(s) < I.STATES.index(state) and state not in I.CLOSED
                                or (state in I.CLOSED and s in (I.NEW, I.ANALYZE, I.RESPOND, I.REVIEW)) else ""))
                     for s in (I.NEW, I.ANALYZE, I.RESPOND, I.REVIEW)] + [
        html.Li(state if state in I.CLOSED else "Closed", className="inv-step" + (" is-current" if state in I.CLOSED else ""))],
        className="inv-steps")
    nexts = [] if r.get("archived") or not user["can_edit"] else I.TRANSITIONS.get(state, [])
    actions = html.Div(className="inv-actions", children=[
        dcc.Textarea(id="inv-decision-note", placeholder="Reason / note for this decision (required to close, "
                     "cancel or send back)", className="inv-note-input", style=None if nexts else {"display": "none"}),
        html.Div([html.Button(f"Send back to {to}" if I.is_backward(state, to) else ACTION_LABEL[to],
                              id={"type": "inv-go", "to": to}, n_clicks=0,
                              className="btn " + ("btn--primary" if to in (I.ANALYZE, I.RESPOND, I.REVIEW, I.CLOSED_COMPLETE)
                                                  else "")) for to in nexts], className="inv-buttons"),
        html.P(_requirements(state), className="inv-hint") if nexts else None,
    ])
    workflow = chart_card("Where it stands", "", body=html.Div([steps, actions]), wide=True, icon_name=("target", "crimson"))

    fields = html.Div(className="inv-grid", children=[
        _field("title", "Name", r.get("title"), locked),
        _field("assignee", "Assigned to", r.get("assignee"), locked, "select", assignee_opts,
               "Needed to escalate to Analyze."),
        _field("description", "Description", r.get("description"), locked, "area"),
        _field("root_cause", "Root cause", r.get("root_cause"), locked, "area", hint="Needed before Respond."),
        _field("root_cause_status", "Root cause is", r.get("root_cause_status"), locked, "select",
               [{"label": s, "value": s} for s in I.ROOT_CAUSE_STATUS]),
        _field("action_taken", "Action taken", r.get("action_taken"), locked, "area",
               hint="Needed before Review, e.g. 'Opened Wepa case 12345'."),
        _field("itsm_ref", "ITSM ticket #", r.get("itsm_ref"), locked, hint="BSU's ticket, if one was opened."),
        _field("wepa_case", "Wepa case #", r.get("wepa_case"), locked),
    ])
    save = html.Button("Save changes", id="inv-save", className="btn btn--primary", n_clicks=0,
                       style={"display": "none"} if locked else None)
    lock_note = (html.P([icon("lock"), html.Span(
        "Fields are locked in " + state + ("; add a note below." if state in I.CLOSED else "; the reviewer can "
                                           "send it back to Respond to change them.") if state not in I.EDITABLE_IN
        else "Read only for your account.")], className="footnote footnote--icon") if locked else None)
    details = chart_card("Record", "", body=html.Div([fields, save, lock_note]), wide=True, icon_name=("file", "blue"))

    factors = data_table(pd.DataFrame(imp.get("factors", []), columns=["factor", "value", "score"]),
                         [("factor", "Factor", None), ("value", "What the data shows", None),
                          ("score", "Score (1-3)", None)])
    override = html.Div(className="inv-grid", children=[
        _field("impact_override", "Set impact by hand", r.get("impact_override"), locked, "select",
               [{"label": s, "value": s} for s in I.IMPACTS], "Leave empty to use the calculated level."),
        _field("impact_reason", "Why", r.get("impact_reason"), locked, hint="Required when setting it by hand."),
    ])
    impact_card = chart_card("Impact", f"Calculated: {imp['level']}. {imp.get('note', '')}",
                             body=html.Div([factors, override]), icon_name=("gauge", "gold"))

    linked = r.get("linked") or []
    inc = ds.sev_inc[ds.sev_inc["ref"].isin(linked)] if "ref" in ds.sev_inc and linked else ds.sev_inc.iloc[0:0]
    if len(inc):
        from ... import narrative as N
        inc = inc.sort_values("start", ascending=False).assign(
            when=lambda d: d["start"].map(_when),
            cause=[", ".join(rules.issue_status(c) + (f" ({x})" if x else "") for c, x in
                             N.outage_causes(ds, s, t)[:2]) or "No cause reported" for s, t in zip(inc["station_id"], inc["start"])],
            lasted=lambda d: [("ongoing" if st_ == "open" else I.fmt_dur(x)) for st_, x in zip(d["status"], d["duration_s"])])
    linked_card = chart_card("Linked outages", f"{len(linked)} reference{'s' if len(linked) != 1 else ''}.",
                             body=data_table(inc, [("ref", "Reference", None), ("when", "Started", None),
                                                   ("cause", "Cause", None), ("lasted", "Lasted", None)],
                                             empty="No outages linked."), icon_name=("pulse", "crimson"))

    notes = html.Ul([html.Li([html.Div([html.B(e.get("user_name")), html.Span(" · " + _when(e["ts"]), className="muted")]),
                              html.P(e["note"])], className="inv-note") for e in reversed(r["notes"])],
                    className="inv-notes") if r["notes"] else html.P("No notes yet.", className="muted")
    can_note = user["can_edit"] and not r.get("archived")
    note_card = chart_card("Notes", "Updates, Wepa's replies, anything worth keeping. Notes can't be edited or removed.",
                           body=html.Div([
                               html.Div([dcc.Textarea(id="inv-note-text", placeholder="Add a note…", className="inv-note-input"),
                                         html.Button("Add note", id="inv-note-go", className="btn", n_clicks=0)],
                                        className="inv-note-add", style=None if can_note else {"display": "none"}),
                               notes]),
                           wide=True, icon_name=("history", "gray"))

    ok, chain = I.verify(ds.data_dir)
    trail = pd.DataFrame([{"when": _when(e["ts"]), "who": f"{e.get('user_name')} ({e['user']})", "what": _describe(e),
                           "note": e.get("note", ""), "hash": e["hash"][:12]} for e in reversed(r["events"])])
    audit = chart_card("Audit trail", ("Verified: " if ok else "PROBLEM: ") + chain,
                       body=data_table(trail, [("when", "When", None), ("who", "Who", None), ("what", "What changed", None),
                                               ("note", "Reason / note", None), ("hash", "Fingerprint", None)],
                                       max_rows=500), wide=True, icon_name=("shield", "green" if ok else "crimson"))
    return [head, _who(), facts, html.Div(className="grid", children=[workflow, lifecycle_card(ds, r), details,
                                                                      impact_card, linked_card, note_card, audit])]


def _tile(label: str, value: str, sub: str = "") -> html.Div:
    return html.Div([html.Div(label, className="inv-tile__label"), html.Div(value, className="inv-tile__value"),
                     html.Div(sub, className="inv-tile__sub") if sub else None], className="inv-tile")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def lifecycle_card(ds: M.Dataset, r: dict):
    """How long it has spent where, how often it went back, who held it, and the outages behind it."""
    m = I.metrics(ds, r)
    f = I.fmt_dur
    ps = m["per_state"]
    tiles = html.Div(className="inv-tiles", children=[
        _tile("Open for" if not m["is_closed"] else "Took", f(m["open_seconds"]),
              "since it was opened" if not m["is_closed"] else "from opening to closing"),
        _tile("To assign", f(m["time_to_assign"])), _tile("To escalate", f(m["time_to_escalate"])),
        _tile("Sent back", str(m["sent_back"]), "times"),
        _tile("Reassigned", str(m["reassignments"]), f"{m['assignees']} assignee{'s' if m['assignees'] != 1 else ''}"),
        _tile("People involved", str(len(m["people"])), ", ".join(m["people"])[:60]),
        _tile("Linked outages", str(m["linked_outages"]), f"{f(m['outage_seconds'])} out of service in total"),
        _tile("First outage to " + ("close" if m["is_closed"] else "now"), f(m["end_to_end"]),
              f"opened {f(m['detection_lag'])} after the first outage" if m["detection_lag"] is not None else ""),
        _tile("Outages", f"{m['before_opened']} · {m['during']} · " + ("—" if m["after_closed"] is None else str(m["after_closed"])),
              "before opening · during · after closing"),
    ] + ([_tile("Did the fix hold?", "Yes" if m["recurred_after_close"] == 0 else "No",
                f"{m['recurred_after_close']} new {r.get('category')} outage(s) on this printer since closing")]
         if m["recurred_after_close"] is not None else []))
    totals = pd.DataFrame([{"state": st, "visits": d["visits"], "total": f(d["seconds"]) if d["visits"] else "—",
                            "desk": f(d["staffed_seconds"]) if d["visits"] else "—",
                            "back": d["sent_back_into"], "first": _when(d["first_entered"])} for st, d in ps.items()])
    v = m["visits"]
    stays = v.assign(which=v["instance"].map(_ordinal) + " time", entered=v["entered_at"].map(_when),
                     left=[("—" if st in I.CLOSED else "now (current)" if c else _when(x))
                           for st, c, x in zip(v["state"], v["current"], v["left_at"])],
                     dur=[("—" if pd.isna(x) else f(x)) for x in v["seconds"]],
                     desk=[("—" if pd.isna(x) else f(x)) for x in v["staffed_seconds"]]) if len(v) else v
    return chart_card(
        "Lifecycle metrics", "Every figure comes from the recorded changes below: when it entered and left each state, "
        "and who moved it.", wide=True, icon_name=("clock", "blue"),
        body=html.Div([
            tiles,
            html.H4("Time in each state", className="inv-subhead"),
            data_table(totals, [("state", "State", None), ("visits", "Times in it", None), ("back", "Sent back into it", None),
                                ("first", "First entered", None), ("total", "Total time", None),
                                ("desk", "Of which desk hours", None)]),
            html.H4("Every stay, in order", className="inv-subhead"),
            data_table(stays, [("state", "State", None), ("which", "Instance", None), ("how", "How it got there", None),
                               ("entered", "Entered", None), ("entered_by", "By", None), ("left", "Left", None),
                               ("next", "Then", None), ("dur", "Time", None), ("desk", "Desk hours", None),
                               ("reason", "Reason given", None)], max_rows=200),
        ]),
        explain=["Desk hours count only the time the responsible support desk (ResNet or the IT Service Center) was "
                 "staffed, so a weekend in Respond doesn't look like neglect.",
                 "'Did the fix hold?' counts new outages of the same category on this printer after closing, linked or "
                 "not. Closed states have no duration."])


def render_metrics(ds: M.Dataset):
    """Across every investigation: where time goes, how often work goes backwards, and outcomes."""
    t = I.metrics_table(ds)
    if t.empty:
        return [empty("No investigations yet, so nothing to measure.", big=False)]
    f = I.fmt_dur
    closed = t[t["state"].isin(I.CLOSED)]
    open_ = t[~t["state"].isin(I.CLOSED)]
    complete = t[t["state"] == I.CLOSED_COMPLETE]
    held = complete["after_closed"].dropna()
    tiles = html.Div(className="inv-tiles", children=[
        _tile("Open now", str(len(open_)), f"{len(closed)} closed"),
        _tile("Median time to escalate", f(t["to_escalate_s"].dropna().median()) if t["to_escalate_s"].notna().any() else "—"),
        _tile("Median time to close", f(closed["to_close_s"].median()) if len(closed) else "—"),
        _tile("Sent back at least once", f"{(t['sent_back'] > 0).mean():.0%}", f"{int(t['sent_back'].sum())} times in all"),
        _tile("Reassigned at least once", f"{(t['reassignments'] > 0).mean():.0%}", f"{int(t['reassignments'].sum())} reassignments"),
        _tile("Linked outage time", f(t["outage_s"].sum()), f"{int(t['linked_outages'].sum())} outages"),
        _tile("Fixes that held", f"{(held == 0).mean():.0%}" if len(held) else "—",
              f"of {len(held)} closed complete: no new outages of that kind since" if len(held) else "none closed complete yet"),
    ])
    summary = I.state_summary(t)
    summary = summary.assign(med=summary["median_s"].map(f), mx=summary["max_s"].map(f), desk=summary["median_staffed_s"].map(f))
    per = t.assign(open_h=t["open_s"].map(f), esc=t["to_escalate_s"].map(f), new=t["new_s"].map(f), an=t["analyze_s"].map(f),
                   rs=t["respond_s"].map(f), rv=t["review_s"].map(f), out=t["outage_s"].map(f),
                   visits=t["analyze_visits"].astype(str) + " / " + t["respond_visits"].astype(str) + " / " + t["review_visits"].astype(str),
                   held=t["after_closed"].map(lambda x: "—" if pd.isna(x) else ("Yes" if x == 0 else f"No ({int(x)})")))
    rows = [html.Tr([html.Td(dcc.Link(x.ref, href=f"/investigations/{x.ref}", className="link mono")), html.Td(state_pill(x.state)),
                     html.Td(x.open_h), html.Td(x.esc), html.Td(x.new), html.Td(x.an), html.Td(x.rs), html.Td(x.rv),
                     html.Td(x.visits), html.Td(str(x.sent_back)), html.Td(str(x.reassignments)), html.Td(str(x.people)),
                     html.Td(f"{x.linked_outages} · {x.out}"), html.Td(x.held)]) for x in per.itertuples()]
    head = ["Reference", "State", "Open / took", "To escalate", "In New", "In Analyze", "In Respond", "In Review",
            "Times in Analyze / Respond / Review", "Sent back", "Reassigned", "People", "Linked outages · time", "Fix held"]
    return [tiles,
            chart_card("Where the time goes", "For each state: how many investigations entered it, how many stays in all "
                       "(repeat stays are rework), and the median and longest total time spent there.", wide=True,
                       icon_name=("clock", "blue"),
                       body=data_table(summary, [("state", "State", None), ("investigations", "Investigations", None),
                                                 ("visits", "Stays", None), ("repeat_visits", "Repeat stays", None),
                                                 ("med", "Median time", None), ("desk", "Median desk hours", None),
                                                 ("mx", "Longest", None)])),
            chart_card("Every investigation", "Download it with Export → Investigations.", wide=True, icon_name=("file", "gray"),
                       body=html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in head])), html.Tbody(rows)],
                                                className="table"), className="table-wrap"))]


def _hidden_controls():
    hide = {"display": "none"}
    return html.Div([html.Button(id="inv-save", n_clicks=0), dcc.Textarea(id="inv-decision-note"),
                     dcc.Textarea(id="inv-note-text"), html.Button(id="inv-note-go", n_clicks=0)], style=hide)


def _requirements(state: str) -> str:
    return {I.NEW: "Escalating needs an assignee. Cancelling needs a reason.",
            I.ANALYZE: "Respond needs a root cause marked Suspected or Confirmed.",
            I.RESPOND: "Review needs the action taken. A second person closes it after review.",
            I.REVIEW: "Closing needs a reason, from someone other than the person who submitted it for review."}.get(state, "")


def _describe(e: dict) -> str:
    if e["action"] == "create":
        return "Opened"
    if e["action"] == "note":
        return "Note added"
    if e["action"] == "archive":
        return "Archived"
    parts = []
    for k, (old, new) in e.get("changes", {}).items():
        if k in ("created_at", "created_by"):
            continue
        lab = I.FIELD_LABEL.get(k, k.replace("_", " ").capitalize())
        if k == "state":
            parts.append(f"{old} → {new}")
        elif isinstance(new, list):
            parts.append(f"{lab}: {len(old or [])} → {len(new)}")
        else:
            o = (str(old)[:40] + "…") if old and len(str(old)) > 40 else (old or "empty")
            n = (str(new)[:40] + "…") if new and len(str(new)) > 40 else (new or "empty")
            parts.append(f"{lab}: {o} → {n}")
    return "; ".join(parts) or e["action"]


# --- evidence packet ----------------------------------------------------------------------------------

def evidence_pdf(ds: M.Dataset, ref: str) -> bytes | None:
    from fpdf import FPDF

    from ...export import _latin1
    r = I.get(ds.data_dir, ref)
    if r is None:
        return None
    ok, chain = I.verify(ds.data_dir)
    imp = I.impact(ds, r["station_id"]) if r.get("station_id") else {"level": "Moderate", "factors": [], "note": ""}

    class Doc(FPDF):
        def header(self):
            self.set_fill_color(137, 25, 31)
            self.rect(0, 0, self.w, 14, "F")
            self.set_xy(10, 4)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 11)
            self.cell(0, 6, _latin1(f"ResNet Print Ops - Evidence packet {ref}"))
            self.set_text_color(0, 0, 0)
            self.ln(14)

        def footer(self):
            self.set_y(-10)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(110, 110, 110)
            self.cell(0, 5, _latin1(f"{ref} - generated {datetime.now():%Y-%m-%d %H:%M} - page {self.page_no()}"), align="C")

    pdf = Doc(format="Letter")
    pdf.set_auto_page_break(True, margin=14)
    pdf.add_page()

    def h(text):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 7, _latin1(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9.5)

    def line(text):
        pdf.multi_cell(0, 5, _latin1(text), new_x="LMARGIN", new_y="NEXT")

    if ds.is_demo:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(160, 90, 0)
        pdf.cell(0, 6, "Generated from DEMO data: synthetic, not real printers.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 15)
    pdf.multi_cell(0, 7, _latin1(r.get("title") or ref), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    for k, v in [("Reference", ref), ("State", r["state"] + (" (archived)" if r.get("archived") else "")),
                 ("Kiosk location", r.get("location") or r.get("station_id")),
                 ("Category", f"{r.get('category')} - {refs.KINDS.get(r.get('category'), '')}"),
                 ("First occurrence", _when(r.get("first_occurrence"))), ("Escalated", _when(r.get("escalated_at"))),
                 ("Impact", (r.get("impact_override") + " (set by hand: " + (r.get("impact_reason") or "") + ")")
                  if r.get("impact_override") else f"{imp['level']} (calculated; {imp.get('note', '')})"),
                 ("Assigned to", r.get("assignee") or "-"), ("ITSM ticket #", r.get("itsm_ref") or "-"),
                 ("Wepa case #", r.get("wepa_case") or "-")]:
        line(f"{k}: {v}")
    for title, key in [("Description", "description"), ("Root cause", "root_cause"), ("Action taken", "action_taken")]:
        h(title + (f" ({r.get('root_cause_status')})" if key == "root_cause" and r.get("root_cause_status") else ""))
        line(r.get(key) or "-")
    h("Impact factors")
    for f in imp.get("factors", []):
        line(f"{f[0]}: {f[1]} (score {f[2]} of 3)")
    m = I.metrics(ds, r)
    h("Lifecycle")
    line(f"{'Took' if m['is_closed'] else 'Open for'} {I.fmt_dur(m['open_seconds'])}; to assign {I.fmt_dur(m['time_to_assign'])}; "
         f"to escalate {I.fmt_dur(m['time_to_escalate'])}; sent back {m['sent_back']} time(s); reassigned "
         f"{m['reassignments']} time(s); people involved: {', '.join(m['people']) or '-'}.")
    for x in m["visits"].itertuples():
        left = "current" if x.current else _when(x.left_at)
        dur = "" if pd.isna(x.seconds) else f" - {I.fmt_dur(x.seconds)} ({I.fmt_dur(x.staffed_seconds)} desk hours)"
        line(f"{x.state} ({_ordinal(x.instance)} time, {x.how.lower()}): {_when(x.entered_at)} to {left}{dur}"
             + (f" - reason: {x.reason}" if x.reason else ""))
    if m["linked_outages"]:
        line(f"Linked outages: {m['linked_outages']} ({I.fmt_dur(m['outage_seconds'])} out of service); {m['before_opened']} "
             f"before opening, {m['during']} during" + (f", {m['after_closed']} after closing" if m["after_closed"] is not None else "")
             + (f". New outages of this kind since closing: {m['recurred_after_close']}." if m["recurred_after_close"] is not None else "."))
    h("Linked outages (from the monitor's minute-by-minute record of Wepa's status page)")
    linked = r.get("linked") or []
    inc = ds.sev_inc[ds.sev_inc["ref"].isin(linked)].sort_values("start") if "ref" in ds.sev_inc and linked else None
    if inc is None or inc.empty:
        line("None linked.")
    else:
        from ... import narrative as N
        for x in inc.itertuples():
            causes = N.outage_causes(ds, x.station_id, x.start)
            cause = ", ".join(rules.issue_status(c) + (f" ({d})" if d else "") for c, d in causes[:2]) or "No cause reported"
            end = "ongoing" if x.status == "open" else _when(x.end)
            line(f"{x.ref}: {_when(x.start)} to {end} - {cause}")
    h("Notes")
    for e in r["notes"] or []:
        line(f"{_when(e['ts'])} - {e.get('user_name')} ({e['user']}): {e['note']}")
    if not r["notes"]:
        line("None.")
    h("Audit trail")
    for e in r["events"]:
        line(f"{_when(e['ts'])} - {e.get('user_name')} ({e['user']}) - {_describe(e)}"
             + (f" - {e['note']}" if e.get("note") else "") + f" [#{e['hash'][:16]}]")
    h("Integrity")
    line(("Verified: " if ok else "WARNING: ") + chain + " Each change carries the SHA-256 fingerprint of the one "
         "before it, so altering or removing any recorded change is detectable.")
    return bytes(pdf.output())
