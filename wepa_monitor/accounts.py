"""The staff directory: one central, administrator-managed list of accounts.

Who sets what
-------------
* The administrator (the shared `bsuresnet` credential in WEPA_BASIC_AUTH) manages every account from the
  command line (`python -m wepa_monitor users …`), which calls this app's admin API over HTTPS. The
  administrator sets: username, name, email, role, active/inactive, and whether the person is a resident
  student and, if so, their home residence hall. Nothing about other people can be changed from a web page.
* Each person manages, from My account: their profile picture, their contact phone number, and their
  password.

Accounts are never deleted (the audit trail refers to them); they're deactivated, which ends their
sessions at once.

Password and sign-in practices (NIST SP 800-63B, without MFA)
-------------------------------------------------------------
* At least MIN_PASSWORD (12) and at most 128 characters; any characters, paste allowed; no forced
  composition rules and no periodic expiry.
* Rejected if it's a common password, contains the username, the person's name or email, or this
  service's name, is one repeated character or a simple sequence, or (when the service is reachable)
  appears in known data breaches (Have I Been Pwned, k-anonymity: only the first 5 characters of the
  password's SHA-1 hash leave the server).
* Stored only as a salted, memory-hard hash (scrypt via werkzeug).
* New and reset passwords are temporary: the person must choose their own at the next sign-in.
* Throttling: MAX_ACCOUNT_FAILURES wrong passwords for one account within 15 minutes lock that account
  for 15 minutes (on top of the per-network lockout in security.py). Error messages never say whether
  the username exists.
* Changing a password, a reset, or deactivation signs the person out everywhere (session epoch).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

MIN_PASSWORD, MAX_PASSWORD = 12, 128
ROLES = ("staff", "viewer")
USERNAME = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")
MAX_ACCOUNT_FAILURES = 5
ACCOUNT_LOCK_S = 15 * 60
PHOTO_MAX_BYTES = 5 * 1024 * 1024
PHOTO_SIZE = 256
CONTEXT_WORDS = ("wepa", "resnet", "bridgewater", "bsuresnet", "printops", "printing", "studentprinting", "password")
COMMON = set("""
123456789012 1234567890123 password1234 password12345 passwordpassword qwertyuiopas qwerty123456 1q2w3e4r5t6y
iloveyou1234 letmein12345 welcome12345 admin1234567 administrator abc123456789 111111111111 000000000000
123123123123 qwertyqwerty football1234 baseball1234 monkey123456 dragon123456 sunshine1234 princess1234
superman1234 trustno11234 changeme1234 passw0rd1234 p@ssw0rd1234 p@ssword1234 summer202020 winter202020
spring202020 autumn202020 fall20202020 1qaz2wsx3edc zaq12wsxcde3 asdfghjkl123 zxcvbnm12345 qazwsxedcrfv
""".split())

_lock = threading.Lock()
_path: Path | None = None
_users: dict[str, dict] = {}
_mtime = 0.0
_fail: dict[str, list[float]] = {}
_locked: dict[str, float] = {}


def _admin_epoch() -> str:
    """Changes whenever the administrator's password does, ending every administrator session."""
    return hashlib.sha256(os.environ.get("WEPA_BASIC_AUTH", "").encode()).hexdigest()[:16]


class AccountError(ValueError):
    pass


# --- storage ------------------------------------------------------------------------------------

def configure(store: Path) -> None:
    global _path
    _path = Path(store) / "users.json"
    _reload(force=True)


def store_dir() -> Path | None:
    return _path.parent if _path else None


def _normalize(u: dict) -> dict:
    """Older records (before the directory fields existed) read as active staff with no email."""
    u.setdefault("email", "")
    u.setdefault("active", not u.pop("disabled", False))
    u.setdefault("resident", False)
    u.setdefault("hall", None)
    u.setdefault("phone", None)
    u.setdefault("photo", 0)
    u.setdefault("must_change", False)
    u.setdefault("epoch", 0)
    return u


