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
                    B(type="tool_use", id="t1", name="wepa_query", input={"question": "longest outage this week?"})])
            assert kw["messages"][-1]["content"][0]["content"] == "Library: 3 h"
            return B(stop_reason="end_turn", content=[B(type="text", text="The Library was down 3 hours.")])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: B(beta=B(messages=Messages())))
    monkeypatch.setenv("WEPA_AI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    reply = ai.ask("Summarize", "facts", cache_key="claude-tool-test",
                   tool=lambda q: asked.append(q) or "Library: 3 h")
    assert reply.text == "The Library was down 3 hours."
    assert asked == ["longest outage this week?"] and len(calls) == 2


def test_assistant_pane_answers_with_and_without_ai(monkeypatch, ds):
    """The header assistant answers from the data; with AI on it carries the conversation along."""
    import json

    from wepa_monitor.dashboard.views import assistant

    monkeypatch.delenv("WEPA_AI_PROVIDER", raising=False)
    m = assistant.reply(ds, "What's down right now?", None, "/", [])
    assert m["role"] == "assistant" and not m["ai"] and m["text"]
    json.dumps(m)                                   # lives in the browser's session store
    assert assistant.render(ds, [], "/")            # welcome screen with suggestions
    assert assistant.render(ds, [{"role": "user", "text": "hi"}, {"role": "pending"}, m], "/")

    seen = []
    monkeypatch.setenv("WEPA_AI_PROVIDER", "fake")
    monkeypatch.setattr(ai, "_fake", lambda message: seen.append(message) or "Two stations are down.")
    history = [{"role": "user", "text": "How was yesterday?"}, {"role": "assistant", "text": "Quiet."}]
    m = assistant.reply(ds, "and today?", None, "/analytics", history)
    assert m["ai"] and m["text"] == "Two stations are down."
    assert "How was yesterday?" in seen[0] and "'/analytics' page" in seen[0]


def test_analyst_tools_run_on_live_data(ds):
    """Every analyst tool answers (or explains why not) without raising, and output is recorded."""
    from wepa_monitor import analyst

    tk = analyst.Toolkit(ds)
    calls = [("data_overview", {}), ("list_stations", {}), ("availability", {"group_by": "station"}),
             ("availability", {"group_by": "hour_of_day", "period": "today"}), ("incidents", {"kind": "outage"}),
             ("incidents", {"kind": "fault", "group_by": "cause"}), ("repair_times", {}), ("supplies", {}),
             ("usage", {"group_by": "building"}), ("report_card", {}), ("compare_periods", {"a": "today", "b": "all"}),
             ("campus_context", {}), ("outage_risk", {}), ("investigations", {}), ("ask_dashboard", {"question": "What's down right now?"}),
             ("availability", {"stations": "no such hall"}), ("nonsense", {})]
    calls += [("statistics", {"kind": k}) for k in ("failure_rates", "warning_to_outage", "recent_changes",
                                                     "coverage_gaps", "staffing_whatif", "by_phase", "downtime_drivers", "shared_outages", "weather_jams", "busy_downtime")]
    sid = ds.stations.iloc[0]["station_id"]
    calls.append(("station_profile", {"station": sid}))
    for name, args in calls:
        out = tk.run(name, args)
        assert isinstance(out, str) and out and " failed (" not in out, (name, out)
    assert "No station, building or area matches" in tk.outputs[15]
    assert len(tk.outputs) == len(calls) and len(tk.specs()) == len(tk.tools)
    s, e, label = analyst.parse_period(ds, "2026-10-01..2026-10-02")
    assert s >= ds.data_start and e <= ds.as_of and label


def test_fact_check_flags_invented_figures():
    src = ["Availability 93.27% (42 printer-hours down); mean repair 710.8 min; Boyden (02144) had 19 outages."]
    assert ai.unsupported("Boyden went down 19 times; availability 93.3%; about 11.8 hours to fix; 42 hours down.",
                          src) == []
    assert ai.unsupported("On Oct 12 at 9 PM in 2026, 3 stations were at 97.5% and 250 hours were lost.",
                          src) == ["97.5", "250"]


def test_unsupported_figures_get_one_rewrite(monkeypatch):
    drafts = iter(["Availability was 97.5% this week.", "Availability was 93.3% this week."])
    monkeypatch.setenv("WEPA_AI_PROVIDER", "fake")
    monkeypatch.setattr(ai, "_fake", lambda message: next(drafts))
    r = ai.ask("How was the week?", "Availability this week: 93.27%.", cache_key="fact-check-rewrite")
    assert r.text == "Availability was 93.3% this week." and r.unverified == ()

    monkeypatch.setattr(ai, "_fake", lambda message: "Availability was 99.1%.")
    r = ai.ask("How was the week?", "Availability this week: 93.27%.", cache_key="fact-check-still-bad")
    assert r.unverified == ("99.1",)
