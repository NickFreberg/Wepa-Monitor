"""Named staff accounts, so every change to an investigation records who made it.

* The shared administrator (WEPA_BASIC_AUTH, "bsuresnet") can sign in and view everything, and is the
  only account that can manage users. It can't edit investigations: governance needs a name on each
  change, and a shared account has no single name.
* Staff accounts are created from the command line (`python -m wepa_monitor users add …`), which calls
  the running app's admin API over HTTPS with the administrator's credentials. Nothing is created from
  the web pages.
* Accounts are never deleted (the audit trail refers to them); they are disabled instead. Passwords
  are stored only as salted hashes (werkzeug scrypt) and must be at least MIN_PASSWORD characters.

Roles: "staff" can work investigations; "viewer" can read everything but change nothing.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

MIN_PASSWORD = 12
ROLES = ("staff", "viewer")
USERNAME = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")

_lock = threading.Lock()
_path: Path | None = None
_users: dict[str, dict] = {}
_mtime = 0.0


class AccountError(ValueError):
    pass


def configure(store: Path) -> None:
    global _path
    _path = Path(store) / "users.json"
    _reload(force=True)


def _reload(force: bool = False) -> None:
    """Pick up changes written by another process (the CLI's local mode, another replica)."""
    global _users, _mtime
    if _path is None:
        return
    try:
        m = _path.stat().st_mtime
    except OSError:
        _users, _mtime = {}, 0.0
        return
    if force or m != _mtime:
        try:
            _users = json.loads(_path.read_text())
            _mtime = m
        except (OSError, ValueError):
            pass


def _save() -> None:
    global _mtime
    if _path is None:
        raise AccountError("account storage isn't configured")
    _path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_users, indent=1, sort_keys=True))
    os.replace(tmp, _path)
    _mtime = _path.stat().st_mtime


def admin_name() -> str:
    return os.environ.get("WEPA_BASIC_AUTH", "").partition(":")[0]


def public(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "hash"}


def users() -> list[dict]:
    with _lock:
        _reload()
        return [public(u) for u in sorted(_users.values(), key=lambda u: u["username"])]


def get(username: str) -> dict | None:
    with _lock:
        _reload()
        u = _users.get((username or "").lower())
        return public(u) if u else None


def check(username: str, password: str) -> bool:
    from werkzeug.security import check_password_hash
    with _lock:
        _reload()
        u = _users.get((username or "").lower())
    return bool(u and not u.get("disabled") and password and check_password_hash(u["hash"], password))


def _validate_password(pw: str) -> None:
    if len(pw or "") < MIN_PASSWORD:
        raise AccountError(f"password must be at least {MIN_PASSWORD} characters")


def generate_password() -> str:
    return "-".join(secrets.token_urlsafe(5) for _ in range(4))


def create(username: str, name: str, password: str, role: str = "staff", by: str = "") -> dict:
    from werkzeug.security import generate_password_hash
    username = (username or "").strip().lower()
    if not USERNAME.match(username):
        raise AccountError("username: 2-32 characters, lowercase letters, digits, '.', '_' or '-', starting with a letter")
    if username == admin_name():
        raise AccountError("that username is the shared administrator")
    if role not in ROLES:
        raise AccountError(f"role must be one of {', '.join(ROLES)}")
    if not (name or "").strip():
        raise AccountError("a display name is required (it's what appears on every change)")
    _validate_password(password)
    with _lock:
        _reload()
        if username in _users:
            raise AccountError(f"'{username}' already exists (accounts are never deleted; enable it instead)")
        _users[username] = {"username": username, "name": name.strip()[:60], "role": role,
                            "hash": generate_password_hash(password), "created": time.time(), "created_by": by,
                            "disabled": False}
        _save()
        return public(_users[username])


def update(username: str, by: str = "", **changes) -> dict:
    from werkzeug.security import generate_password_hash
    with _lock:
        _reload()
        u = _users.get((username or "").lower())
        if not u:
            raise AccountError(f"no account '{username}'")
        if "password" in changes:
            _validate_password(changes["password"])
            u["hash"] = generate_password_hash(changes.pop("password"))
            u["password_changed"] = time.time()
        for k in ("disabled", "name", "role"):
            if k in changes:
                if k == "role" and changes[k] not in ROLES:
                    raise AccountError(f"role must be one of {', '.join(ROLES)}")
                u[k] = changes[k]
        u["updated"], u["updated_by"] = time.time(), by
        _save()
        return public(u)


# --- who is making this request -------------------------------------------------------------------

def current() -> dict:
    """The signed-in person for this request: username, name, role, and whether they may edit."""
    try:
        from flask import request
        a = request.authorization
    except Exception:  # noqa: BLE001 - outside a request (tests, CLI)
        a = None
    if not os.environ.get("WEPA_BASIC_AUTH"):
        return {"username": "local", "name": "Local user (no sign-in configured)", "role": "staff",
                "can_edit": True, "shared": False}
    if a and a.username == admin_name():
        return {"username": a.username, "name": "Shared administrator", "role": "admin", "can_edit": False,
                "shared": True}
    u = get(a.username) if a else None
    if not u:
        return {"username": "", "name": "Unknown", "role": "viewer", "can_edit": False, "shared": False}
    return {**u, "can_edit": u["role"] == "staff" and not u.get("disabled"), "shared": False}


# --- admin API (used by the CLI) -------------------------------------------------------------------

def install_admin_api(server) -> None:
    """JSON endpoints under /_admin/users, for the shared administrator only. The custom header
    blocks cross-site form posts riding on a browser's saved sign-in."""
    from flask import jsonify, request

    from . import security

    def guard():
        if request.headers.get("X-Wepa-Admin") != "1":
            return jsonify(error="missing X-Wepa-Admin header"), 400
        a = request.authorization
        if not os.environ.get("WEPA_BASIC_AUTH") or not a or a.username != admin_name():
            return jsonify(error="only the shared administrator can manage accounts"), 403
        return None

    @server.get("/_admin/users")
    def _list_users():
        return guard() or jsonify(users=users())

    @server.post("/_admin/users")
    def _create_user():
        bad = guard()
        if bad:
            return bad
        body = request.get_json(silent=True) or {}
        try:
            u = create(body.get("username", ""), body.get("name", ""), body.get("password", ""),
                       body.get("role", "staff"), by=admin_name())
        except AccountError as exc:
            return jsonify(error=str(exc)), 409 if "exists" in str(exc) else 400
        security.audit("account created", f"{u['username']} ({u['role']})")
        return jsonify(user=u), 201

    @server.post("/_admin/users/<username>")
    def _update_user(username):
        bad = guard()
        if bad:
            return bad
        body = request.get_json(silent=True) or {}
        allowed = {k: body[k] for k in ("disabled", "name", "role", "password") if k in body}
        try:
            u = update(username, by=admin_name(), **allowed)
        except AccountError as exc:
            return jsonify(error=str(exc)), 404 if "no account" in str(exc) else 400
        what = ", ".join(sorted(k if k != "disabled" else ("disabled" if allowed[k] else "enabled") for k in allowed))
        security.audit("account updated", f"{username}: {what}")
        return jsonify(user=u)
