"""Where one more printer would help: the candidate sites and the verdict rules."""
from __future__ import annotations

import math

from wepa_monitor import advanced as A


def test_candidates_are_the_four_buildings_without_printers():
    sites = A.candidate_sites()
    assert sorted(sites["building"]) == ["Art Center", "Burnell Hall", "Hart Hall", "Kelly Gymnasium"]
    assert dict(zip(sites["building"], sites["map_no"])) == {"Hart Hall": "28", "Burnell Hall": "29",
                                                             "Art Center": "6", "Kelly Gymnasium": "17"}


def test_verdicts():
    assert A._verdict(396, 2.2, "Moakley Center", 0)[0] == "Strongest case"
    assert A._verdict(396, 1.6, "DMF", 0)[0] == "Hard to justify"            # a printer is already close
    assert A._verdict(0, 4.0, "Moakley Center", 0)[0] == "Hard to justify"   # no classes
    assert A._verdict(0, 4.0, "Moakley Center", 75)[0] == "Worth a look"     # but real backup value
    assert A._verdict(60, 3.0, "Moakley Center", 0)[0] == "Worth a look"
    assert A._verdict(10, math.inf, "", 0)[0] == "Hard to justify"


def test_walk_words_never_round_across_the_threshold():
    assert A.walk_words(0.9) == "under a minute"
    assert A.walk_words(1.6) == "under 2 minutes"
    assert A.walk_words(2.2) == "a 2-minute walk"
    assert A.walk_words(4.0) == "a 4-minute walk"


def test_printer_review_waits_for_a_full_year(monkeypatch, tmp_path):
    # Its own small synthetic history (CI has no demo data folder): three weeks, well short of a year.
    from datetime import datetime, timezone

    from wepa_monitor import metrics as M, synth
    synth.generate(tmp_path, days=21, end=datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc), seed=11,
                   progress=lambda m: None)
    ds = M.load(tmp_path)
    monkeypatch.delenv("WEPA_PRINTER_REVIEW", raising=False)
    st = A.review_status(ds)
    assert not st["ready"] and 0 < st["days"] < A.REVIEW_DAYS
    assert A.printer_review(ds) is None
    monkeypatch.setenv("WEPA_PRINTER_REVIEW", "1")
    r = A.printer_review(ds)
    assert r is not None and len(r["singles"]) and len(r["groups"])
    assert set(r["singles"]["verdict"]) <= {"Keep", "Review"}
    assert set(r["groups"]["verdict"]) <= {"Keep both", "One may be enough"}
    assert (r["groups"]["printers"] >= 2).all()
    # a lightly used printer next to another one is flagged; one far from any other is kept
    s = r["singles"]
    flagged = s[(s["relative"] <= A.LIGHT_USE) & (s["walk_min"] < A.REMOVE_WALK_MIN)]
    assert (flagged["verdict"] == "Review").all()
    assert (s[s["relative"] > A.LIGHT_USE]["verdict"] == "Keep").all()


def test_a_building_under_renovation_is_planned_for_not_dismissed():
    import pandas as pd
    site = pd.Series({"status": "renovation", "until": "2027-05-31", "until_text": "spring 2027"})
    before = pd.Timestamp("2026-10-05", tz="America/New_York")
    after = pd.Timestamp("2027-09-15", tz="America/New_York")
    assert A.renovation_status(site, 0, before)[0] == "Plan for the reopening"
    assert "check the reopening date" in A.renovation_status(site, 0, after)[1]      # overdue, still no classes
    assert A.renovation_status(site, 120, after) is None                             # back in use: normal rules
    assert A.renovation_status(pd.Series({"status": ""}), 0, before) is None
    burnell = A.candidate_sites().set_index("building").loc["Burnell Hall"]
    assert burnell["status"] == "renovation" and burnell["until_text"] == "spring 2027"
