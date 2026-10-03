"""Sign-in: a real login page and sessions, following NIST SP 800-63B practices (without MFA)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import csrf_of, login
from werkzeug.security import generate_password_hash

from wepa_monitor import accounts, auth, security

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"
ADMIN_PW = "a long test password"


@pytest.fixture
def app(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:" + generate_password_hash(ADMIN_PW))
    monkeypatch.setattr(security, "_failures", {})
    monkeypatch.setattr(security, "_locked_until", {})
    monkeypatch.setattr(accounts, "_fail", {})
    monkeypatch.setattr(accounts, "_locked", {})
    from wepa_monitor.dashboard.app import create_app
    return create_app(tmp_path).server


def _staff(username="jsmith", pw="temporary-pass-0001"):
    return accounts.create(username, "Jordan Smith", pw, email=f"{username}@bridgew.edu")


def test_pages_need_a_session(app):
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login?next=")
    assert c.get("/_dash-layout").status_code == 401                   # Dash's own requests too
    assert c.get("/login").status_code == 200


def test_admin_signs_in_with_safe_cookie(app):
    c = app.test_client()
    bad = login(c, "bsuresnet", "wrong password!!")
    assert bad.status_code == 401 and "match" in bad.get_data(as_text=True)
    r = login(c, "bsuresnet", ADMIN_PW)
    assert r.status_code == 302
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert c.get("/").status_code == 200
    assert c.get("/logout").status_code == 302 and c.get("/").status_code == 302


def test_csrf_origin_and_redirect_protection(app):
    c = app.test_client()
    assert c.post("/login", data={"username": "bsuresnet", "password": ADMIN_PW}).status_code == 400   # no token
    token = csrf_of(c, "/login")
    r = c.post("/login", data={"username": "bsuresnet", "password": ADMIN_PW, "csrf": token},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = login(c, "bsuresnet", ADMIN_PW, next_="https://evil.example/")
    assert r.headers["Location"] == "/"                               # never redirects off-site


def test_temporary_password_must_be_changed_and_policy_applies(app):
    _staff()
    c = app.test_client()
    login(c, "jsmith", "temporary-pass-0001")
    assert c.get("/").headers["Location"] == "/account/password"
    assert c.get("/_dash-layout").status_code == 401
    token = csrf_of(c, "/account/password")

    def change(new, new2=None, cur="temporary-pass-0001"):
        return c.post("/account/password", data={"current": cur, "new": new, "new2": new2 or new, "csrf": token})
    assert "at least 12" in change("short").get_data(as_text=True)
    assert "common" in change("password1234").get_data(as_text=True)
    assert "username, name or email" in change("jsmith-likes-tea").get_data(as_text=True)
    assert "service" in change("my wepa printer pass").get_data(as_text=True)
    assert "match" in change("correct horse battery", "something else").get_data(as_text=True)
    assert "current password" in change("correct horse battery", cur="nope").get_data(as_text=True)
    ok = change("correct horse battery")
    assert ok.status_code == 302 and c.get("/").status_code == 200


def test_password_change_and_deactivation_end_other_sessions(app):
    _staff()
    first, second = app.test_client(), app.test_client()
    login(first, "jsmith", "temporary-pass-0001")
    token = csrf_of(first, "/account/password")
    first.post("/account/password", data={"current": "temporary-pass-0001", "new": "correct horse battery",
                                          "new2": "correct horse battery", "csrf": token})
    login(second, "jsmith", "correct horse battery")
    assert first.get("/").status_code == 200 and second.get("/").status_code == 200
    accounts.admin_update("jsmith", active=False)
    assert first.get("/").status_code == 302 and second.get("/").status_code == 302
    assert login(app.test_client(), "jsmith", "correct horse battery").status_code == 401   # can't sign in either


def test_account_lockout(app):
    _staff()
    c = app.test_client()
    for _ in range(accounts.MAX_ACCOUNT_FAILURES):
        login(c, "jsmith", "wrong wrong wrong")
    r = login(c, "jsmith", "temporary-pass-0001")
    assert r.status_code in (401, 429) and "locked" in r.get_data(as_text=True).lower()


def test_idle_sessions_expire(app, monkeypatch):
    c = app.test_client()
    login(c, "bsuresnet", ADMIN_PW)
    assert c.get("/").status_code == 200
    real = auth.time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + auth.IDLE_S + 5)
    assert c.get("/").status_code == 302


def test_open_without_setting(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.delenv("WEPA_BASIC_AUTH", raising=False)
    from wepa_monitor.dashboard.app import create_app
    assert create_app(tmp_path).server.test_client().get("/").status_code == 200


def test_profile_picture_is_cleaned_and_phone_normalized(tmp_path):
    import io

    from PIL import Image
    accounts.configure(tmp_path / "security")
    accounts.create("alee", "Alex Lee", "temporary-pass-0002", email="alee@bridgew.edu")
    im = Image.new("RGB", (900, 600), (137, 25, 31))
    exif = Image.Exif()
    exif[0x8825] = {2: (41.0, 59.0, 0.0)}                                 # a GPS location
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    u = accounts.set_photo("alee", buf.getvalue())
    saved = Image.open(accounts.photo_path("alee"))
    assert u["photo"] and saved.size == (accounts.PHOTO_SIZE, accounts.PHOTO_SIZE) and saved.format == "PNG"
    assert not saved.getexif() and "exif" not in saved.info
    with pytest.raises(accounts.AccountError):
        accounts.set_photo("alee", b"<svg onload=alert(1)>")                 # not an image we accept
    assert accounts.set_phone("alee", "1-508-531-1000")["phone"] == "(508) 531-1000"
    assert accounts.set_phone("alee", "")["phone"] is None
    with pytest.raises(accounts.AccountError):
        accounts.set_phone("alee", "555-1234")
    with pytest.raises(accounts.AccountError, match="residence hall"):
        accounts.admin_update("alee", resident=True)                          # resident needs a hall
