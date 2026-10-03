"""Site security and usage: sign-in protection (the sign-in itself is in auth.py and accounts.py), security headers, who is using
the site, and a small audit trail. All of it powers the System page.

Privacy by design (this records people, so it records as little as possible):
* No passwords are ever stored, not even wrong ones; only that an attempt failed, for which
  username, when, and from where.
* IP addresses are kept truncated (IPv4 to /24, e.g. 203.0.113.x; IPv6 to /48), enough to tell
  campus from off campus and to block a guessing attack, not enough to identify a device.
* "Where" is the network label from WEPA_CAMPUS_NETWORKS (CIDR list, e.g. BSU's ranges), else
  "Off campus"; no third-party geolocation lookups.
* Records older than RETENTION_DAYS are dropped.

Brute-force protection: after MAX_FAILURES failed sign-ins from one network in LOCKOUT_WINDOW,
that network gets HTTP 429 for LOCKOUT_FOR before it may try again.
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

RETENTION_DAYS = 90
MAX_FAILURES = 8
LOCKOUT_WINDOW = 15 * 60
LOCKOUT_FOR = 15 * 60
ACTIVE_WITHIN = 150          # a tab that sent a heartbeat this recently is "active now" (beats every 60 s)
SESSION_GAP = 30 * 60        # silence longer than this ends a session

_lock = threading.Lock()
_events: deque = deque(maxlen=5000)          # (ts, kind, detail dict)
_sessions: dict[str, dict] = {}              # tab id -> session
_failures: dict[str, deque] = {}             # network -> recent failure times
_locked_until: dict[str, float] = {}
_store: Path | None = None
console: deque = deque(maxlen=400)           # the System page's live log (see log())


def log(message: str, kind: str = "info") -> None:
    """Add a line to the live log on the System page (and print it, for the host's own log)."""
    console.append((time.time(), kind, message))
    print(message, flush=True)


# --- addresses and devices -----------------------------------------------------------------------

def client_ip(request) -> str:
    """The visitor's address. Behind Azure's ingress the real one is the first X-Forwarded-For entry."""
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


def truncate_ip(ip: str) -> str:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if a.version == 4:
        return ".".join(str(a).split(".")[:3]) + ".x"
    return str(ipaddress.ip_network(f"{a}/48", strict=False).network_address) + "/48"


def _campus_networks() -> list:
    nets = []
    for part in os.environ.get("WEPA_CAMPUS_NETWORKS", "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return nets


def where(ip: str) -> str:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "Unknown"
    if a.is_loopback:
        return "This computer"
    if a.is_private:
        return "Local network"
    if any(a in n for n in _campus_networks()):
        return "BSU network"
    return "Off campus"


def device(user_agent: str) -> str:
    """'iPhone · Safari', 'Mac · Chrome', 'Windows · Edge'."""
    ua = user_agent or ""
    os_ = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else
           "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "ChromeOS" if "CrOS" in ua else
           "Linux" if "Linux" in ua else "Unknown device")
    br = ("Edge" if "Edg/" in ua else "Firefox" if "Firefox/" in ua else "Chrome" if "Chrome/" in ua else
          "Safari" if "Safari/" in ua else "Browser")
    return f"{os_} · {br}"


# --- persistence ------------------------------------------------------------------------------------

def _record(kind: str, **detail) -> None:
    now = time.time()
    with _lock:
        if kind != "session":                 # sessions live in _sessions; keep the event list for alerts
            _events.append((now, kind, detail))
        if _store is not None:
            try:
                _store.mkdir(parents=True, exist_ok=True)
                month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
                with (_store / f"events-{month}.jsonl").open("a") as f:
                    f.write(json.dumps({"ts": now, "kind": kind, **detail}) + "\n")
            except OSError:
                pass


def _load(store: Path) -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    for path in sorted(store.glob("events-*.jsonl")):
        try:
            month = datetime.strptime(path.stem.split("-", 1)[1], "%Y-%m").replace(tzinfo=timezone.utc)
            if month.timestamp() < cutoff - 31 * 86400:
                path.unlink(missing_ok=True)      # retention
                continue
            for line in path.read_text().splitlines()[-5000:]:
                e = json.loads(line)
                if e.get("ts", 0) >= cutoff:
                    ts, kind = e.pop("ts"), e.pop("kind")
                    _events.append((ts, kind, e))
                    if kind == "session":
                        _sessions[e.get("tab", "")] = {**e, "first": e.get("first", ts), "last": e.get("last", ts)}
        except (OSError, ValueError):
            continue


def audit(kind: str, detail: str = "") -> None:
    """Something worth a line in the audit trail (an export, a password change)."""
    _record("audit", what=kind, detail=detail)
    log(f"{kind}: {detail}" if detail else kind, "audit")


# --- sign-in -------------------------------------------------------------------------------------------

def _is_locked(net: str) -> bool:
    until = _locked_until.get(net, 0)
    return until > time.time()


def _failed(net: str, user: str, ip: str, ua: str) -> None:
    now = time.time()
    q = _failures.setdefault(net, deque())
    # The browser re-sends the same wrong password with every request until the prompt reappears:
    # count one attempt per network+username per few seconds.
    last = next((e for e in reversed(_events) if e[1] == "login_failed"), None)
    if last and now - last[0] < 3 and last[2].get("network") == net and last[2].get("user") == user:
        return
    q.append(now)
    while q and now - q[0] > LOCKOUT_WINDOW:
        q.popleft()
    _record("login_failed", user=user[:40], network=net, where=where(ip), device=device(ua))
    log(f"sign-in failed for '{user[:40]}' from {net} ({where(ip)})", "warn")
    if len(q) >= MAX_FAILURES:
        _locked_until[net] = now + LOCKOUT_FOR
        q.clear()
        _record("lockout", network=net, minutes=LOCKOUT_FOR // 60)
        log(f"too many failed sign-ins from {net}: blocked for {LOCKOUT_FOR // 60} minutes", "warn")


def install(server, data_dir: Path | None = None) -> None:
    """Security headers on every response; the optional login (WEPA_BASIC_AUTH = "user:<werkzeug
    hash>"); the session heartbeat endpoint; persistent records next to the data."""
    global _store
    from flask import jsonify, request

    if data_dir is not None and _store is None:
        _store = Path(data_dir).parent / "security" if Path(data_dir).name == "live" else Path(data_dir) / "security"
        if _store.exists():
            _load(_store)
    from . import accounts
    if data_dir is not None:
        accounts.configure(Path(data_dir).parent / "security" if Path(data_dir).name == "live"
                           else Path(data_dir) / "security")
    accounts.install_admin_api(server)

    from . import auth
    auth.install(server, accounts.store_dir())

    @server.after_request
    def _headers(resp):
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.headers.get("X-Forwarded-Proto", request.scheme) == "https":
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith(("/_dash-update-component", "/_session", "/_admin")):
            h.setdefault("Cache-Control", "no-store")
        return resp

    @server.post("/_session/beat")
    def _beat():
        body = request.get_json(silent=True) or {}
        tab = str(body.get("tab", ""))[:40]
        if not tab:
            return jsonify(ok=False), 400
        now = time.time()
        ip = client_ip(request)
        from . import accounts
        me = accounts.current()
        with _lock:
            s = _sessions.get(tab)
            if s is None or now - s["last"] > SESSION_GAP:
                s = {"tab": tab, "first": now, "last": now, "pages": 0,
                     "user": (me["username"] if os.environ.get("WEPA_BASIC_AUTH") else "open access") or "signed in",
                     "network": truncate_ip(ip), "where": where(ip),
                     "device": device(request.headers.get("User-Agent", ""))}
                _sessions[tab] = s
                new = True
            else:
                new = False
            s["last"] = now
            s["page"] = str(body.get("path", "/"))[:60]
            s["pages"] = s.get("pages", 0) + 1
            # Keep memory bounded: forget sessions that ended more than the retention period ago.
            if len(_sessions) > 2000:
                for k in sorted(_sessions, key=lambda k: _sessions[k]["last"])[:500]:
                    _sessions.pop(k, None)
        if new:
            log(f"new visit: {s['device']} from {s['where']}", "session")
        if new or s["pages"] % 5 == 0:        # persist now and then, not every minute
            _record("session", **{k: s[k] for k in ("tab", "first", "last", "user", "network", "where", "device",
                                                    "page")})
        return jsonify(ok=True)


# --- summaries for the System page -------------------------------------------------------------------

def sessions_frame():
    """One row per visit (a browser tab's continuous use): who, where, device, started, last seen,
    minutes, active now."""
    import pandas as pd
    now = time.time()
    with _lock:
        rows = [dict(s) for s in _sessions.values()]
    if not rows:
        return pd.DataFrame(columns=["user", "where", "network", "device", "page", "start", "last", "minutes", "active"])
    df = pd.DataFrame(rows)
    df["active"] = now - df["last"] <= ACTIVE_WITHIN
    df["minutes"] = ((df["last"] - df["first"]) / 60).clip(lower=1)
    df["start"] = pd.to_datetime(df["first"], unit="s", utc=True)
    df["last"] = pd.to_datetime(df["last"], unit="s", utc=True)
    df = df[df["last"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=RETENTION_DAYS)]
    return df.sort_values("last", ascending=False).reset_index(drop=True)


def events_frame(kinds=("login_failed", "lockout", "audit")):
    import pandas as pd
    with _lock:
        rows = [{"ts": t, "kind": k, **d} for t, k, d in _events if k in kinds]
    if not rows:
        return pd.DataFrame(columns=["ts", "kind"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df.sort_values("ts", ascending=False).reset_index(drop=True)


def login_enabled() -> bool:
    return bool(os.environ.get("WEPA_BASIC_AUTH"))


def locked_networks() -> list[tuple[str, float]]:
    now = time.time()
    return [(n, (u - now) / 60) for n, u in _locked_until.items() if u > now]
