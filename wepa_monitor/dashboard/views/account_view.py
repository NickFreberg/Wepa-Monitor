"""My account (picture, phone, password) and the staff Directory, plus the header's user menu."""
from __future__ import annotations

import pandas as pd
from dash import dcc, html

from ... import accounts, auth
from ..components import icon


def _initials(name: str) -> str:
    parts = [p for p in (name or "?").replace("(", " ").split() if p[:1].isalpha()]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")) if parts else "?").upper()


def avatar(u: dict, size: int = 32) -> html.Span:
    style = {"width": f"{size}px", "height": f"{size}px", "fontSize": f"{max(size // 2.6, 10):.0f}px"}
    if u.get("photo") and u.get("username"):
        return html.Img(src=f"/_avatar/{u['username']}.png?v={u['photo']}", alt="", className="avatar", style=style)
    return html.Span(_initials(u.get("name", "")), className="avatar avatar--initials", style=style,
                     **{"aria-hidden": "true"})


def user_menu() -> html.Details:
    me = accounts.current()
    if me.get("username") and me["role"] != "admin":
        me = {**me, **(accounts.get(me["username"]) or {})}
    signed_in = auth.enabled()
    return html.Details(id="user-menu", className="popover", children=[
        html.Summary(avatar(me, 30), className="topbar__icon-btn user-btn", title=me.get("name", "Account")),
        html.Div(className="popover__panel user-panel", children=[
            html.Div([avatar(me, 44), html.Div([html.B(me.get("name")),
                                                html.Div(me.get("email") or me.get("username") or "", className="muted"),
                                                html.Div(me["role"].capitalize(), className="muted")])],
                     className="user-panel__who"),
            dcc.Link([icon("users"), html.Span("My account")], href="/account", className="user-panel__link")
            if me["role"] != "admin" else None,
            dcc.Link([icon("building"), html.Span("Directory")], href="/directory", className="user-panel__link"),
            dcc.Link([icon("send"), html.Span("Suggest a feature")], href="/feedback", className="user-panel__link"),
            html.A([icon("lock"), html.Span("Change password")], href="/account/password", className="user-panel__link")
            if signed_in and me["role"] != "admin" else None,
            html.A([icon("x"), html.Span("Sign out")], href="/logout", className="user-panel__link")
            if signed_in else html.P("Sign-in is off on this copy (no WEPA_BASIC_AUTH).", className="muted"),
        ]),
    ])


# --- My account -----------------------------------------------------------------------------------------

def layout(params: dict):
    return [html.Div(id="acct-msg", className="inv-msg",
                     children=html.Span("Password changed. Other sessions have been signed out.", className="inv-ok")
                     if params.get("changed") else None),
            html.Div(id="acct-body")]


def render():
    me = accounts.current()
    if me["role"] == "admin":
        return html.Div(className="card acct", children=[
            html.H3("Administrator"),
            html.P("The administrator account isn't a person in the directory: it manages accounts from the command "
                   "line (python -m wepa_monitor users …) and can view everything. Its password is set in the app's "
                   "configuration.", className="muted")])
    if not auth.enabled():
        return html.P("Sign-in is off on this copy, so there are no personal accounts.", className="empty")
    u = accounts.get(me["username"]) or {}
    admin_set = [("Name", u.get("name")), ("Email", u.get("email") or "Not set yet"), ("Username", u.get("username")),
                 ("Role", (u.get("role") or "").capitalize()),
                 ("Resident student", "Yes" if u.get("resident") else "No"),
                 ("Home residence hall", u.get("hall") or "—")]
    return html.Div(className="acct-grid", children=[
        html.Section(className="card acct", children=[
            html.H3("Profile picture"),
            html.Div(className="acct-photo", children=[
                avatar(u, 112),
                html.Div([
                    dcc.Upload(id="acct-photo-upload", accept="image/png,image/jpeg,image/webp,image/gif",
                               max_size=accounts.PHOTO_MAX_BYTES, multiple=False, className="acct-upload",
                               children=html.Div([icon("download"), html.Span("Upload a picture")])),
                    html.Button("Remove picture", id="acct-photo-remove", className="btn btn--sm", n_clicks=0,
                                style=None if u.get("photo") else {"display": "none"}),
                    html.P("PNG, JPEG, WebP or GIF up to 5 MB. It's cropped square, resized, and saved without any "
                           "location or camera data.", className="inv-hint")]),
            ]),
        ]),
        html.Section(className="card acct", children=[
            html.H3("Contact phone"),
            html.P("Where staff can reach you about printer problems. Optional.", className="muted"),
            html.Div([dcc.Input(id="acct-phone", type="tel", value=u.get("phone") or "", placeholder="(508) 531-1000",
                                autoComplete="tel", className="acct-input"),
                      html.Button("Save", id="acct-phone-save", className="btn btn--primary", n_clicks=0)],
                     className="acct-row"),
        ]),
        html.Section(className="card acct", children=[
            html.H3("Password"),
            html.P("Changing it signs you out on every other device.", className="muted"),
            html.A("Change password", href="/account/password", className="btn"),
            html.P(f"Last signed in: {_when(u.get('last_login'))}", className="inv-hint"),
        ]),
        html.Section(className="card acct", children=[
            html.H3("Set by your administrator"),
            html.Dl([x for k, v in admin_set for x in (html.Dt(k), html.Dd(v))], className="inv-facts"),
            html.P("To change any of these, ask the ResNet administrator.", className="inv-hint"),
        ]),
    ])


def _when(ts) -> str:
    if not ts:
        return "—"
    from ... import config
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(config.LOCAL_TZ).strftime("%a %b %-d, %Y %-I:%M %p")


# --- Directory ------------------------------------------------------------------------------------------

def directory():
    me = accounts.current()
    is_admin = me["role"] == "admin" or not auth.enabled()
    rows = accounts.users()
    if not rows:
        return html.P("No staff accounts yet. The administrator adds them with python -m wepa_monitor users add.",
                      className="empty")
    head = ["", "Name", "Email", "Phone", "Role", "Status"] + (["Resident", "Last sign-in"] if is_admin else [])
    body = []
    for u in rows:
        if not u.get("active", True) and not is_admin:
            continue
        cells = [html.Td(avatar(u, 30)), html.Td([html.B(u["name"]), html.Div(u["username"], className="muted mono")]),
                 html.Td(html.A(u["email"], href=f"mailto:{u['email']}", className="link") if u.get("email") else "—"),
                 html.Td(html.A(u["phone"], href=f"tel:{u['phone']}", className="link") if u.get("phone") else "—"),
                 html.Td(u["role"].capitalize()),
                 html.Td(html.Span("Active" if u.get("active", True) else "Inactive",
                                   className="inv-state inv-state--" + ("good" if u.get("active", True) else "neutral")))]
        if is_admin:
            cells += [html.Td(u["hall"] if u.get("resident") else "No"), html.Td(_when(u.get("last_login")))]
        body.append(html.Tr(cells))
    note = ("Home residence halls and sign-in times are shown to the administrator only."
            if not is_admin else "You're seeing everything, including inactive accounts and residence halls.")
    return [html.P(["Everyone with an account. Names, emails and roles are managed centrally by the administrator; "
                    "people set their own picture and phone. ", note], className="muted"),
            html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in head])), html.Tbody(body)], className="table"),
                     className="table-wrap")]
