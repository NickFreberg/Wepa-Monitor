"""Rounds planner: Dijkstra, stop ordering, and plans on the stored campus network."""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import pytest

from wepa_monitor import reference, routing as R


def toy_graph(edges, n):
    adj = [[] for _ in range(n)]
    meters = {}
    for a, b, w in edges:
        adj[a].append((b, w))
        adj[b].append((a, w))
        meters[(a, b)] = meters[(b, a)] = w
    return R.Graph(np.zeros((n, 2)), adj, meters, np.ones(n, dtype=bool))


def test_dijkstra_shortest_path():
    #   0 --1-- 1 --1-- 2
    #    \______5______/      the direct edge is longer than going through 1
    g = toy_graph([(0, 1, 1.0), (1, 2, 1.0), (0, 2, 5.0), (2, 3, 2.0)], 5)
    dist, prev = R.dijkstra(g, 0)
    assert dist[2] == 2.0 and dist[3] == 4.0 and np.isinf(dist[4])
    assert R.path_to(prev, 3) == [0, 1, 2, 3]


@pytest.mark.parametrize("seed", range(5))
def test_held_karp_matches_brute_force(seed):
    rng = np.random.default_rng(seed)
    pts = rng.random((7, 2))
    cost = np.linalg.norm(pts[:, None] - pts[None], axis=2)
    for back in (True, False):
        best = min(R._tour_cost(cost, 0, list(p), back) for p in itertools.permutations(range(1, 7)))
        got = R._tour_cost(cost, 0, R._held_karp(cost, 0, list(range(1, 7)), back), back)
        assert got == pytest.approx(best)


def test_urgent_first_visits_down_printers_first():
    pts = np.array([[0, 0], [1, 0], [2, 0], [10, 0]], dtype=float)
    cost = np.linalg.norm(pts[:, None] - pts[None], axis=2)
    order = R._order(cost, priority=[1, 1, 0], round_trip=False, urgent_first=True)
    assert order[0] == 3                                       # the far, down printer comes first
    assert R._order(cost, [1, 1, 0], False, urgent_first=False) == [1, 2, 3]


@pytest.fixture(scope="module")
def queue():
    rows = [("02063", "Harrington Hall, Lab (02063)", "Harrington Hall", "red", "Printer down"),
            ("02059", "Moakley Lobby (02059)", "Moakley Center", "red", "Printer down"),
            ("02062", "Maxwell Library (02062)", "Maxwell Library", "consumable_soon", "Drum K at 3%"),
            ("00999", "Flight School (00999)", "New Bedford Flight School", "yellow", "Paper low")]
    q = pd.DataFrame(rows, columns=["station_id", "station", "building", "kind", "issue"])
    return q.assign(fix="Inspect printer")


@pytest.mark.skipif(not R.NETWORK_PATH.exists(), reason="campus network not built")
@pytest.mark.parametrize("mode", ["walk", "van"])
def test_plan_on_campus(queue, mode):
    p = R.plan(queue, reference.load_buildings(), "Maxwell Library", mode)
    assert [s.building for s in p.order][:2] == ["Harrington Hall", "Moakley Center"] or \
        {s.building for s in p.order[:2]} == {"Harrington Hall", "Moakley Center"}     # down printers first
    assert p.skipped == ["New Bedford Flight School"]                                    # off campus
    assert 300 < p.meters < 8000 and p.travel_s > 0
    assert all(np.isfinite(l.seconds) for legs in p.legs for l in legs)
    if mode == "van":
        assert any(l.mode == "drive" for legs in p.legs for l in legs)
    assert R.supplies(p) == ["1 × Drum K"]


@pytest.mark.skipif(not R.NETWORK_PATH.exists(), reason="campus network not built")
def test_parking_list_overrides_osm(tmp_path, monkeypatch):
    f = tmp_path / "parking.csv"
    f.write_text("building,lat,lon\nMoakley Center,41.9901,-70.9655\nHunt Hall,,\n")
    monkeypatch.setattr(R, "PARKING_PATH", f)
    b = reference.load_buildings()
    spots = R.parking_spots(b[b["building"].isin(["Moakley Center", "Hunt Hall"])]).set_index("building")
    assert spots.loc["Moakley Center", "park_source"] == "your parking list"
    assert spots.loc["Moakley Center", "park_lat"] == pytest.approx(41.9901)
    assert spots.loc["Hunt Hall", "park_source"].startswith("nearest lot")
