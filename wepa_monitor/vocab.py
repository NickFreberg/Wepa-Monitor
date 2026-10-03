"""The words the app uses for status, in one place, so every page, export and AI answer agrees.

Station status (right now)
    Operational      printing normally
    Degraded         printing, with a warning (paper low, drum near end of life, wrong paper size)
    Out of service   can't print
    No signal        the station hasn't reported recently, so its status isn't known

Incidents (an outage or warning, from start to finish)
    Ongoing          still open
    Resolved         over; the time it took is shown with it

Support sub-status, added to anything not Operational
    Support unavailable until <when>   the responsible desk (ResNet or the IT Service Center) is
                                        closed, so nobody is on duty to fix it before then

"In progress" is deliberately not used: the monitor can see that a problem is open, not whether
someone is working on it.
"""
from __future__ import annotations

import pandas as pd

STATE = {"green": "Operational", "yellow": "Degraded", "red": "Out of service", "stale": "No signal",
         "nodata": "No data"}
STATE_SHORT = {"green": "Operational", "yellow": "Degraded", "red": "Out of service", "stale": "No signal"}
INCIDENT = {"open": "Ongoing", "closed": "Resolved"}
EVENT = {"down": "Outage began", "recovered": "Resolved", "warning": "Degraded", "warning_cleared": "Restored"}


def state(s: str) -> str:
    return STATE.get(s, STATE["stale"])


def incident(open_: bool) -> str:
    return INCIDENT["open" if open_ else "closed"]


def support_substate(owner: str, now: pd.Timestamp) -> str:
    """'' while the desk is staffed; otherwise 'Support unavailable until tomorrow 10 AM'."""
    from . import support
    try:
        open_, text = support.desk_status(owner, now)
    except Exception:  # noqa: BLE001 - an unknown team just gets no sub-status
        return ""
    if open_:
        return ""
    return "Support unavailable" + (text[len("Closed"):] if text.startswith("Closed") else "")
