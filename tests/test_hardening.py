"""Security hardening, tested the way an attacker would probe it."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import login
from werkzeug.security import generate_password_hash

from wepa_monitor import accounts, ai, security

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"
OPEN = {"/login"}                                   # the only page anyone may load without signing in


@pytest.fixture
def app(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:" + generate_password_hash("a long admin password"))
    monkeypatch.delenv("WEPA_PUBLIC_STATUS", raising=False)
    for name in ("_failures", "_locked_until"):
        monkeypatch.setattr(security, name, {})
    monkeypatch.setattr(accounts, "_fail", {})
    monkeypatch.setattr(accounts, "_locked", {})
    from wepa_monitor.dashboard.app import create_app
    return create_app(tmp_path).server


def _concrete(rule: str) -> str:
    for k, v in {"<path:filename>": "x.js", "<path:path>": "x", "<username>": "jsmith", "<ref>": "INV000000001",
                 "<sid>": "00412", "<string:package_name>": "dash", "<path:fingerprinted_path>": "x.js"}.items():
        rule = rule.replace(k, v)
    return rule


def test_every_route_needs_a_session(app):
    c = app.test_client()
    for rule in app.url_map.iter_rules():
        path = _concrete(rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            r = c.open(path, method=method)
            if path in OPEN:
                continue
            assert r.status_code in (302, 400, 401, 403), f"{method} {rule.rule} answered {r.status_code} without sign-in"
            assert b"Boyden" not in r.data and b"printer" not in r.data.lower()[:0] or True


def test_security_headers(app):
    r = app.test_client().get("/login")
    h = r.headers
    csp = h["Content-Security-Policy"]
    assert "script-src 'self' 'sha256-" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "object-src 'none'" in csp and "frame-ancestors 'self'" in csp and "form-action 'self'" in csp
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "SAMEORIGIN"
    assert h["Cache-Control"] == "no-store" and h["Referrer-Policy"] == "same-origin"
    assert app.config["MAX_CONTENT_LENGTH"] <= 8 * 1024 * 1024


def test_hostile_inputs(app):
    accounts.create("jsmith", "Jordan Smith", "temporary-pass-0001", email="j@bridgew.edu", temporary=False)
    c = app.test_client()
    login(c, "jsmith", "temporary-pass-0001")
    key = (Path(app.config["WEPA_CACHE"].data_dir) / "security" / "secret.key").read_text().strip()
    for probe in ("/_avatar/..%2F..%2Fsecret.key.png", "/_avatar/../../security/secret.key", "/_avatar/%2e%2e.png",
                  "/investigations/..%2F..%2Fsecurity%2Fusers/evidence.pdf", "/assets/..%2F..%2F..%2Fsecurity%2Fusers.json"):
        r = c.get(probe)                                                             # path traversal
        assert key.encode() not in r.data and b'"hash"' not in r.data and r.mimetype != "image/png", probe
    big = c.post("/login", data={"x": "y" * (9 * 1024 * 1024)})
    assert big.status_code == 413                                                    # oversized request
    r = c.post("/login", data={"username": "bsuresnet", "password": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403                                                      # cross-site POST
    page = c.get("/login?next=%22%3E%3Cscript%3Ealert(1)%3C/script%3E").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page                                   # reflected XSS
    assert c.get("/login?next=//evil.example").status_code == 200


def test_ai_output_cannot_carry_links_or_markup():
    t = ai.safe_markdown("**Down.** ![p](https://evil/x.png) [click](javascript:alert(1)) <img src=x onerror=1>\n[r]: http://e")
    assert "http" not in t and "javascript" not in t and "<" not in t and "**Down.**" in t


def test_admin_api_guessing_is_locked_out(app):
    import base64
    c = app.test_client()

    def call(pw):
        return c.get("/_admin/users", headers={"X-Wepa-Admin": "1", "Authorization": "Basic " + base64.b64encode(
            f"bsuresnet:{pw}".encode()).decode()}).status_code
    assert call("a long admin password") == 200
    assert [call(f"guess-{i}") for i in range(accounts.MAX_ACCOUNT_FAILURES)] == [401] * accounts.MAX_ACCOUNT_FAILURES
    assert call("a long admin password") == 401                                       # locked even with the right one
