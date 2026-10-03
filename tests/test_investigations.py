"""Investigations: reference numbers, accounts, the governed workflow, the tamper-evident trail,
detection, and the command-line account tool."""
from __future__ import annotations

import base64
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from werkzeug.security import generate_password_hash

from wepa_monitor import accounts, investigations as I, metrics, refs, security

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"
A = {"username": "jsmith", "name": "Jordan Smith", "can_edit": True}
B = {"username": "alee", "name": "Alex Lee", "can_edit": True}


@pytest.fixture()
def ds(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    d = metrics.load(tmp_path, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))
    return d


def _plant(ds, sid, code, n, gap_h=24):
    t0 = ds.data_start + pd.Timedelta(minutes=5)
    sev, flt = [], []
    for k in range(n):
        s = t0 + pd.Timedelta(hours=gap_h * k)
        e = s + pd.Timedelta(minutes=30)
        sev.append({"station_id": sid, "severity": "red", "start": s, "end": e, "status": "resolved",
                    "duration_s": 1800.0, "censored_start": False})
        flt.append({"station_id": sid, "start": s, "end": e, "code": code, "detail": "", "severity": "red",
                    "status": "resolved", "duration_s": 1800.0, "censored_start": False, "label": code})
    ds.sev_inc = pd.concat([ds.sev_inc, pd.DataFrame(sev)], ignore_index=True)
    ds.fault_inc = pd.concat([ds.fault_inc, pd.DataFrame(flt)], ignore_index=True)


def test_reference_numbers_are_permanent_and_by_cause(ds):
    sid = ds.stations["station_id"].iloc[0]
    _plant(ds, sid, "paper_jam", 2)
    _plant(ds, ds.stations["station_id"].iloc[1], "not_reachable", 1)
    _plant(ds, ds.stations["station_id"].iloc[2], "fatal_error", 1)
    r1 = refs.sync(ds)
    red = ds.sev_inc["severity"] == "red"
    assert r1[red].str.match(r"^(OUT|JAM|PAP|SUP|ERR)\d{9}$").all()
    assert (r1[~red] == "").all()                                     # warnings never get a number
    assert {"JAM000000001", "JAM000000002", "OUT000000001", "ERR000000001"} <= set(r1)
    assert (refs.sync(ds) == r1).all()                                 # stable across reloads
    assert refs.prefix_for("paper_out") == refs.prefix_for("tray_missing") == "PAP"
    assert refs.prefix_for("toner_empty") == refs.prefix_for("drum_end") == "SUP"
    assert refs.prefix_for("offline") == "OUT" and refs.prefix_for(None) == "ERR"


def test_governed_workflow_and_tamper_evidence(ds):
    d = ds.data_dir
    ref = I.create(d, A, "00412", "Hardware faults: Scott Hall", "Seen three times.", "ERR")
    assert ref == "INV000000001"
    with pytest.raises(I.InvestigationError, match="Assign"):
        I.transition(d, ref, A, I.ANALYZE)
    I.update(d, ref, A, {"assignee": "jsmith"})
    assert I.transition(d, ref, A, I.ANALYZE)["escalated_at"]
    with pytest.raises(I.InvestigationError, match="root cause"):
        I.transition(d, ref, A, I.RESPOND)
    I.update(d, ref, A, {"root_cause": "Fuser failing", "root_cause_status": "Suspected"})
    I.transition(d, ref, A, I.RESPOND)
    with pytest.raises(I.InvestigationError, match="why"):
        I.update(d, ref, A, {"impact_override": "High"})
    with pytest.raises(I.InvestigationError, match="action taken"):
        I.transition(d, ref, A, I.REVIEW)
    I.update(d, ref, A, {"action_taken": "Opened Wepa case 1", "wepa_case": "1"})
    I.transition(d, ref, A, I.REVIEW)
    with pytest.raises(I.InvestigationError, match="locked"):
        I.update(d, ref, A, {"title": "changed"})
    with pytest.raises(I.InvestigationError, match="Someone other"):
        I.transition(d, ref, A, I.CLOSED_COMPLETE, "done")
    with pytest.raises(I.InvestigationError, match="reason"):
        I.transition(d, ref, B, I.CLOSED_COMPLETE, "")
    r = I.transition(d, ref, B, I.CLOSED_COMPLETE, "Fixed by Wepa", impact_now="Moderate")
    assert r["state"] == I.CLOSED_COMPLETE and r["impact_at_close"] == "Moderate"
    I.add_note(d, ref, A, "Wepa confirmed")                             # notes still allowed when closed
    with pytest.raises(I.InvestigationError, match="named staff"):
        I.add_note(d, ref, {"username": "bsuresnet", "can_edit": False}, "x")
    assert I.verify(d)[0]
    p = I._path(d)
    good = p.read_text()
    p.write_text(good.replace("Fuser failing", "Firmware"))
    assert not I.verify(d)[0]
    lines = good.splitlines()
    p.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    assert not I.verify(d)[0]                                         # a removed change is detected too


