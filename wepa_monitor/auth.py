"""Sign-in: a proper login page and sessions, in place of the browser's Basic-auth pop-up.

* Sessions are signed cookies (Flask) carrying only: username, the account's session epoch, a CSRF
  token, when it was issued and when it was last used. The cookie is HttpOnly, SameSite=Lax, and Secure
  whenever the site is reached over HTTPS (Azure's ingress is), so scripts can't read it and other sites
  can't use it.
* A session ends after IDLE_S without activity or ABSOLUTE_S after signing in, whichever comes first,
  and at once when the person signs out, changes their password, is reset, or is deactivated.
* The signing key comes from WEPA_SECRET_KEY, or is generated once and kept (owner-only) in the security
  folder, so sessions survive restarts and deploys.
* Forms carry a CSRF token; POSTs whose Origin isn't this site are refused. Redirects after sign-in only
  go to paths on this site.
* Wrong passwords: one generic message, per-account lockout (accounts.py) and per-network lockout
  (security.py). Every sign-in, failure, sign-out and password change goes to the audit log.

The administrator API (/_admin, used by the CLI) keeps HTTP Basic over HTTPS and is administrator-only.
"""
from __future__ import annotations

import html
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlparse

IDLE_S = 2 * 3600
ABSOLUTE_S = 12 * 3600
OPEN_PATHS = ("/login", "/logout", "/assets/boyden-mark.svg", "/assets/pattern.svg", "/assets/favicon.ico",
              "/favicon.ico", "/_admin/", "/_peer/")


def enabled() -> bool:
    return bool(os.environ.get("WEPA_BASIC_AUTH"))


def _secret(store: Path | None) -> str:
    key = os.environ.get("WEPA_SECRET_KEY")
    if key:
        return key
    if store is None:
        return secrets.token_hex(32)
    p = Path(store) / "secret.key"
    try:
        return p.read_text().strip()
    except OSError:
        store.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        p.write_text(key)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return key


def _safe_next(target: str | None) -> str:
    t = target or "/"
    u = urlparse(t)
    if u.scheme or u.netloc or not t.startswith("/") or t.startswith("//"):
        return "/"
    return t


PAGE_CSS = """
:root{--bg:#f4f1ec;--card:#fbfaf7;--ink:#1c1717;--muted:#5c5753;--m1:#89191f;--m2:#c99a2e;--m3:#4f7396;--pa:rgba(137,25,31,.065);--pb:rgba(168,116,5,.06);--line:#e2ded6;--brand:#89191f;--bad:#b42323;--ok:#0b7a32}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1c1c1a;--ink:#f3f2ee;--muted:#a8a59e;--m1:#f2ece6;--m2:#e0636b;--m3:#c99a2e;--pa:rgba(255,255,255,.055);--pb:rgba(224,99,107,.07);--line:#2c2c2a;--bad:#ff8a8a;--ok:#3ccf6d}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
.card{width:min(420px,100%);background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px 26px;
box-shadow:0 8px 30px rgba(0,0,0,.06)}.brand{display:flex;align-items:center;gap:12px;margin-bottom:18px}
.mark{position:relative;width:42px;height:48px;flex:none}.mark i{position:absolute;inset:0;background:var(--m1);-webkit-mask:url(/assets/boyden-mark.svg) center/contain no-repeat;mask:url(/assets/boyden-mark.svg) center/contain no-repeat}.mark i:nth-child(1){background:var(--m3);transform:translate(-1.5px,1px);opacity:.8}.mark i:nth-child(2){background:var(--m2);transform:translate(1.5px,-1px);opacity:.9}body::before,body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:-1;-webkit-mask:url(/assets/pattern.svg) 0 0/720px repeat;mask:url(/assets/pattern.svg) 0 0/720px repeat}body::before{background:var(--pa)}body::after{background:var(--pb);transform:translate(2px,2px)}@media (prefers-contrast:more),(forced-colors:active){body::before,body::after{display:none}}.brand b{display:block;font-size:17px}.brand span{color:var(--muted);font-size:13px}
h1{font-size:20px;margin:0 0 6px}p{margin:0 0 14px;color:var(--muted);font-size:14px}label{display:block;font-weight:600;
font-size:14px;margin:12px 0 4px}input{width:100%;height:44px;border-radius:10px;border:1px solid var(--line);padding:0 12px;
font:inherit;background:var(--card);color:var(--ink)}input:focus{outline:2px solid var(--brand);border-color:transparent}
button{width:100%;height:46px;margin-top:18px;border:0;border-radius:10px;background:var(--brand);color:#fff;font:inherit;
font-weight:700;cursor:pointer}.err{color:var(--bad);font-weight:600;font-size:14px;margin:10px 0 0}
.ok{color:var(--ok);font-weight:600;font-size:14px;margin:10px 0 0}.hint{font-size:12.5px;color:var(--muted);margin-top:6px}
.foot{font-size:12px;color:var(--muted);margin-top:16px}a{color:var(--brand)}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'>"
            f"<title>{html.escape(title)} · BSU Student Printing Ops</title><style>{PAGE_CSS}</style></head><body>"
            f"<main class='card'><div class='brand'><span class='mark' role='img' aria-label='Boyden Hall, drawn as a wireframe'><i></i><i></i><i></i></span><div><b>BSU Student Printing Ops</b>"
            f"<span>Bridgewater State University</span></div></div>{body}</main></body></html>")


def install(server, store: Path | None) -> None:
    from flask import Response, g, jsonify, redirect, request, session
    from flask.sessions import SecureCookieSessionInterface
    from werkzeug.middleware.proxy_fix import ProxyFix

    from . import accounts, security

    server.secret_key = _secret(store)
    server.config.update(SESSION_COOKIE_NAME="wepa_session", SESSION_COOKIE_HTTPONLY=True,
                         SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=ABSOLUTE_S)
    server.wsgi_app = ProxyFix(server.wsgi_app, x_proto=1, x_host=1)

    class Sessions(SecureCookieSessionInterface):
        def get_cookie_secure(self, app):            # Secure whenever the request came over HTTPS
            return request.is_secure

    server.session_interface = Sessions()

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def csrf_ok() -> bool:
        return bool(request.form.get("csrf")) and secrets.compare_digest(request.form.get("csrf", ""),
                                                                         session.get("csrf", ""))

    def login_html(error: str = "", note: str = "", username: str = "", nxt: str = "/") -> str:
        return _page("Sign in", f"""
