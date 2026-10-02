"""The optional site login (WEPA_BASIC_AUTH) used when the host has no sign-in of its own."""
from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


def _auth(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:" + generate_password_hash("a long test password"))
    from wepa_monitor.dashboard.app import create_app
    return create_app(tmp_path).server.test_client()


def test_requires_login(client):
    r = client.get("/")
    assert r.status_code == 401 and "Basic" in r.headers["WWW-Authenticate"]
    assert client.get("/", headers=_auth("bsuresnet", "wrong")).status_code == 401
    assert client.get("/", headers=_auth("someoneelse", "a long test password")).status_code == 401
    assert client.get("/", headers=_auth("bsuresnet", "a long test password")).status_code == 200
    # Dash's own requests are protected too.
    assert client.get("/_dash-layout").status_code == 401


def test_open_without_setting(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.delenv("WEPA_BASIC_AUTH", raising=False)
    from wepa_monitor.dashboard.app import create_app
    assert create_app(tmp_path).server.test_client().get("/").status_code == 200