def _reload(force: bool = False) -> None:
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
            _users = {k: _normalize(v) for k, v in json.loads(_path.read_text()).items()}
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
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, _path)
    _mtime = _path.stat().st_mtime


PRIVATE = ("hash", "fail", "password_changed")


def public(u: dict) -> dict:
    return {k: v for k, v in u.items() if k not in PRIVATE}


def admin_name() -> str:
    return os.environ.get("WEPA_BASIC_AUTH", "").partition(":")[0]


def users() -> list[dict]:
    with _lock:
        _reload()
        return [public(u) for u in sorted(_users.values(), key=lambda u: u["username"])]


def get(username: str) -> dict | None:
    with _lock:
        _reload()
        u = _users.get((username or "").lower())
        return public(u) if u else None


# --- validation -----------------------------------------------------------------------------------

def residence_halls() -> list[str]:
    """Halls a resident student can be assigned to: BSU's residence halls plus residence-hall printer
    buildings."""
    names = set()
    try:
        from . import campus
        c = campus.load()
        names |= set(c.halls["building"].dropna()) if len(c.halls) else set()
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import reference
        st = reference.load_stations()
        names |= set(st.loc[(st["station_type"] == "residence") & (st["access"] != "public"), "building"].dropna())
    except Exception:  # noqa: BLE001
        pass
    return sorted(names)


def password_problems(pw: str, username: str = "", name: str = "", email: str = "", check_breaches: bool = True) -> list[str]:
    pw = pw or ""
    low = pw.lower()
    out = []
    if len(pw) < MIN_PASSWORD:
        out.append(f"use at least {MIN_PASSWORD} characters (a few unrelated words works well)")
    if len(pw) > MAX_PASSWORD:
        out.append(f"use at most {MAX_PASSWORD} characters")
    if low in COMMON or low.rstrip("!1") in COMMON:
        out.append("that's a commonly used password")
    if len(set(pw)) <= 2 or low in "0123456789012345678901234567890" or low in "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz":
        out.append("avoid repeated characters and simple sequences")
    personal = [w for w in [username, email.split("@")[0], *re.split(r"\s+", name or "")] if len(w or "") >= 3]
    if any(w.lower() in low for w in personal):
        out.append("don't include your username, name or email")
    if any(w in low for w in CONTEXT_WORDS):
        out.append("don't include this service's name (Wepa, ResNet, Bridgewater)")
    if not out and check_breaches and os.environ.get("WEPA_BREACH_CHECK", "1") == "1" and breached(pw):
        out.append("that password has appeared in a known data breach")
    return out


def breached(pw: str) -> bool:
    """Have I Been Pwned range API (k-anonymity). False when unreachable: it's a best-effort extra."""
    try:
        import requests
        h = hashlib.sha1(pw.encode(), usedforsecurity=False).hexdigest().upper()   # the HIBP range API is keyed by SHA-1
        r = requests.get(f"https://api.pwnedpasswords.com/range/{h[:5]}", timeout=3,
                         headers={"Add-Padding": "true", "User-Agent": "BSU-Student-Printing-Ops"})
        if r.status_code != 200:
            return False
        return any(line.split(":")[0] == h[5:] and line.split(":")[1].strip() != "0" for line in r.text.splitlines())
    except Exception:  # noqa: BLE001
        return False


def _check_password(pw: str, u: dict) -> None:
    probs = password_problems(pw, u.get("username", ""), u.get("name", ""), u.get("email", ""))
    if probs:
        raise AccountError("Password not accepted: " + "; ".join(probs) + ".")