<h1>Sign in</h1><p>Use the account your ResNet administrator gave you.</p>
<form method='post' action='/login?next={quote(nxt)}' autocomplete='on'>
<input type='hidden' name='csrf' value='{csrf_token()}'>
<label for='u'>Username</label><input id='u' name='username' autocomplete='username' autocapitalize='none'
 spellcheck='false' required value='{html.escape(username)}' autofocus>
<label for='p'>Password</label><input id='p' name='password' type='password' autocomplete='current-password' required>
<button type='submit'>Sign in</button>
{f"<div class='err' role='alert'>{html.escape(error)}</div>" if error else ""}
{f"<div class='ok' role='status'>{html.escape(note)}</div>" if note else ""}
</form><div class='foot'>Forgot your password? Ask your administrator to reset it.</div>""")

    def password_html(error: str = "", forced: bool = False) -> str:
        who = g.wepa_user
        lead = ("Your password was set by an administrator. Choose your own to continue." if forced
                else "Choose a new password. You'll stay signed in here; other sessions end.")
        return _page("Change password", f"""
<h1>Change your password</h1><p>{lead}</p>
<form method='post' action='/account/password'>
<input type='hidden' name='csrf' value='{csrf_token()}'>
<input type='hidden' name='username' autocomplete='username' value='{html.escape(who["username"])}'>
<label for='c'>Current password</label><input id='c' name='current' type='password' autocomplete='current-password' required>
<label for='n'>New password</label><input id='n' name='new' type='password' autocomplete='new-password' minlength='{accounts.MIN_PASSWORD}'
 maxlength='{accounts.MAX_PASSWORD}' required>