def test_archive_after_two_years(ds, monkeypatch):
    d = ds.data_dir
    ref = I.create(d, A, "00412", "x", "y")
    I.transition(d, ref, A, I.CLOSED_CANCELLED, "duplicate")
    real = I.time.time
    monkeypatch.setattr(I.time, "time", lambda: real() + (I.ARCHIVE_AFTER_DAYS + 1) * 86400)
    assert I.get(d, ref)["archived"]
    with pytest.raises(I.InvestigationError, match="Archived"):
        I.add_note(d, ref, A, "late")
    I.archive_due(d)
    assert I.get(d, ref)["events"][-1]["action"] == "archive"


def test_detects_recurring_network_drops_once(ds):
    from wepa_monitor.dashboard.app import attach_refs
    sid = ds.stations["station_id"].iloc[3]
    _plant(ds, sid, "not_reachable", 4, gap_h=6)
    ds.as_of = ds.data_start + pd.Timedelta(days=2)
    attach_refs(ds)                                                   # loading the data runs detection
    made = list(I.table(ds.data_dir)["ref"])
    assert len(made) == 1
    r = I.get(ds.data_dir, made[0])
    assert r["state"] == I.NEW and r["category"] == "OUT" and r["origin"] == "auto" and len(r["linked"]) == 4
    assert "Recurring network" in r["title"] and "OUT000000001" in r["description"]
    assert I.detect(ds, force=True) == []                             # no duplicate while it's open
    imp = I.impact(ds, sid)
    assert imp["level"] == "Moderate" and imp["default"]                # under 90 days of data


def _basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_accounts_admin_api_and_sign_in(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:" + generate_password_hash("admin password 123"))
    monkeypatch.setattr(security, "_failures", {})
    monkeypatch.setattr(security, "_locked_until", {})
    from wepa_monitor.dashboard.app import create_app
    c = create_app(tmp_path).server.test_client()
    adm = {**_basic("bsuresnet", "admin password 123"), "X-Wepa-Admin": "1"}
    body = {"username": "jsmith", "name": "Jordan Smith", "password": "a good long password"}
    assert c.post("/_admin/users", json=body, headers=_basic("bsuresnet", "admin password 123")).status_code == 400
    assert c.post("/_admin/users", json=body, headers=adm).status_code == 201
    assert c.post("/_admin/users", json=body, headers=adm).status_code == 409
    assert c.post("/_admin/users", json={**body, "username": "short", "password": "short"}, headers=adm).status_code == 400
    assert c.get("/", headers=_basic("jsmith", "a good long password")).status_code == 200
    assert c.get("/_admin/users", headers={**_basic("jsmith", "a good long password"), "X-Wepa-Admin": "1"}).status_code == 403
    assert c.post("/_admin/users/jsmith", json={"disabled": True}, headers=adm).status_code == 200
    assert c.get("/", headers=_basic("jsmith", "a good long password")).status_code == 401
    stored = json.loads((tmp_path / "security" / "users.json").read_text())
    assert "a good long password" not in json.dumps(stored) and stored["jsmith"]["hash"].startswith(("scrypt", "pbkdf2"))


def test_cli_local_mode(tmp_path, capsys, monkeypatch):
    from wepa_monitor.__main__ import main
    monkeypatch.delenv("WEPA_BASIC_AUTH", raising=False)
    assert main(["--data-dir", str(tmp_path), "users", "add", "alee", "--name", "Alex Lee", "--local"]) == 0
    out = capsys.readouterr().out
    assert "Created alee" in out and "shown once" in out
    accounts.configure(tmp_path / "security")
    assert accounts.get("alee")["name"] == "Alex Lee"
    assert main(["--data-dir", str(tmp_path), "users", "disable", "alee", "--local"]) == 0
    assert accounts.get("alee")["disabled"]
