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


def test_printer_review_waits_for_a_full_year(monkeypatch):
    from wepa_monitor import config, metrics as M
    ds = M.load(config.DEMO_DATA_DIR)
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