def normalize_phone(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        raise AccountError("Enter a 10-digit US phone number, like (508) 531-1000.")
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def _validate_directory(fields: dict) -> dict:
    out = {}
    if "name" in fields:
        n = (fields["name"] or "").strip()
        if not n:
            raise AccountError("A name is required (it appears on every change the person makes).")
        out["name"] = n[:60]
    if "email" in fields:
        e = (fields["email"] or "").strip().lower()
        if not EMAIL.match(e):
            raise AccountError("Enter a valid email address.")
        out["email"] = e
    if "role" in fields:
        if fields["role"] not in ROLES:
            raise AccountError(f"Role must be one of: {', '.join(ROLES)}.")
        out["role"] = fields["role"]
    if "active" in fields:
        out["active"] = bool(fields["active"])
    if "resident" in fields or "hall" in fields:
        res = bool(fields.get("resident", False))
        hall = (fields.get("hall") or "").strip() or None
        if res and not hall:
            raise AccountError("A resident student needs a home residence hall (--hall).")
        if res:
            halls = residence_halls()
            match = next((h for h in halls if h.lower() == hall.lower()), None)
            if halls and not match:
                raise AccountError(f"Unknown residence hall '{hall}'. Choose one of: {', '.join(halls)}.")
            hall = match or hall
        out["resident"], out["hall"] = res, (hall if res else None)
    return out


# --- administrator actions (via the admin API / CLI) ------------------------------------------------

def generate_password() -> str:
    return "-".join(secrets.token_urlsafe(5) for _ in range(4))


def create(username: str, name: str, password: str, role: str = "staff", by: str = "", email: str = "",
           resident: bool = False, hall: str | None = None, temporary: bool = True) -> dict:
    from werkzeug.security import generate_password_hash
    username = (username or "").strip().lower()
    if not USERNAME.match(username):
        raise AccountError("Username: 2-32 characters, lowercase letters, digits, '.', '_' or '-', starting with a letter.")
    if username == admin_name():
        raise AccountError("That username is the administrator account.")
    fields = _validate_directory({"name": name, "email": email, "role": role, "resident": resident, "hall": hall})
    with _lock:
        _reload()
        if username in _users:
            raise AccountError(f"'{username}' already exists (accounts are never deleted; activate it instead).")
        if any(u.get("email") == fields["email"] for u in _users.values()):
            raise AccountError("Another account already uses that email address.")
        u = {"username": username, **fields, "active": True, "phone": None, "photo": 0, "epoch": 0,
             "must_change": temporary, "created": time.time(), "created_by": by}
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"Password not accepted: use at least {MIN_PASSWORD} characters.")
        u["hash"] = generate_password_hash(password)
        _users[username] = u
        _save()
        return public(u)


def admin_update(username: str, by: str = "", **changes) -> dict:
    """Administrator changes: directory fields, active, or a password reset (temporary)."""
    from werkzeug.security import generate_password_hash
    with _lock:
        _reload()
        u = _users.get((username or "").lower())
        if not u:
            raise AccountError(f"No account '{username}'.")
        given = {k: changes[k] for k in ("name", "email", "role", "active") if k in changes}
        if "resident" in changes or "hall" in changes:
            given["resident"] = changes.get("resident", u.get("resident", False))
            given["hall"] = changes.get("hall", u.get("hall"))
        fields = _validate_directory(given)
        if "email" in fields and any(x.get("email") == fields["email"] and x["username"] != u["username"]
                                     for x in _users.values()):
            raise AccountError("Another account already uses that email address.")
        if "password" in changes:
            if len(changes["password"] or "") < MIN_PASSWORD:
                raise AccountError(f"Password not accepted: use at least {MIN_PASSWORD} characters.")
            u["hash"] = generate_password_hash(changes["password"])
            u["must_change"] = True
            u["epoch"] = u.get("epoch", 0) + 1
            u["password_changed"] = time.time()
            _fail.pop(u["username"], None)
            _locked.pop(u["username"], None)
        if fields.get("active") is False and u.get("active", True):
            u["epoch"] = u.get("epoch", 0) + 1                     # sign them out everywhere
        u.update(fields)
        u["updated"], u["updated_by"] = time.time(), by
        _save()
        return public(u)


# Kept for callers from the first version of accounts.
def update(username: str, by: str = "", **changes) -> dict:
    if "disabled" in changes:
        changes["active"] = not changes.pop("disabled")
    return admin_update(username, by=by, **changes)


# --- self-service (My account) ------------------------------------------------------------------------

