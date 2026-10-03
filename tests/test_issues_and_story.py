"""Fault taxonomy, story tone and history awareness, nearest backups."""
from __future__ import annotations

from pathlib import Path


from wepa_monitor import metrics as M, narrative as N, nearby, ops, rules

LIVE = Path(__file__).parent / "fixtures" / "live_first_minutes"


def test_printer_down_with_a_jam_counts_as_a_jam():
    got = rules.diagnose("printer_down", "Paper Feed Jam | Paper Jam for Exit Unit | Paper Jam for Duplex Unit")
    assert got == {"paper_jam": ["paper feed", "exit unit", "duplex unit"]}


def test_unexplained_printer_down_stays_offline():
    assert list(rules.diagnose("printer_down", "")) == ["offline"]


def test_toner_colors_fold_into_one_type():
    got = rules.diagnose("printer_critical_toner_magenta,printer_critical_toner_yellow", "Paper Out Warning for Tray1")
    assert got == {"toner_empty": ["magenta", "yellow"]}            # an empty tray isn't a fault


def test_card_lines_are_plain_language():
    assert rules.describe("tray_missing,paper_out_error", "Tray1 missing") == ["Paper tray out (tray 1)", "Out of paper"]
    assert rules.describe("", "Paper Out Warning for Tray2") == ["Tray 2 empty"]
    assert rules.describe("", "Drum Life Warning for Black") == ["Drum wearing out (black)"]


def test_fault_incidents_use_issues_with_details():
    ds = M.load(LIVE)
    f = ds.fault_inc.set_index(["station_id", "code"])
    assert f.loc[("00517", "paper_jam"), "detail"] == "paper feed, exit unit, duplex unit"
    assert ("00517", "offline") not in f.index


def test_one_long_outage_in_a_fleet_is_not_a_warning():
    assert N.availability_tone(96.7) == "good"
    assert N.availability_tone(93.0) == "warning"
    assert N.availability_tone(88.0) == "critical"
    assert N.availability_tone(93.4, norm=91.7) == "good"           # better than its own norm
    assert N.availability_tone(96.0, norm=99.5) == "warning"        # well below its norm


def test_no_usual_until_a_week_is_recorded():
    ds = M.load(LIVE)
    ds = M.load(LIVE, now=ds.latest["scrape_ts"].max().to_pydatetime())
    p = N.period(ds, "today")
    text = N.to_text(N.story(ds, p).paragraphs)
    assert "than usual" not in text and "typical" not in text
    assert "Monitoring began" in text


def test_backups_point_to_open_buildings_in_us_units():
    ds = M.load(LIVE)
    ds = M.load(LIVE, now=ds.latest["scrape_ts"].max().to_pydatetime())
    alt = nearby.backups(ops.current_status(ds), "00412")
    assert alt.iloc[0]["kind"] == "same building"                  # Scott Hall has a second printer
    public = alt[alt["kind"] == "open to everyone"]
    assert len(public) and (public["building"] != "Scott Hall").all()
    assert not public["building"].isin(ds.stations.loc[ds.stations["station_type"] == "residence", "building"]).any()
    assert nearby.fmt_distance(120) == "390 ft" and nearby.fmt_distance(700) == "0.4 mi"