<div class='hint'>At least {accounts.MIN_PASSWORD} characters. A few unrelated words is strong and easy to remember. Avoid your
name, common passwords and anything you've used elsewhere.</div>
<label for='n2'>New password again</label><input id='n2' name='new2' type='password' autocomplete='new-password' required>
<button type='submit'>Change password</button>
{f"<div class='err' role='alert'>{html.escape(error)}</div>" if error else ""}
</form>{"" if forced else "<div class='foot'><a href='/account'>Back to My account</a></div>"}""")

    @server.before_request
    def _gate():
        if not enabled():
            return None
        path = request.path
        # Same-origin check for anything that changes state.
        if request.method == "POST" and request.headers.get("Origin"):
            o = urlparse(request.headers["Origin"])
            if o.netloc and o.netloc != request.host:
                return Response("Cross-site request refused.", 403)
        from .dashboard.public import is_public
        if is_public(path) or path.startswith(OPEN_PATHS):
            return None
        now = time.time()
        who = None
        if session.get("u"):
            fresh = now - session.get("seen", 0) < IDLE_S and now - session.get("iat", 0) < ABSOLUTE_S
            who = accounts.session_valid(session["u"], session.get("e")) if fresh else None
            if who is None:
                session.clear()
        if who is None:
            if path.startswith(("/_dash", "/_session", "/_reload")) or request.accept_mimetypes.best == "application/json":
                return jsonify(error="signed out"), 401
            return redirect("/login?next=" + quote(_safe_next(request.full_path.rstrip("?"))))
        if now - session.get("seen", 0) > 60:
            session["seen"] = now
        g.wepa_user = who
        if who.get("must_change") and path not in ("/account/password",) and not path.startswith("/_dash-component-suites"):
            if path.startswith(("/_dash", "/_session")):
                return jsonify(error="password change required"), 401
            return redirect("/account/password")
        return None

    @server.route("/login", methods=["GET", "POST"])
    def _login():
        nxt = _safe_next(request.args.get("next"))
        if not enabled():
            return redirect("/")
        if request.method == "GET":
            return Response(login_html(nxt=nxt, note="You've been signed out." if request.args.get("out") else ""),
                            mimetype="text/html", headers={"Cache-Control": "no-store"})
        ip = security.client_ip(request)
        net = security.truncate_ip(ip)
        username = (request.form.get("username") or "").strip().lower()[:64]
        if security._is_locked(net):
            return Response(login_html("Too many failed sign-ins from this network. Try again in 15 minutes.",
                                       username=username, nxt=nxt), 429, mimetype="text/html")
        if not csrf_ok():
            return Response(login_html("Your sign-in form expired. Please try again.", username=username, nxt=nxt),
                            400, mimetype="text/html")
        who = accounts.authenticate(username, request.form.get("password", ""))
        if who is None:
            security._failed(net, username, ip, request.headers.get("User-Agent", ""))
            msg = ("This account is temporarily locked after several wrong passwords. Try again in 15 minutes."
                   if accounts.account_locked(username) else "That username and password don't match an active account.")
            return Response(login_html(msg, username=username, nxt=nxt), 401, mimetype="text/html")
        session.clear()                                   # a fresh session: no fixation
        now = time.time()
        session.update(u=who["username"], e=who.get("epoch", 0), iat=now, seen=now, csrf=secrets.token_urlsafe(24))
        session.permanent = True
        security._record("login", user=who["username"], network=net, where=security.where(ip),
                         device=security.device(request.headers.get("User-Agent", "")))
        security.log(f"{who['username']} signed in from {net}", "audit")
        return redirect(nxt)

    @server.route("/logout", methods=["GET", "POST"])
    def _logout():
        u = session.get("u")
        session.clear()
        if u:
            security.log(f"{u} signed out", "audit")
        return redirect("/login?out=1")

    @server.route("/account/password", methods=["GET", "POST"])
    def _password():
        if not enabled():
            return redirect("/")
        who = g.wepa_user
        forced = bool(who.get("must_change"))
        if who["role"] == "admin":
            return Response(_page("Administrator", "<h1>Administrator password</h1><p>The administrator password is set "
                                  "in the app's configuration (WEPA_BASIC_AUTH), not here.</p><a href='/'>Back</a>"),
                            mimetype="text/html")
        if request.method == "GET":
            return Response(password_html(forced=forced), mimetype="text/html", headers={"Cache-Control": "no-store"})
        if not csrf_ok():
            return Response(password_html("The form expired. Please try again.", forced), 400, mimetype="text/html")
        new, new2 = request.form.get("new", ""), request.form.get("new2", "")
        if new != new2:
            return Response(password_html("The new passwords don't match.", forced), 400, mimetype="text/html")
        try:
            u = accounts.change_password(who["username"], request.form.get("current", ""), new)
        except accounts.AccountError as exc:
            return Response(password_html(str(exc), forced), 400, mimetype="text/html")
        session["e"] = u["epoch"]                         # this session continues; all others end
        security.audit("password changed", who["username"])
        return redirect("/account?changed=1")

    @server.get("/_avatar/<username>.png")
    def _avatar(username):
        from flask import abort, send_file
        p = accounts.photo_path(username)
        if p is None:
            abort(404)
        return send_file(p, mimetype="image/png", max_age=3600)
