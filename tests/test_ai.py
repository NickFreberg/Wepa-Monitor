"""The AI layer: off by default, grounded facts, cached, and pages fall back cleanly when it fails."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wepa_monitor import ai, metrics
from wepa_monitor.dashboard.views import insights_view

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    d = tmp_path_factory.mktemp("live")
    shutil.copytree(FIXTURE, d, dirs_exist_ok=True)
    return metrics.load(d, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))


def test_off_unless_configured(monkeypatch, ds):
    monkeypatch.delenv("WEPA_AI_PROVIDER", raising=False)
    assert not ai.enabled()
    assert insights_view.render_story_ai(ds, "today", None) is None
    with pytest.raises(ai.AIError):
        ai.ask("hi", "facts")


def test_facts_are_computed_and_honest(ds):
    f = ai.facts(ds)
    assert "Right now:" in f and "Monitoring began" in f and "desk" in f
    assert "Story for" in f


def test_answers_use_ai_and_keep_the_numbers(monkeypatch, ds):
    monkeypatch.setenv("WEPA_AI_PROVIDER", "fake")
    out = insights_view.render_answer(ds, "What's down right now?", None)
    text = str(out)
    assert "ai-note" in text and "The numbers behind it" in text
    assert insights_view.render_story_ai(ds, "today", None) is not None


def test_failures_fall_back_to_rules(monkeypatch, ds):
    monkeypatch.setenv("WEPA_AI_PROVIDER", "fake")

    def boom(*a, **k):
        raise ai.AIError("down")
    monkeypatch.setattr(ai, "ask", boom)
    text = str(insights_view.render_answer(ds, "What's down right now?", None))
    assert "isn't available right now" in text and "ai-note" not in text
    assert insights_view.render_story_ai(ds, "today", None) is None


def test_hourly_spending_guard(monkeypatch):
    monkeypatch.setenv("WEPA_AI_PROVIDER", "fake")
    monkeypatch.setenv("WEPA_AI_MAX_PER_HOUR", "2")
    monkeypatch.setattr(ai, "_calls", __import__("collections").deque())
    ai.ask("a", "x1", cache_key="g1")
    ai.ask("a", "x2", cache_key="g2")
    with pytest.raises(ai.AIError):
        ai.ask("a", "x3", cache_key="g3")
    assert ai.ask("a", "x1", cache_key="g1").text            # cached answers don't count


def test_outcomes_copy_written_by_ai(monkeypatch, ds):
    from wepa_monitor.dashboard.views import outcomes
    monkeypatch.setenv("WEPA_AI_PROVIDER", "fake")
    reply = ai.Reply('{"title": "Printing, Minute by Minute", "subtitle": "A new view of campus printers", '
                     '"paragraphs": ["One.", "Two.", "Three.", "Four."], "pull_quote": "Thirty-one printers."}',
                     "test model", "x", 0.1)
    monkeypatch.setattr(ai, "ask", lambda *a, **k: reply)
    text = str(outcomes.render(ds, "light", "all", None))
    assert "Printing, Minute by Minute" in text and "Thirty-one printers." in text and "drafted by test model" in text

    def boom(*a, **k):
        raise ai.AIError("down")
    monkeypatch.setattr(ai, "ask", boom)
    text = str(outcomes.render(ds, "light", "all", None))
    assert "built-in text" in text                              # falls back cleanly


def test_claude_can_look_things_up(monkeypatch):
    """Claude gets the same read-only lookup tool as Copilot and answers after using it."""
    import types

    import anthropic

    B = types.SimpleNamespace
    calls, asked = [], []

    class Messages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                assert kw["tools"][0]["name"] == "wepa_query"
                return B(stop_reason="tool_use", content=[
                    B(type="tool_use", id="t1", input={"question": "longest outage this week?"})])
            assert kw["messages"][-1]["content"][0]["content"] == "Library: 3 h"
            return B(stop_reason="end_turn", content=[B(type="text", text="The Library was down 3 hours.")])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: B(beta=B(messages=Messages())))
    monkeypatch.setenv("WEPA_AI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    reply = ai.ask("Summarize", "facts", cache_key="claude-tool-test",
                   tool=lambda q: asked.append(q) or "Library: 3 h")
    assert reply.text == "The Library was down 3 hours."
    assert asked == ["longest outage this week?"] and len(calls) == 2
