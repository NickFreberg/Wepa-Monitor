"""Planning and statistics (issue #2) and the class-schedule parser."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wepa_monitor import advanced as A, courses, metrics as M

DEMO = Path(__file__).parent.parent / "data" / "demo"
FIX = Path(__file__).parent / "fixtures" / "bsu_course_search.html"


def test_course_page_parsing():
    html = FIX.read_text()
    assert courses.current_term(html) == ("1508611", "2026 FALL - ALL")
    df = courses.parse_page(html, "2026 FALL - ALL")
    assert len(df) and (df["start_min"] < df["end_min"]).all()
    assert "instructor" not in df.columns                       # names are never kept
    assert courses._minutes("9:05am") == 545 and courses._minutes("12:15pm") == 735


def test_weekly_grid_counts_each_hour_a_class_touches():
    df = pd.DataFrame([{"term": "t", "code": "HRG", "building": "Harrington Hall", "room": "HRG205",
                        "days": "MW", "start_min": 14 * 60, "end_min": 15 * 60 + 15,
                        "start_date": pd.Timestamp("2026-09-02"), "end_date": pd.Timestamp("2026-12-18"),
                        "status": "OPEN"}])
    g = courses.weekly_grid(df, pd.Timestamp("2026-10-05"))
    assert set(zip(g["weekday"], g["hour"])) == {(0, 14), (0, 15), (2, 14), (2, 15)}


@pytest.fixture(scope="module")
def ds():
    if not (DEMO / "meta.json").exists():
        pytest.skip("demo data not generated")
    return M.load(DEMO)


def test_monte_carlo_is_ordered_and_reproducible(ds):
    a = A.supplies_monte_carlo(ds, 30, sims=500)
    b = A.supplies_monte_carlo(ds, 30, sims=500)
    t = a["table"]
    assert (t["p50"] <= t["p90"]).all() and (t["p90"] <= t["p95"]).all()
    assert t.equals(b["table"])


def test_extra_coverage_never_makes_outages_longer(ds):
    s, e = M.window(ds, 30)
    for key in A.SCENARIOS:
        r = A.staffing_whatif(ds, s, e, scenario=key, sims=200)
        lo, mid, hi = r["saved_h"]
        assert 0 <= lo <= mid <= hi <= r["total_h"]


def test_bayes_shrinks_toward_campus(ds):
    s, e = M.window(ds, 30)
    b = A.bayes_rates(ds, s, e)
    camp = b.attrs["campus"]
    assert (b["lo"] <= b["mean"]).all() and (b["mean"] <= b["hi"]).all()
    # every estimate sits between its raw rate and the campus rate
    assert (np.minimum(b["raw"], camp) - 1e-9 <= b["mean"]).all()
    assert (b["mean"] <= np.maximum(b["raw"], camp) + 1e-9).all()


def test_spatial_and_markov(ds):
    s, e = M.window(ds, 30)
    cov = A.coverage(ds, s, e)
    assert (cov.loc[cov["same_building"] > 0, "gap"] == False).all()   # noqa: E712
    w = A.warning_to_outage(ds, s, e)
    assert 0 <= w["share"] <= 1 and w["ci"][0] <= w["share"] <= w["ci"][1]
    assert courses.class_vs_printing(ds, s, e)["hours"]["hour"].between(7, 22).all()
