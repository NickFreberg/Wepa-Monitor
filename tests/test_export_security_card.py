"""Export formats, sign-in protection and visit records, and the station report card."""
from __future__ import annotations

import base64
import io
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from werkzeug.security import generate_password_hash

from wepa_monitor import export, metrics as M, report_card, security

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    d = tmp_path_factory.mktemp("live")
    shutil.copytree(FIXTURE, d, dirs_exist_ok=True)
    return M.load(d, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))


@pytest.mark.parametrize("fmt", list(export.FORMATS))
def test_every_format(ds, fmt):
    for what in export.DATASETS:
        data, name, mime = export.build(ds, what, fmt, None, "BSU print stations", "all")
        assert data and name.startswith("bsu-print-") and mime
    data, name, _ = export.build(ds, "everything", fmt, None, "BSU print stations", "all")
    if fmt == "json":
        doc = json.loads(data)
        assert doc["about"]["stations"] == "BSU print stations" and len(doc["tables"]) == 6
    elif fmt == "csv":
        assert len(zipfile.ZipFile(io.BytesIO(data)).namelist()) == 7
    elif fmt == "xlsx":
        assert len(pd.ExcelFile(io.BytesIO(data)).sheet_names) == 7
    else:
        assert data[:4] == b"%PDF"


def test_report_card_needs_three_days(ds):
    card = report_card.build(ds, *M.window(ds, None))
    assert (card["grade"] == "—").all()            # minutes of data: no grades yet, rather than noisy ones


def test_grades_and_reasons():
    assert report_card.grade_of(91)[0] == "A" and report_card.grade_of(59)[0] == "F"


def _auth(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_failed_sign_ins_are_recorded_and_blocked(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:" + generate_password_hash("a long test password"))
    monkeypatch.setattr(security, "_failures", {})
    monkeypatch.setattr(security, "_locked_until", {})
    from wepa_monitor.dashboard.app import create_app
    c = create_app(tmp_path).server.test_client()
    assert c.get("/").status_code == 401                       # no credentials yet: not a failure
    for i in range(security.MAX_FAILURES):
        monkeypatch.setattr(security.time, "time", lambda i=i: 1_000_000 + i * 10)
        c.get("/", headers=_auth(f"guess{i}", "nope"), environ_base={"REMOTE_ADDR": "203.0.113.9"})
    fails = security.events_frame()
    assert (fails["kind"] == "login_failed").sum() >= security.MAX_FAILURES
    assert "nope" not in fails.to_string()                     # passwords never recorded
    assert (fails["network"] == "203.0.113.x").any()
    r = c.get("/", headers=_auth("bsuresnet", "a long test password"), environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert r.status_code == 429                                # the network is blocked for a while
    ok = c.get("/", headers=_auth("bsuresnet", "a long test password"), environ_base={"REMOTE_ADDR": "198.51.100.4"})
    assert ok.status_code == 200 and ok.headers["X-Content-Type-Options"] == "nosniff"


def test_visits_are_anonymized(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.delenv("WEPA_BASIC_AUTH", raising=False)
    from wepa_monitor.dashboard.app import create_app
    c = create_app(tmp_path).server.test_client()
    r = c.post("/_session/beat", json={"tab": "abc123", "path": "/stations"},
               headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1",
                        "X-Forwarded-For": "8.8.4.77"})
    assert r.status_code == 200
    s = security.sessions_frame()
    row = s[s["tab"] == "abc123"].iloc[0]
    assert row["network"] == "8.8.4.x" and row["device"] == "iPhone · Safari" and row["where"] == "Off campus"
