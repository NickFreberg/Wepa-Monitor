"""bridgew.edu parsers and the academic-calendar logic (fixtures are saved copies of the real pages)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from wepa_monitor import campus as C

F = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cal():
    return C.parse_academic_calendar((F / "bsu_academic_calendar.html").read_text())


@pytest.fixture(scope="module")
def halls_schedule():
    return C.parse_halls_schedule((F / "bsu_moving_in.html").read_text())


@pytest.fixture(scope="module")
def library():
    return C.parse_library_hours((F / "bsu_library_hours.html").read_text(), date(2026, 10, 1))


@pytest.fixture(scope="module")
def built(cal, halls_schedule, library):
    halls = C.parse_hall_sizes((F / "bsu_residence_halls.html").read_text())
    return C.build(pd.concat([cal, halls_schedule], ignore_index=True), halls, library)


def test_calendar_parses_every_entry(cal):
    assert len(cal) == 166
    assert not (cal["kind"] == "other").any()
    fall = cal[cal["term"] == "Fall 2026"].set_index("kind")["date"]
    assert fall["classes_begin"] == date(2026, 9, 2)
    assert fall["finals_start"] == date(2026, 12, 14) and fall["finals_end"] == date(2026, 12, 18)


def test_halls_schedule(halls_schedule):
    k = halls_schedule.set_index("date")["kind"]
    assert k[date(2026, 12, 19)] == "halls_close" and k[date(2027, 1, 17)] == "halls_open"
    assert (halls_schedule["kind"] == "move_in").sum() == 3


def test_hall_sizes():
    h = C.parse_hall_sizes((F / "bsu_residence_halls.html").read_text()).set_index("building")
    assert h.loc["Shea/Durgin", "residents"] == 658 and not h.loc["Shea/Durgin", "estimated"]
    assert h.loc["Weygand Hall", "residents"] == 500
    assert h.loc["Miles Hall", "estimated"]          # not stated on the page: shares the remainder
    assert h["residents"].sum() == pytest.approx(3300, abs=2)


def test_library_hours(library):
    lib = library.set_index("date")
    assert (lib.loc[date(2026, 9, 28), "open"], lib.loc[date(2026, 9, 28), "close"]) == (7.5, 23.0)
    assert pd.isna(lib.loc[date(2026, 10, 12), "open"])        # closed on Indigenous Peoples Day
    assert date(2027, 1, 16) in lib.index                      # year rolls over correctly


def test_phases(built):
    on = lambda d: built.on(d)["phase"]
    assert on(date(2026, 9, 1)) == "move_in"
    assert on(date(2026, 9, 7)) == "holiday"
    assert on(date(2026, 11, 27)) == "thanksgiving"
    assert on(date(2026, 12, 12)) == "classes"                 # weekend inside the term
    assert on(date(2026, 12, 15)) == "finals"
    assert on(date(2027, 1, 5)) == "winter_break"
    assert on(date(2027, 3, 10)) == "spring_break"
    assert on(date(2027, 7, 1)) in ("summer", "summer_session")


def test_halls_open_and_inferred_years(built):
    assert not built.on(date(2027, 1, 5))["halls_open"]
    assert built.on(date(2027, 1, 17))["halls_open"]
    # 2027-28 isn't published by Residence Life: inferred close the day after fall finals.
    assert built.on(date(2027, 12, 17))["halls_open"] and not built.on(date(2027, 12, 18))["halls_open"]


def test_closed_dates_include_holidays(built):
    closed = built.closed_dates()
    assert {"2026-10-12", "2026-11-11", "2026-11-26", "2027-01-18", "2026-12-25"} <= closed
    assert "2026-11-27" not in closed                          # the library is open the day after


def test_state_holidays_observed():
    hol = C.state_holidays(2026)
    assert date(2026, 7, 3) in hol                             # July 4 is a Saturday
    assert date(2026, 4, 20) in hol                            # Patriots' Day, third Monday of April


def test_exposure_respects_halls_and_library(built):
    tz = "America/New_York"
    s, e = pd.Timestamp("2027-01-04 00:00", tz=tz), pd.Timestamp("2027-01-05 00:00", tz=tz)
    assert C.in_use_seconds(built, "Shea/Durgin", "residence", s, e) == 0          # halls closed
    assert C.in_use_seconds(built, "Moakley Center", "lab", s, e) == 86400
    s, e = pd.Timestamp("2026-09-28 00:00", tz=tz), pd.Timestamp("2026-09-29 00:00", tz=tz)
    assert C.in_use_seconds(built, "Maxwell Library", "lab", s, e) == 15.5 * 3600  # 7:30 AM-11 PM


def test_layout_change_raises():
    with pytest.raises(C.ParseError):
        C.parse_academic_calendar("<html><body><h1>Moved</h1></body></html>")
