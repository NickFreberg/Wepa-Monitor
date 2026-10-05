"""The Rounds map: what the administrator draws, who may change it, and how Rounds uses it."""
from __future__ import annotations

import re

import pandas as pd
import pytest

from wepa_monitor import nearby, roundsmap, routing

needs_network = pytest.mark.skipif(routing.network() is None, reason="campus walking network not built")
HART = (41.989148, -70.966067)
ART = (41.988312, -70.973446)


@pytest.fixture(autouse=True)
def _map(tmp_path):
    roundsmap.configure(tmp_path)
    yield tmp_path
    roundsmap._dir = None


def test_validate_cleans_and_rejects():
    clean = roundsmap.validate({"points": [{"id": "a b<", "kind": "door", "lat": 41.9875, "lon": -70.97,
                                            "name": "<b>Main door</b>", "building": "Boyden Hall", "extra": 1}],
                                "paths": [{"kind": "walk", "coords": [[41.9875, -70.97], [41.988, -70.971]]}]})
    assert clean["points"][0] == {"id": "ab", "kind": "door", "lat": 41.9875, "lon": -70.97, "name": "bMain door/b",
                                  "building": "Boyden Hall"}
    assert clean["paths"][0]["id"] == "l0"
    for bad in ({"points": [{"kind": "helipad", "lat": 41.98, "lon": -70.97}]},
                {"points": [{"kind": "start", "lat": 40.0, "lon": -70.97}]},                 # off campus
                {"paths": [{"kind": "walk", "coords": [[41.98, -70.97]]}]},                   # one point
                {"points": [{"id": "x", "kind": "end", "lat": 41.98, "lon": -70.97}] * 2}):   # duplicate id
        with pytest.raises(roundsmap.MapError):
            roundsmap.validate(bad)


def test_save_load_and_backup(_map):
    roundsmap.save({"points": [{"kind": "start", "lat": 41.9875, "lon": -70.97, "name": "Desk"}]}, "bsuresnet")
    roundsmap.save({"points": [{"kind": "end", "lat": 41.9876, "lon": -70.971, "name": "Office"}]}, "bsuresnet")
    m = roundsmap.load()
    assert [p["name"] for p in m["points"]] == ["Office"] and m["updated_by"] == "bsuresnet"
    assert (_map / "records" / "rounds_map.prev.json").exists()
    assert roundsmap.place_name(m["points"][0]) == "Office"


def _client(monkeypatch, role):
    from flask import Flask, g
    app = Flask(__name__)
    app.secret_key = "test"
    if role:
        monkeypatch.setenv("WEPA_BASIC_AUTH", "bsuresnet:x")

        @app.before_request
        def _who():
            g.wepa_user = {"username": "u", "role": role}
    roundsmap.install(app)
    return app.test_client()


def test_only_the_administrator_can_edit(monkeypatch):
    staff = _client(monkeypatch, "staff")
    assert staff.get("/_rounds/map.json").get_json()["can_edit"] is False
    assert staff.get("/rounds/map").status_code == 403
    assert staff.post("/_rounds/map.json", json={"points": []}).status_code == 403


def test_admin_save_needs_the_page_token(monkeypatch):
    admin = _client(monkeypatch, "admin")
    page = admin.get("/rounds/map").get_data(as_text=True)
    token = re.search(r'"csrf": "([^"]+)"', page).group(1)
    body = {"points": [{"kind": "parking", "lat": 41.9875, "lon": -70.97, "building": "Boyden Hall"}], "paths": []}
    assert admin.post("/_rounds/map.json", json=body).status_code == 400                       # no token
    assert admin.post("/_rounds/map.json", json=body, headers={"X-CSRF-Token": "nope"}).status_code == 400
    r = admin.post("/_rounds/map.json", json=body, headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and r.get_json()["points"][0]["building"] == "Boyden Hall"
    bad = admin.post("/_rounds/map.json", json={"points": [{"kind": "door", "lat": 0, "lon": 0}]},
                     headers={"X-CSRF-Token": token})
    assert bad.status_code == 400 and "outside" in bad.get_json()["error"]


@needs_network
def test_a_drawn_path_shortens_walks():
    before = nearby.walk_seconds("Hart Hall", "Art Center")
    roundsmap.save({"paths": [{"kind": "walk", "coords": [list(HART), list(ART)]}]}, "admin")
    after = nearby.walk_seconds("Hart Hall", "Art Center")
    assert after < before


@needs_network
def test_doors_parking_and_end_points_shape_the_plan():
    door = (41.98760, -70.97440)
    roundsmap.save({"points": [{"kind": "door", "lat": door[0], "lon": door[1], "building": "Boyden Hall"},
                               {"kind": "parking", "lat": 41.98745, "lon": -70.97500, "building": "Boyden Hall"},
                               {"kind": "end", "lat": 41.98620, "lon": -70.96520, "name": "Crimson desk"}]}, "admin")
    from wepa_monitor import reference
    b = reference.load_buildings()
    b = pd.concat([b, pd.DataFrame([{"building": "Crimson desk", "lat": 41.98620, "lon": -70.96520, "campus": "Main"}])])
    q = pd.DataFrame([{"building": "Boyden Hall", "kind": "red", "station_id": "x", "station": "Boyden", "issue": "Jam",
                       "fix": "Clear"}])
    p = routing.plan(q, b, "Maxwell Library", "walk", round_trip=False, end="Crimson desk")
    assert (p.order[0].lat, p.order[0].lon) == door                 # routed to the door, not the middle
    assert p.end == "Crimson desk" and p.back                        # and on to the end point
    park = routing.parking_spots(b[b["building"] == "Boyden Hall"])
    assert park.iloc[0]["park_source"] == "your Rounds map"
