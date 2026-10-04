"""How every duration in the app is written: whole days, hours and minutes, never decimals.

    human(5400)          -> "1 hour, 30 minutes"
    human(97 * 3600)     -> "4 days, 1 hour"
    human(30)            -> "less than 1 minute"
    hours(2.25)          -> "2 hours, 15 minutes"

Values are rounded to the nearest minute first, so 1 h 59.6 min reads "2 hours". Zero parts are left
out ("2 days, 5 minutes"). Inside each part the space is non-breaking, so "3 days" never splits
across lines; the comma between parts may wrap.
"""
from __future__ import annotations

import math

NB = " "


def _bad(v) -> bool:
    try:
        return v is None or not math.isfinite(float(v))
    except (TypeError, ValueError):
        return True


def parts(seconds: float) -> tuple[int, int, int]:
    total = int(round(max(float(seconds), 0.0) / 60))
    d, rem = divmod(total, 1440)
    h, m = divmod(rem, 60)
    return d, h, m


def human(seconds, missing: str = "—", nbsp: bool = True) -> str:
    if _bad(seconds):
        return missing
    sp = NB if nbsp else " "
    s = max(float(seconds), 0.0)
    if 0 < s < 30:
        return f"less than 1{sp}minute"
    d, h, m = parts(s)
    out = [f"{n:,}{sp}{unit}{'s' if n != 1 else ''}" for n, unit in ((d, "day"), (h, "hour"), (m, "minute")) if n]
    return ", ".join(out) if out else f"0{sp}minutes"


def minutes(m, missing: str = "—", nbsp: bool = True) -> str:
    return missing if _bad(m) else human(float(m) * 60, missing, nbsp)


def hours(h, missing: str = "—", nbsp: bool = True) -> str:
    return missing if _bad(h) else human(float(h) * 3600, missing, nbsp)


def days(d, missing: str = "—", nbsp: bool = True) -> str:
    return missing if _bad(d) else human(float(d) * 86400, missing, nbsp)


def ago(seconds, missing: str = "never") -> str:
    """'45 seconds ago' under 90 seconds, otherwise the same whole-unit form plus 'ago'."""
    if _bad(seconds):
        return missing
    if seconds < 90:
        return f"{max(seconds, 0):.0f} seconds ago"
    return human(seconds) + " ago"
