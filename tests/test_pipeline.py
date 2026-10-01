"""Tests for the parser and the business rules that every metric depends on."""
from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pandas as pd
import pytest

from wepa_monitor import config, consumables, events, rules
from wepa_monitor.scrape import ParseError, parse_page_timestamp, parse_status_page

FIXTURE = Path(__file__).parent / "fixtures" / "status_page_2026-10-01.html"


# --- parser -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def parsed():
    return parse_status_page(FIXTURE.read_text())


def test_parses_all_sections_and_stations(parsed):
    _, records = parsed
    assert len(records) == 31
    sections = pd.Series([r["section"] for r in records]).value_counts().to_dict()
    assert sections == {"ResNet": 16, "Student Computer Labs": 14, "Satellite Campuses": 1}


def test_page_timestamp_is_utc(parsed):
    ts, _ = parsed
    assert ts.tzinfo == timezone.utc
    assert (ts.hour, ts.minute) == (16, 44)          # 11:44 CDT


def test_columns_mapped_by_header_not_position(parsed):
    _, records = parsed
    crimson = next(r for r in records if r["station_id"] == "01270")
    assert (crimson["toner_k"], crimson["toner_c"], crimson["toner_m"], crimson["toner_y"]) == (90, 10, 99, 16)
    assert (crimson["belt"], crimson["fuser"]) == (93, 95)   # matches the page's own detail row
    assert crimson["row_status"] == "green"


def test_multiple_codes_and_concatenated_printer_text(parsed):
    _, records = parsed
    maxwell = next(r for r in records if r["station_id"] == "00518")
    assert maxwell["row_status"] == "red"
    assert maxwell["status_codes"] == "tray_missing,paper_out_error"
    shea = next(r for r in records if r["station_id"] == "00498")
    assert shea["printer_text"] == "Paper Out Warning for Tray1 | Paper Out Warning for Tray2"
    dmf = next(r for r in records if r["station_id"] == "02065")
    assert dmf["printer_text"] == "Paper Feed Jam | Paper Jam for Duplex Unit"


def test_missing_column_fails_loudly():
    html = FIXTURE.read_text().replace("<center>Belt %</center>", "<center>Bolt %</center>")
    with pytest.raises(ParseError):
        parse_status_page(html)


def test_timestamp_parser_handles_ordinals():
    assert parse_page_timestamp("Mon Nov 02nd, 2026 9:05 CST").hour == 15


def test_tray_extraction():
    msgs = rules.split_printer_text("Paper Out Warning for Tray1Paper Out Warning for Tray2")
    assert rules.empty_trays(msgs) == ["Tray1", "Tray2"]


# --- consumables: the replacement rule ------------------------------------------------

def _series(levels, comp="toner_k", station="00001"):
    ts = pd.date_range("2026-09-01", periods=len(levels), freq="1h", tz="UTC")
    return pd.DataFrame({"station_id": station, "scrape_ts": ts, "level": [float(v) for v in levels],
                         "component": comp})


def test_usage_ignores_replacement_and_keeps_counting():
    used, repl = consumables.usage(_series([12, 7, 3, 100, 94]))
    assert used["used"].sum() == pytest.approx(15)            # 5 + 4 + 6, never -97
    assert len(repl) == 1
    assert repl.iloc[0]["level_before"] == 3 and repl.iloc[0]["level_after"] == 100


def test_jitter_is_not_double_counted():
    used, repl = consumables.usage(_series([51, 50, 51, 50, 51, 49]))
    assert used["used"].sum() == pytest.approx(2)             # true drop 51 -> 49
    assert repl.empty


def test_small_rise_below_threshold_is_not_a_replacement():
    used, repl = consumables.usage(_series([40, 30, 30 + config.REPLACEMENT_JUMP_PTS - 1, 20]))
    assert repl.empty
    assert used["used"].sum() == pytest.approx(20)


def test_series_are_independent():
    df = pd.concat([_series([50, 40], "toner_k"), _series([80, 100, 90], "toner_c")], ignore_index=True)
    used, repl = consumables.usage(df.sort_values(["station_id", "component", "scrape_ts"], ignore_index=True))
    by = used.groupby("component")["used"].sum()
    assert by["toner_k"] == 10 and by["toner_c"] == 10
    assert list(repl["component"]) == ["toner_c"]


# --- incidents ----------------------------------------------------------------------

def _snap(states, start="2026-09-01 12:00", freq="1min", station="00001"):
    ts = pd.date_range(start, periods=len(states), freq=freq, tz="UTC")
    return pd.DataFrame({"station_id": station, "scrape_ts": ts, "row_status": states})


def test_incident_duration_runs_to_first_clear_snapshot():
    snap = _snap(["green", "red", "red", "red", "green", "green"])
    inc = events.severity_incidents(snap, snap["scrape_ts"].max())
    assert len(inc) == 1
    assert inc.iloc[0]["status"] == "resolved"
    assert inc.iloc[0]["duration_s"] == 180


def test_open_incident_is_not_resolved():
    snap = _snap(["green", "red", "red"])
    inc = events.severity_incidents(snap, snap["scrape_ts"].max())
    assert inc.iloc[0]["status"] == "open"


def test_long_gap_splits_and_marks_unknown_end():
    a = _snap(["red", "red"], start="2026-09-01 00:00")
    b = _snap(["red", "green"], start="2026-09-01 12:00")     # 12 h later > bridge
    snap = pd.concat([a, b], ignore_index=True)
    inc = events.severity_incidents(snap, snap["scrape_ts"].max())
    assert list(inc["status"]) == ["unknown_end", "resolved"]


def test_unobserved_gap_not_counted_as_covered_time():
    a = _snap(["green", "green"], start="2026-09-01 00:00")
    b = _snap(["green"], start="2026-09-01 02:00")
    spans = events.observation_spans(pd.concat([a, b], ignore_index=True))
    assert spans["covered_s"].tolist() == [60, 60, 60]
