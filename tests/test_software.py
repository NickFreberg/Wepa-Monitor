"""Vulnerability monitoring, versions and the change log, update packages, backup collectors, self-check."""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from conftest import login
from werkzeug.security import generate_password_hash

from wepa_monitor import __version__, accounts, config, activity, metrics, peer, security, selfcheck, store, sysevents
from wepa_monitor import updates, vulns
from wepa_monitor.scrape import ScrapeResult

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"
KEY = b"k" * 40


# --- OSV, faked -----------------------------------------------------------------------------------------

class _Resp:
    def __init__(self, obj, status=200):
        self._obj, self.status_code, self.ok = obj, status, status < 400

    def json(self):
        return self._obj

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


RECORDS = {
    "PYSEC-2019-217": {"id": "PYSEC-2019-217", "aliases": ["CVE-2019-10906", "GHSA-462w-v97r-4m45"],
                       "details": "In Pallets Jinja before 2.10.1, str.format_map allows a sandbox escape.",
                       "affected": [{"package": {"name": "jinja2", "ecosystem": "PyPI"},
                                     "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.10.1"}]}]}],
                       "published": "2019-04-07T00:29:00Z", "references": [{"type": "FIX", "url": "https://github.com/pallets/jinja/commit/a2a6c93"}]},
    "GHSA-462w-v97r-4m45": {"id": "GHSA-462w-v97r-4m45", "aliases": ["CVE-2019-10906", "PYSEC-2019-217"],
                            "summary": "Jinja2 sandbox escape via string formatting",
                            "database_specific": {"severity": "HIGH"},
                            "affected": [{"package": {"name": "Jinja2", "ecosystem": "PyPI"},
                                          "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.10.1"}]}]}],
                            "published": "2019-04-10T14:30:00Z"},
    "GHSA-pip0-0000-0000": {"id": "GHSA-pip0-0000-0000", "summary": "pip thing", "severity":
                            [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N"}],
                            "affected": [{"package": {"name": "pip", "ecosystem": "PyPI"},
                                          "ranges": [{"type": "ECOSYSTEM", "events": [{"fixed": "25.3"}]}]}]},
}


class FakeOSV:
    def __init__(self, vulnerable=("jinja2", "pip")):
        self.vulnerable = set(vulnerable)
        self.queries = []

    def post(self, url, json=None, timeout=None):
        self.queries.append(json)
        hits = {"jinja2": ["PYSEC-2019-217", "GHSA-462w-v97r-4m45"], "pip": ["GHSA-pip0-0000-0000"]}
        return _Resp({"results": [{"vulns": [{"id": i} for i in hits.get(q["package"]["name"], [])]}
                                  if q["package"]["name"] in self.vulnerable else {} for q in json["queries"]]})

    def get(self, url, timeout=None):
        return _Resp(RECORDS[url.rsplit("/", 1)[-1]])


PKGS = {"jinja2": "2.10", "pip": "24.0", "requests": "2.32.0"}