def set_phone(username: str, raw: str | None) -> dict:
    phone = normalize_phone(raw)
    with _lock:
        _reload()
        u = _users[username]
        u["phone"], u["updated"], u["updated_by"] = phone, time.time(), username
        _save()
        return public(u)


def set_photo(username: str, data: bytes) -> dict:
    """Re-encode the upload: square crop, 256 px PNG, every bit of metadata (EXIF, GPS) dropped."""
    from PIL import Image, ImageOps
    if len(data) > PHOTO_MAX_BYTES:
        raise AccountError("That picture is over 5 MB.")
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in ("PNG", "JPEG", "WEBP", "GIF"):
            raise AccountError("Use a PNG, JPEG, WebP or GIF picture.")
        im.load()
        im = ImageOps.exif_transpose(im).convert("RGB")
    except AccountError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AccountError("That file isn't a picture we can read.") from exc
    im = ImageOps.fit(im, (PHOTO_SIZE, PHOTO_SIZE), Image.LANCZOS)
    d = store_dir() / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    (d / f"{username}.png").write_bytes(buf.getvalue())
    with _lock:
        _reload()
        u = _users[username]
        u["photo"], u["updated"], u["updated_by"] = int(time.time()), time.time(), username
        _save()
        return public(u)


def remove_photo(username: str) -> dict:
    p = store_dir() / "avatars" / f"{username}.png"
    p.unlink(missing_ok=True)
    with _lock:
        _reload()
        u = _users[username]
        u["photo"] = 0
        _save()
        return public(u)


def photo_path(username: str) -> Path | None:
    if not USERNAME.match(username or ""):
        return None
    p = store_dir() / "avatars" / f"{username}.png" if store_dir() else None
    return p if p and p.exists() else None


def change_password(username: str, current: str, new: str) -> dict:
    from werkzeug.security import check_password_hash, generate_password_hash
    with _lock:
        _reload()
        u = _users.get(username)
    if not u or not check_password_hash(u["hash"], current or ""):
        raise AccountError("Your current password isn't right.")
    if current == new:
        raise AccountError("Choose a new password, not the current one.")
    _check_password(new, u)
    with _lock:
        _reload()
        u = _users[username]
        u["hash"] = generate_password_hash(new)
        u["must_change"] = False
        u["epoch"] = u.get("epoch", 0) + 1                            # other sessions end
        u["password_changed"] = time.time()
        _save()
        return public(u)


# --- sign-in --------------------------------------------------------------------------------------

_DUMMY_HASH = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        from werkzeug.security import generate_password_hash
        _DUMMY_HASH = generate_password_hash(secrets.token_hex(16))
    return _DUMMY_HASH


def account_locked(username: str) -> bool:
    return _locked.get((username or "").lower(), 0) > time.time()


def _note_failure(username: str) -> None:
    now = time.time()
    q = [t for t in _fail.get(username, []) if now - t < ACCOUNT_LOCK_S] + [now]
    _fail[username] = q
    if len(q) >= MAX_ACCOUNT_FAILURES:
        _locked[username] = now + ACCOUNT_LOCK_S
        _fail[username] = []


def authenticate(username: str, password: str) -> dict | None:
    """The account if the password is right and it may sign in; None otherwise. Always spends the same
    hashing effort, so response time doesn't reveal whether a username exists."""
    from werkzeug.security import check_password_hash
    username = (username or "").strip().lower()
    if account_locked(username):
        check_password_hash(_dummy_hash(), password or "")
        return None
    adm_user, _, adm_hash = os.environ.get("WEPA_BASIC_AUTH", "").partition(":")
    if adm_user and username == adm_user:
        ok = bool(password) and check_password_hash(adm_hash, password)
        if not ok:
            _note_failure(username)
        return {"username": adm_user, "name": "Administrator", "role": "admin", "epoch": _admin_epoch()} if ok else None
    with _lock:
        _reload()
        u = _users.get(username)
    ok = check_password_hash(u["hash"] if u else _dummy_hash(), password or "")
    if not (u and ok):
        if u:
            _note_failure(username)
        return None
    if not u.get("active", True):
        return None
    _fail.pop(username, None)
    with _lock:
        _reload()
        _users[username]["last_login"] = time.time()
        _save()
    return public(_users[username])


