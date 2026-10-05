"""The app shell: the grouped menu, the phone tab bar and More menu, page headings, and the IT Outcomes
launch-year briefing."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wepa_monitor import metrics
from wepa_monitor.dashboard import app as A
from wepa_monitor.dashboard.views import outcomes

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


def _text(component) -> str:
    return json.dumps(component.to_plotly_json() if hasattr(component, "to_plotly_json") else component,
                      default=lambda o: o.to_plotly_json() if hasattr(o, "to_plotly_json") else str(o))


def test_menu_is_grouped_and_admin_pages_are_not_in_it():
    groups = [g for g, _ in A.NAV_GROUPS]
    assert groups == ["Operate", "Analyze", "Report", "Manage"]
    hrefs = [h for h, _, _ in A.NAV]
    assert "/inventory" in hrefs and "/system" not in hrefs and "/software" not in hrefs
    menu = _text(A.nav_menu("/analytics"))
    assert all(g in menu for g in groups) and "(current page)" in menu


def test_phone_tab_bar_has_four_pages_and_more_with_everything():
    bar = A.tabbar("/inventory")
    assert len(bar) == 5
    more = _text(bar[-1])
    for href, _, _ in A.NAV + A.ADMIN_NAV:
        assert f'"href": "{href}"' in more
    assert "is-active" in _text(bar[-1].children[0])          # Inventory lives under More, so More is active


def test_every_menu_page_has_a_heading():
    for href, _, _ in A.NAV + A.ADMIN_NAV:
        title, sub = A.page_heading(href)
        assert title != "Page not found" and sub.endswith(".")


@pytest.fixture()
def ds(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    return metrics.load(tmp_path, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))


def test_launch_year_briefing(ds, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WEPA_AI_API_KEY", raising=False)
    s = outcomes._settings()
    p1, p2, p4, p5, extra = outcomes.launch_story(ds, s, "this year", "Printers could print 95% of the time.")
    text = " ".join([p1, p2, p4, p5, *extra])
    began = ds.data_start.tz_convert(outcomes.TZ)
    assert s["mission"] in text and f"{began:%B %-d}" in text
    assert "partial picture" in text and "large language model" in text and "21 days" in text
    facts = "\n".join(outcomes.launch_facts(ds))
    assert "learning" in facts or "live" in facts
    page = _text(outcomes.render(ds, "crimson", "all", None))
    assert "Our mission" in page and "Measure" in page