def test_cvss_base_scores_match_the_published_calculator():
    assert vulns._cvss_base("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert vulns._cvss_base("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N") == 6.1
    assert vulns._cvss_base("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N") == 5.5
    assert vulns._cvss_base("not a vector") is None


def test_scan_groups_aliases_and_explains_in_plain_english():
    osv = FakeOSV()
    fs = vulns.scan(PKGS, session=osv)
    assert [q["package"]["name"] for q in osv.queries[0]["queries"]] == sorted(PKGS)
    assert "version" in osv.queries[0]["queries"][0] and set(osv.queries[0]["queries"][0]) == {"package", "version"}
    jinja = next(f for f in fs if f.package == "jinja2")
    assert jinja.ids[0] == "CVE-2019-10906" and len(jinja.ids) == 3            # one finding, not two
    assert jinja.severity == "High" and jinja.fixed == "2.10.1"
    assert jinja.summary == "Jinja2 sandbox escape via string formatting."
    urls = [lk["url"] for lk in jinja.links]
    assert "https://nvd.nist.gov/vuln/detail/CVE-2019-10906" in urls
    assert "https://github.com/advisories/GHSA-462w-v97r-4m45" in urls
    assert all(u.startswith("https://") for u in urls)
    pip = next(f for f in fs if f.package == "pip")
    assert pip.tooling and pip.severity == "Moderate"                          # rated from its CVSS vector
    assert fs[0].package == "jinja2"                                           # worst first


def test_refresh_caches_logs_new_and_fixed_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(vulns, "installed", lambda: dict(PKGS))
    vulns.refresh(tmp_path, session=FakeOSV(vulnerable=()))
    assert vulns.load(tmp_path)["findings"] == [] and not sysevents.load(tmp_path)
    vulns.refresh(tmp_path, force=True, session=FakeOSV())               # too soon after the last check
    assert vulns.load(tmp_path)["findings"] == []
    c = vulns.load(tmp_path)
    c["checked_at"] -= 3600
    vulns._write(tmp_path, c)
    vulns.refresh(tmp_path, force=True, session=FakeOSV())
    kinds = [e["kind"] for e in sysevents.load(tmp_path)]
    assert kinds.count("vuln_found") == 2
    found = next(e for e in sysevents.load(tmp_path) if "CVE-2019-10906" in e["title"])
    assert found["href"] == "/software#CVE-2019-10906" and found["severity"] == "critical"
    c = vulns.load(tmp_path)
    c["checked_at"] -= 2 * vulns.MAX_AGE_S
    vulns._write(tmp_path, c)
    vulns.refresh(tmp_path, session=FakeOSV(vulnerable=("pip",)))
    assert any(e["kind"] == "vuln_resolved" and "CVE-2019-10906" in e["title"] for e in sysevents.load(tmp_path))


def test_a_failed_lookup_keeps_the_last_good_result(tmp_path, monkeypatch):
    monkeypatch.setattr(vulns, "installed", lambda: dict(PKGS))
    vulns.refresh(tmp_path, session=FakeOSV())

    class Down:
        def post(self, *a, **k):
            raise ConnectionError("no route")
    c = vulns.load(tmp_path)
    c["checked_at"] -= 2 * vulns.MAX_AGE_S
    vulns._write(tmp_path, c)
    out = vulns.refresh(tmp_path, session=Down())
    assert "ConnectionError" in out["error"] and len(out["findings"]) == 2


# --- versions, change log, update packages -----------------------------------------------------------

def test_changelog_has_the_running_version_and_parses():
    rel = updates.changelog()
    assert rel[0].version == __version__ and updates.release(__version__) is not None
    assert all(r.date and r.summary for r in rel)
    assert [r.version for r in rel] == sorted((r.version for r in rel), key=updates.version_key, reverse=True)
    assert any(rel[0].sections.values())                                  # at least one list of changes


def test_security_ids_are_extracted(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text("# x\n\n## [1.0.1] - 2026-10-05\nFix.\n\n### Security\n- Updated a to fix "
                 "([CVE-2025-27516](https://nvd.nist.gov/vuln/detail/CVE-2025-27516), GHSA-cpwx-vrp4-4pq7)\n")
    r = updates.changelog(p)[0]
    assert r.security_ids == ["CVE-2025-27516", "GHSA-cpwx-vrp4-4pq7"] and r.summary == "Fix."


def test_new_version_is_recorded_once_and_announced(tmp_path, monkeypatch):
    assert updates.record_running(tmp_path)["version"] == __version__
    assert updates.record_running(tmp_path) is None                      # same version: nothing new
    ev = sysevents.load(tmp_path)
    assert len(ev) == 1 and ev[0]["href"] == f"/changelog/{__version__}"
    monkeypatch.setattr(updates, "__version__", "9.9.9")
    updates.record_running(tmp_path)
    ev = sysevents.load(tmp_path)[-1]
    assert ev["title"] == f"Software updated to version 9.9.9 (from {__version__})" and ev["href"] == "/changelog/9.9.9"


def _with_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(vulns, "installed", lambda: dict(PKGS))
    vulns.refresh(tmp_path, session=FakeOSV())


def test_update_package_plan_prepare_and_start(tmp_path, monkeypatch):
    _with_findings(tmp_path, monkeypatch)
    p = updates.plan(tmp_path)
    assert p["spec"] == "jinja2==2.10.1"                                   # pip is an install tool: not in the package
    assert [e["package"] for e in p["tooling"]] == ["pip"]
    row = updates.prepare(tmp_path, by="Admin")
    assert row["ref"] == "UPD000000001" and row["state"] == "Prepared"
    monkeypatch.delenv("WEPA_GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="WEPA_GITHUB_TOKEN"):
        updates.start(tmp_path, row["ref"], by="Admin")

    sent = {}

    class GH:
        def post(self, url, headers=None, json=None, timeout=None):
            sent.update(url=url, headers=headers, json=json)
            return _Resp({}, 204)
    monkeypatch.setenv("WEPA_GITHUB_TOKEN", "ghp_test")
    out = updates.start(tmp_path, row["ref"], by="Admin", session=GH())
    assert sent["url"].endswith("/actions/workflows/update.yml/dispatches")
    assert sent["json"]["inputs"] == {"packages": "jinja2==2.10.1", "ticket": "UPD000000001", "requested_by": "Admin"}
    assert out["state"] == "Requested" and updates.packages(tmp_path)[0]["state"] == "Requested"
    with pytest.raises(ValueError, match="already"):
        updates.start(tmp_path, row["ref"], by="Admin", session=GH())
    kinds = [e["kind"] for e in sysevents.load(tmp_path)]
    assert "update_prepared" in kinds and "update_requested" in kinds


def test_prepare_update_script(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("prep", Path(__file__).parent.parent / "scripts" / "prepare_update.py")
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    root = Path(__file__).parent.parent
    (tmp_path / "wepa_monitor").mkdir()
    for f in ("requirements.txt", "requirements.lock", "CHANGELOG.md"):
        shutil.copy(root / f, tmp_path / f)
    (tmp_path / "wepa_monitor" / "__init__.py").write_text('__version__ = "1.4.2"\n')
    lock = (tmp_path / "requirements.lock").read_text()
    jinja_now = prep.locked_versions(lock)["jinja2"]
    monkeypatch.setattr(prep.vulns, "scan", lambda pk, **k: vulns.scan({"jinja2": "2.10"}, session=FakeOSV()))
    target = ".".join(str(int(x) + (1 if i == 2 else 0)) for i, x in enumerate(jinja_now.split(".")[:3]))
    assert prep.main([f"jinja2=={target}", "--ticket", "UPD000000007", "--by", "Admin", "--no-lock",
                      "--root", str(tmp_path)]) == 0
    assert f"jinja2>={target}" in (tmp_path / "requirements.txt").read_text()
    assert '__version__ = "1.4.3"' in (tmp_path / "wepa_monitor" / "__init__.py").read_text()
    top = updates.changelog(tmp_path / "CHANGELOG.md")[0]
    assert top.version == "1.4.3" and "UPD000000007" in " ".join(top.sections["Sources"])
    assert "pandas>=" in (tmp_path / "requirements.txt").read_text()
    with pytest.raises(SystemExit):
        prep.main(["jinja2==0.1", "--no-lock", "--root", str(tmp_path)])     # never a downgrade
    with pytest.raises(SystemExit):
        prep.main(["jinja2; rm -rf /", "--no-lock", "--root", str(tmp_path)])
    assert prep.raise_minimum("dash>=2.18,<5\n", "dash", "4.1.0", "x") == "dash>=4.1.0,<5\n"


# --- the app: pages, feed, admin-only actions ---------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:" + generate_password_hash("a long admin password"))
    monkeypatch.setenv("WEPA_PEER_KEY", KEY.decode())
    monkeypatch.setenv("WEPA_VULN_CHECK", "0")
    for name in ("_failures", "_locked_until"):
        monkeypatch.setattr(security, name, {})
    monkeypatch.setattr(accounts, "_fail", {})
    monkeypatch.setattr(accounts, "_locked", {})
    peer._seen_nonces.clear()
    from wepa_monitor.dashboard.app import create_app
    a = create_app(tmp_path)
    a.server.config["DATA_DIR"] = tmp_path
    return a


def _dash(client, output, inputs, state=None, trigger=None):
    outs = [{"id": o.split(".")[0], "property": o.split(".")[1]} for o in output.strip(".").split("...")]
    return client.post("/_dash-update-component", json={
        "output": output, "outputs": outs[0] if len(outs) == 1 else outs,
        "inputs": inputs, "state": state or [], "changedPropIds": [trigger] if trigger else []})


def test_pages_and_feed(app):
    c = app.server.test_client()
    login(c, "bsuresnet", "a long admin password")
    for path in ("/software", "/changelog", f"/changelog/{__version__}", "/changelog/0.0.0"):
        assert c.get(path).status_code == 200
    ds = metrics.load(app.server.config["DATA_DIR"])
    ev = activity.events(ds)
    row = ev[ev["kind"] == "software_updated"].iloc[0]
    assert row["href"] == f"/changelog/{__version__}" and activity.KIND_GROUP[row["kind"]] == "System"
    r = _dash(c, "sw-body.children", [{"id": "sw-tick", "property": "n_intervals", "value": 1},
                                       {"id": "sw-msg", "property": "children", "value": None}])
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Self-check" in body and "Investigation audit trail" in body


def test_only_the_administrator_can_start_updates_and_needs_the_password_again(app, monkeypatch):
    data_dir = app.server.config["DATA_DIR"]
    accounts.create("jsmith", "Jordan Smith", "maple river lantern 42", by="t", email="j@bridgew.edu", temporary=False)
    from wepa_monitor.dashboard.views import software
    ds = metrics.load(data_dir)
    with app.server.test_request_context("/"):
        from flask import g
        g.wepa_user = {"username": "jsmith", "role": "staff", "name": "Jordan Smith"}
        msg, tone = software.act(ds, "sw-prepare", None)
        assert tone == "err" and "Only the administrator" in msg
        g.wepa_user = {"username": "bsuresnet", "role": "admin", "name": "Administrator"}
        monkeypatch.setenv("WEPA_VULN_CHECK", "1")
        _with_findings(data_dir, monkeypatch)
        msg, tone = software.act(ds, "sw-prepare", None)
        assert tone == "ok" and "UPD000000001" in msg
        msg, tone = software.act(ds, "sw-start", "not the password")
        assert tone == "err" and "isn't the administrator password" in msg
        assert updates.packages(data_dir)[0]["state"] == "Prepared"


# --- backup collectors ----------------------------------------------------------------------------------

class FlaskTransport:
    """requests-like session that calls the app in-process."""
    def __init__(self, client):
        self.c = client
        self.down = False

    def request(self, method, url, data=None, headers=None, timeout=None, allow_redirects=False):
        if self.down:
            raise ConnectionError("down")
        path = url.split("://", 1)[1].split("/", 1)[1]
        r = self.c.open("/" + path, method=method, data=data, headers=headers)

        class R:
            status_code, content, headers = r.status_code, r.data, r.headers
        return R()


def _scrape_into(d, n=3):
    now = datetime.now(timezone.utc)
    store.append_result(d, ScrapeResult(attempt_ts=now, ok=True, http_status=200, duration_ms=500, page_ts=now,
                                        records=[{"section": "ResNet", "station_id": f"0090{i}", "description": f"Hall {i}",
                                                  "row_status": "ok", "status_codes": "", "printer_text": "",
                                                  **{c: 80 for c in config.COMPONENTS}}
                                                 for i in range(n)]))


def test_signing_refuses_tampering_old_messages_and_replays():
    h = peer.sign(KEY, "POST", "/_peer/event", b'{"a":1}')
    assert peer.verify(KEY, "POST", "/_peer/event", h, b'{"a":1}')
    assert not peer.verify(KEY, "POST", "/_peer/event", h, b'{"a":1}')                    # replay
    h = peer.sign(KEY, "POST", "/_peer/event", b'{"a":1}')
    assert not peer.verify(KEY, "POST", "/_peer/event", h, b'{"a":2}')                    # body changed
    assert not peer.verify(KEY, "POST", "/_peer/upload", h, b'{"a":1}')                   # other path
    assert not peer.verify(b"x" * 40, "POST", "/_peer/event", h, b'{"a":1}')              # wrong key
    old = {**h, "X-Wepa-Peer-Ts": str(int(time.time()) - 600)}
    assert not peer.verify(KEY, "POST", "/_peer/event", old, b'{"a":1}')


def test_peer_api_is_off_without_a_key_and_refuses_unsigned(app, monkeypatch):
    c = app.server.test_client()
    assert c.get("/_peer/status", headers={"X-Wepa-Peer-Id": "x"}).status_code == 401
    monkeypatch.delenv("WEPA_PEER_KEY")
    assert c.get("/_peer/status").status_code == 404


def test_failover_dashboard_upload_handback_within_a_minute(app, tmp_path):
    primary = app.server.config["DATA_DIR"]
    t = FlaskTransport(app.server.test_client())
    bdir = tmp_path / "backupdata"
    scrapes = []
    b = peer.Backup("https://primary.example", KEY, bdir, "mac", session=t, self_update=False, log=lambda m: None,
                    scrape=lambda d: (scrapes.append(1), _scrape_into(d)))
    t0 = time.time()
    # The fixture's own readings are old: the main collector is silent.
    assert b.step(t0) == "standby" and not scrapes
    assert b.step(t0 + 30) == "standby"
    assert b.step(t0 + 61) == "active" and not scrapes                    # took over after 60 s
    assert b.step(t0 + 62) == "active" and len(scrapes) == 1                # collects, sends to the dashboard
    imported = list((primary / "imports" / "snapshots").glob("*.backupmac.parquet"))
    assert imported and len(pd.read_parquet(imported[0])) == 3
    assert any(e["kind"] == "backup_active" for e in sysevents.load(primary))
    assert peer.peers(primary)["mac"]["state"] == "active"
    # The main collector reads the page again: the next check stands the backup down.
    _scrape_into(primary)
    t1 = time.time()
    assert b.step(t1) == "standby"
    assert time.time() - t1 < 60
    n = len(scrapes)
    b.step(t1 + 120)
    assert len(scrapes) == n                                                # no longer collecting
    ev = [e for e in sysevents.load(primary) if e["kind"] == "backup_handover"]
    assert ev and "backup 'mac' stood down" in ev[0]["title"]
    assert peer.peers(primary)["mac"]["state"] == "standby" and peer.peers(primary)["mac"]["last_cover"]
    # The dashboard sees the backup's readings (merged and de-duplicated with its own).
    ds = metrics.load(primary)
    assert {"00900", "00901", "00902"} <= set(ds.stations["station_id"])


def test_backup_takes_over_when_the_main_app_is_unreachable_and_sends_later(app, tmp_path):
    primary = app.server.config["DATA_DIR"]
    t = FlaskTransport(app.server.test_client())
    t.down = True
    b = peer.Backup("https://primary.example", KEY, tmp_path / "b", "pi", session=t, self_update=False,
                    log=lambda m: None, scrape=_scrape_into)
    t0 = time.time()
    b.step(t0)
    assert b.step(t0 + 61) == "active"
    b.step(t0 + 62)
    assert not (primary / "imports").exists() or not list((primary / "imports").rglob("*backuppi*"))
    t.down = False
    _scrape_into(primary)
    assert b.step(time.time()) == "standby"
    assert list((primary / "imports" / "snapshots").glob("*.backuppi.parquet"))


def test_forged_status_from_an_impostor_is_ignored(app, tmp_path):
    """An attacker who can answer for the main app, but doesn't know the key, can't make the backup stand down."""
    class Impostor:
        def request(self, method, url, data=None, headers=None, **k):
            class R:
                status_code = 200
                content = json.dumps({"collector_live": True, "version": "99.0.0"}).encode()
                headers = {"X-Wepa-Peer-Nonce": headers["X-Wepa-Peer-Nonce"], "X-Wepa-Peer-Ts": str(int(time.time())),
                           "X-Wepa-Peer-Sig": "0" * 64}
            return R()
    b = peer.Backup("https://primary.example", KEY, tmp_path, "x", session=Impostor(), self_update=True,
                    log=lambda m: None, scrape=lambda d: None)
    assert b.check() is None
    t0 = time.time()
    b.step(t0)
    assert b.step(t0 + 61) == "active"


def test_backup_updates_itself_to_the_main_version_only_when_newer(tmp_path, monkeypatch):
    b = peer.Backup("https://p", KEY, tmp_path, "x", session=object(), log=lambda m: None)
    ran, restarted = [], []
    monkeypatch.setattr(b, "_update_code", lambda commit: ran.append(commit) or True)
    monkeypatch.setattr(b, "_restart", lambda: restarted.append(1))
    monkeypatch.setattr(b, "_call", lambda *a, **k: {})
    assert not b.maybe_update({"version": __version__})
    assert not b.maybe_update({"version": "0.0.1"})
    assert not b.maybe_update({"version": "1.0; rm -rf /"})
    assert b.maybe_update({"version": "99.0.0", "commit": "abc1234"}) and ran == ["abc1234"] and restarted
    b.state = "active"
    assert not b.maybe_update({"version": "100.0.0"})                     # never mid-failover


def test_upload_rejects_bad_input(app):
    c = app.server.test_client()

    def post(obj, bid="mac"):
        body = json.dumps(obj).encode()
        return c.post("/_peer/upload", data=body, headers={**peer.sign(KEY, "POST", "/_peer/upload", body),
                                                           "X-Wepa-Peer-Id": bid})
    assert post({"kind": "../../etc", "day": "2026-10-04", "rows": []}).status_code == 400
    assert post({"kind": "snapshots", "day": "../x", "rows": []}).status_code == 400
    assert post({"kind": "snapshots", "day": "2026-10-04", "rows": [{"x": 1}]}).status_code == 400
    assert post({"kind": "snapshots", "day": "2026-10-04", "rows": []}, bid="../evil").status_code == 400


# --- self-check -----------------------------------------------------------------------------------------

def test_selfcheck_reports_facts_and_catches_tampering(app, monkeypatch):
    from wepa_monitor import investigations as INV
    data_dir = app.server.config["DATA_DIR"]
    ds = metrics.load(data_dir)
    checks = {c.name: c for c in selfcheck.run(ds)}
    assert checks["Sign-in"].status == "ok"
    assert checks["Investigation audit trail"].status == "ok"
    assert checks["Version"].status == "ok" and __version__ in checks["Version"].detail
    assert checks["Backup collectors"].status == "warn"                       # key set, nobody checked in
    accounts.create("alee", "Alex Lee", "maple river lantern 42", by="t", email="a@bridgew.edu", temporary=False)
    INV.create(data_dir, {"username": "alee", "name": "Alex Lee", "role": "staff", "can_edit": True}, ds.stations["station_id"].iloc[0], "Test", "A test")
    p = INV._path(data_dir)
    p.write_text(p.read_text().replace("Test", "Edited"))
    checks = {c.name: c for c in selfcheck.run(ds)}
    assert checks["Investigation audit trail"].status == "fail"
    tone, sentence = selfcheck.summary(list(checks.values()))
    assert tone == "critical" and "investigation audit trail" in sentence


def test_assistant_self_check_tool(app):
    from wepa_monitor import analyst
    ds = metrics.load(app.server.config["DATA_DIR"])
    tk = analyst.Toolkit(ds)
    out = tk.run("self_check", {})
    assert out.startswith("Self-check:") and "Version" in out and __version__ in out
    from wepa_monitor import ai
    assert "self_check" in ai.SYSTEM_PROMPT and "consciousness" in ai.SYSTEM_PROMPT