def check(username: str, password: str) -> bool:
    return authenticate(username, password) is not None


def session_valid(username: str, epoch) -> dict | None:
    """The account behind a session, if it's still active and the session predates no sign-out-everywhere."""
    if username == admin_name() and admin_name():
        return {"username": username, "name": "Administrator", "role": "admin"} if epoch == _admin_epoch() else None
    u = get(username)
    if not u or not u.get("active", True) or u.get("epoch", 0) != epoch:
        return None
    return u


# --- who is making this request ---------------------------------------------------------------------

def current() -> dict:
    """The signed-in person: username, name, role, and whether they may edit investigations."""
    if not os.environ.get("WEPA_BASIC_AUTH"):
        return {"username": "local", "name": "Local user (no sign-in configured)", "role": "staff",
                "can_edit": True, "shared": False}
    try:
        from flask import g
        who = getattr(g, "wepa_user", None)
    except Exception:  # noqa: BLE001 - outside a request
        who = None
    if not who:
        return {"username": "", "name": "Not signed in", "role": "viewer", "can_edit": False, "shared": False}
    if who["role"] == "admin":
        return {**who, "can_edit": False, "shared": True}
    return {**who, "can_edit": who["role"] == "staff" and who.get("active", True), "shared": False}


# --- admin API (used by the CLI) ---------------------------------------------------------------------

def install_admin_api(server) -> None:
    """JSON endpoints under /_admin/users for the administrator only, with HTTP Basic credentials sent by
    the CLI over HTTPS. The custom header blocks cross-site form posts."""
    from flask import jsonify, request

    from . import security

    def guard():
        if request.headers.get("X-Wepa-Admin") != "1":
            return jsonify(error="missing X-Wepa-Admin header"), 400
        a = request.authorization
        if not os.environ.get("WEPA_BASIC_AUTH") or not a or a.username != admin_name():
            return jsonify(error="only the administrator can manage accounts"), 403
        ip = security.client_ip(request)
        if security._is_locked(security.truncate_ip(ip)):
            return jsonify(error="too many failed sign-ins from this network"), 429
        if authenticate(a.username, a.password or "") is None:
            security._failed(security.truncate_ip(ip), a.username or "", ip, request.headers.get("User-Agent", ""))
            return jsonify(error="sign-in failed"), 401
        return None

    @server.get("/_admin/users")
    def _list_users():
        return guard() or jsonify(users=users(), halls=residence_halls())

    @server.post("/_admin/users")
    def _create_user():
        bad = guard()
        if bad:
            return bad
        b = request.get_json(silent=True) or {}
        try:
            u = create(b.get("username", ""), b.get("name", ""), b.get("password", ""), b.get("role", "staff"),
                       by=admin_name(), email=b.get("email", ""), resident=bool(b.get("resident")), hall=b.get("hall"))
        except AccountError as exc:
            return jsonify(error=str(exc)), 409 if "already" in str(exc) else 400
        security.audit("account created", f"{u['username']} ({u['role']})")
        return jsonify(user=u), 201

    @server.post("/_admin/users/<username>")
    def _update_user(username):
        bad = guard()
        if bad:
            return bad
        b = request.get_json(silent=True) or {}
        if "disabled" in b:
            b["active"] = not b.pop("disabled")
        allowed = {k: b[k] for k in ("name", "email", "role", "active", "resident", "hall", "password") if k in b}
        try:
            u = admin_update(username, by=admin_name(), **allowed)
        except AccountError as exc:
            return jsonify(error=str(exc)), 404 if "No account" in str(exc) else 400
        what = ", ".join(sorted("password reset" if k == "password" else
                                (("activated" if allowed[k] else "deactivated") if k == "active" else k) for k in allowed))
        security.audit("account updated", f"{username}: {what}")
        return jsonify(user=u)
