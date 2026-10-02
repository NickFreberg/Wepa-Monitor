"""Every page must render from the first few minutes of real data (the state right after deploying).

Fixture: the first snapshots collected from the live Wepa page on 2026-10-02.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wepa_monitor import ask, metrics, narrative as N
from wepa_monitor.dashboard.views import (activity_log, analytics, executive, insights_view, overview, rounds,
                                          station, stations)

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    d = tmp_path_factory.mktemp("live")
    shutil.copytree(FIXTURE, d, dirs_exist_ok=True)
    return metrics.load(d, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))


def test_loads(ds):
    assert not ds.empty and len(ds.stations) == 31


def test_pages_render(ds):
    sid = ds.stations["station_id"].iloc[0]
    overview.render(ds, "light", None, None)
    stations.render(ds, None, None)
    station.render(ds, "light", sid, "30")
    for tab in ("reliability", "faults", "consumables", "stats", "quality"):
        analytics.render(ds, "light", None, None, "30", tab)
    executive.render(ds, "light", executive.default_month(ds), None, None)
    activity_log.render(ds, None, None, "30", ["Status", "Parts", "Monitoring"], "")
    for team in ("ResNet", "IT Service Center"):
        rounds.render(ds, "light", team, None, "walk", ["red", "yellow", "tray", "consumable_now"], "urgent", "loop")


@pytest.mark.parametrize("key", [k for k, _ in N.PERIOD_KEYS])
def test_stories(ds, key):
    insights_view.render_story(ds, key, None, None)


def test_questions(ds):
    for q in ask.EXAMPLES:
        assert N.to_text(ask.answer(ds, q).paragraphs).strip()
