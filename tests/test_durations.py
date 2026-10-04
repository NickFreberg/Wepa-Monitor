"""Every duration reads as whole days, hours and minutes."""
from __future__ import annotations

import re
from pathlib import Path

from wepa_monitor import durations as D


def plain(s):
    return s.replace(D.NB, " ")


def test_whole_units_never_decimals():
    assert plain(D.human(5400)) == "1 hour, 30 minutes"
    assert plain(D.hours(2.25)) == "2 hours, 15 minutes"
    assert plain(D.human(90061)) == "1 day, 1 hour, 1 minute"
    assert plain(D.days(3.2)) == "3 days, 4 hours, 48 minutes"
    assert plain(D.human(97 * 3600)) == "4 days, 1 hour"
    assert plain(D.human(3599.9)) == "1 hour"                          # rounded to the minute first
    assert plain(D.human(20)) == "less than 1 minute"
    assert plain(D.human(0)) == "0 minutes"
    assert D.minutes(float("nan")) == "—" and D.hours(None) == "—"
    assert plain(D.ago(45)) == "45 seconds ago" and plain(D.ago(7200)) == "2 hours ago"


def test_no_decimal_hours_or_days_left_in_the_app():
    """Guard: no f-string writes a decimal number followed by hours/days (or h/d) anywhere in the app."""
    pat = re.compile(r"\{[^{}]*:,?\.[1-9]f\}\s?(h|hrs?|hours?|d|days?)\b")
    hits = [f"{p}:{i}" for p in Path("wepa_monitor").rglob("*.py")
            for i, line in enumerate(p.read_text().splitlines(), 1) if pat.search(line)]
    assert hits == []
