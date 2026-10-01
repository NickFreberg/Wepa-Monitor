"""Support desk hours, period stories and the rule-based 'Ask the data'."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from wepa_monitor import ask, config, metrics, narrative as N, support, synth

ET = config.LOCAL_TZ


def local(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=ET)


# --- desk hours -------------------------------------------------------------------------------------

def test_owner_mapping():
    assert support.owner("ResNet") == "ResNet"
    assert support.owner("Student Computer Labs") == "IT Service Center"
    assert support.owner("Something new") == config.DEFAULT_OWNER


def test_hours_text():
    assert support.hours_text("ResNet") == "Mon–Thu 10 AM–6 PM, Fri 10 AM–4 PM"
    assert support.hours_text("IT Service Center") == "Mon–Fri 9 AM–4 PM"


@pytest.mark.parametrize("team, when, expected", [
    ("ResNet", "2026-10-01 17:30", True),             # Thursday 5:30 PM
    ("ResNet", "2026-10-02 17:30", False),            # Friday closes at 4
    ("IT Service Center", "2026-10-01 08:59", False),
    ("IT Service Center", "2026-10-01 09:00", True),
    ("IT Service Center", "2026-10-03 12:00", False),  # Saturday
])
def test_is_open(team, when, expected):
    assert support.is_open(team, local(when)) is expected


def test_friday_evening_waits_for_monday():
    fri = local("2026-10-02 17:00")
    assert support.next_open("ResNet", fri) == local("2026-10-05 10:00")
    assert support.desk_status("ResNet", fri) == (False, "Closed until Monday 10 AM")
    # Friday 5 PM to Monday 11 AM: only Monday 10-11 is staffed.
    assert support.staffed_seconds("ResNet", fri, local("2026-10-05 11:00")) == 3600


def test_staffed_seconds_across_dst():
    # Clocks fall back on Sun Nov 1, 2026; Fri 7 h + Mon 7 h + Tue 9-12 = 17 h.
    s = support.staffed_seconds("IT Service Center", local("2026-10-30 08:00"), local("2026-11-03 12:00"))
    assert s == 17 * 3600


def test_closed_dates(monkeypatch):
    monkeypatch.setattr(config, "SUPPORT_CLOSED_DATES", {"2026-10-12"})
    assert not support.is_open("IT Service Center", local("2026-10-12 10:00"))
    assert support.next_open("IT Service Center", local("2026-10-12 10:00")) == local("2026-10-13 09:00")


def test_annotate_splits_downtime():
    stations = pd.DataFrame({"station_id": ["1"], "owner": ["ResNet"]})
    inc = pd.DataFrame({"station_id": ["1"], "start": [local("2026-10-02 15:00").tz_convert("UTC")],
                        "end": [local("2026-10-05 11:00").tz_convert("UTC")], "status": ["resolved"],
                        "duration_s": [68 * 3600.0]})
    out = support.annotate(inc, stations, pd.Timestamp.now(tz="UTC")).iloc[0]
    assert bool(out["in_hours"]) is True and out["wait_s"] == 0
    assert out["staffed_s"] == 2 * 3600          # Fri 3-4 PM + Mon 10-11 AM
    assert out["after_s"] == 66 * 3600


# --- stories and questions on a small synthetic dataset -------------------------------------------------

@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo")
    synth.generate(path, days=21, end=datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc), seed=11,
                   progress=lambda m: None)
    return metrics.load(path)


def test_incidents_carry_desk_columns(ds):
    for col in ("owner", "in_hours", "wait_s", "staffed_s", "after_s"):
        assert col in ds.sev_inc
    red = ds.sev_inc[ds.sev_inc["severity"] == "red"]
    # Waiting for the desk only happens to outages that began after hours.
    assert (red.loc[red["in_hours"].astype(bool), "wait_s"] == 0).all()
    assert set(ds.stations["owner"]) <= set(config.SUPPORT_TEAMS)


@pytest.mark.parametrize("key", [k for k, _ in N.PERIOD_KEYS])
def test_every_period_tells_a_story(ds, key):
    st = N.story(ds, N.period(ds, key))
    text = N.to_text(st.paragraphs)
    assert st.headline and text
    assert "Fleet" not in text and "fleet" not in text


def test_story_counts_outages_that_began_earlier(ds):
    p = N.period(ds, "yesterday")
    inc = N.overlapping(ds, p)
    assert (inc["start"] < p.end).all()
    finish = inc["end"].fillna(ds.as_of)
    assert (finish >= p.start).all()


@pytest.mark.parametrize("question, intent", [
    ("What's down right now?", "now"),
    ("How many outages start after hours?", "support"),
    ("Who supports Weygand?", "support"),
    ("Is the ResNet desk open?", "support"),
    ("Which station was down the longest last week?", "ranking"),
    ("What toner will run out next?", "forecast"),
    ("When do jams happen most?", "when"),
    ("Which buildings ran out of paper the most?", "paper"),
    ("Compare this week to last week", "compare"),
])
def test_intents(question, intent):
    assert ask.intent(" " + question.lower() + " ") == intent


def test_parse_period_and_subject(ds):
    assert ask.parse_period(ds, " last week ").key == "last_week"
    assert ask.parse_period(ds, " past 10 days ").label == "the last 10 days"
    subj = ask.parse_subject(ds, " how is the it service center doing ")
    assert subj.ids and set(ds.stations.set_index("station_id").loc[subj.ids, "owner"]) == {"IT Service Center"}


def test_answers_are_plain_sentences(ds):
    for q in ask.EXAMPLES + ["blorp"]:
        a = ask.answer(ds, q)
        assert a.understood and N.to_text(a.paragraphs).strip()
    assert ask.answer(ds, "blorp").understood.startswith("I didn't recognize")
