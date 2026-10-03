"""Student status pages: facts from the latest reading, nearest working printers, QR codes, and the
login staying in place unless the pages are explicitly made public."""
from __future__ import annotations

import shutil
from pathlib import Path

from werkzeug.security import generate_password_hash

from wepa_monitor import security

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


def _client(tmp_path, monkeypatch, public: bool, login: bool = True):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(security, "_failures", {})
    monkeypatch.setattr(security, "_locked_until", {})
    if login:
        monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:" + generate_password_hash("a long test password"))
    else:
        monkeypatch.delenv("WEPA_BASIC_AUTH", raising=False)
    if public:
        monkeypatch.setenv("WEPA_PUBLIC_STATUS", "1")
    else:
        monkeypatch.delenv("WEPA_PUBLIC_STATUS", raising=False)
    from wepa_monitor.dashboard.app import create_app
    app = create_app(tmp_path)
    return app.server.test_client(), app.server.config["WEPA_CACHE"].get()


def test_pages_show_status_and_backups(tmp_path, monkeypatch):
    c, ds = _client(tmp_path, monkeypatch, public=False, login=False)
    sid = ds.stations.iloc[0]["station_id"]
    page = c.get(f"/status/{sid}").get_data(as_text=True)
    assert ds.stations.iloc[0]["description"] in page and "As of" in page
    assert any(w in page for w in ("Working", "Not working", "Status unknown"))
    assert "printers working" in c.get("/status").get_data(as_text=True)
    qr = c.get(f"/status/qr/{sid}.svg")
    assert qr.status_code == 200 and qr.mimetype == "image/svg+xml" and b"<svg" in qr.data
    assert c.get("/status/not-a-printer").status_code == 404


def test_login_stays_unless_made_public(tmp_path, monkeypatch):
    c, ds = _client(tmp_path, monkeypatch, public=False)
    sid = ds.stations.iloc[0]["station_id"]
    assert c.get(f"/status/{sid}").status_code == 302          # sent to sign in


def test_public_switch_opens_student_pages_only(tmp_path, monkeypatch):
    c, ds = _client(tmp_path, monkeypatch, public=True)
    sid = ds.stations.iloc[0]["station_id"]
    assert c.get(f"/status/{sid}").status_code == 200
    assert c.get("/status").status_code == 200
    assert c.get("/status/signs").status_code == 302      # staff printout stays private
    assert c.get("/").status_code == 302                  # the dashboard stays private
