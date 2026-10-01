"""Business rules for Wepa status codes.

The status page already classifies each station as green / yellow / red via
the row's CSS class; that is the vendor's verdict and the primary severity
signal. The tables below only add friendly names, a fix category (used for
routing and pick lists) and a fallback severity when a row has no class.

Codes marked `observed` were seen on the live BSU page. The rest are inferred
from the page's own "Alert Info" legend and should be confirmed as they show
up in real snapshots.
"""
from __future__ import annotations

import re

SEVERITY_ORDER = {"green": 0, "yellow": 1, "red": 2}

# code -> (friendly name, fallback severity, fix category, observed?)
STATUS_CODES: dict[str, tuple[str, str, str, bool]] = {
    "paper_out_error": ("Paper out", "red", "paper", True),
    "printer_down": ("Printer down", "red", "hardware", True),
    "tray_missing": ("Tray missing", "red", "paper", True),
    "paper_jam": ("Paper jam", "red", "jam", False),
    "fatal_error": ("Fatal error", "red", "power_cycle", False),
    "service_call": ("Service call required", "red", "power_cycle", False),
    "not_reachable": ("Not reachable", "red", "network", False),
    "cover_open": ("Top cover open", "red", "hardware", False),
    "toner_sensor_error": ("Toner sensor error", "red", "consumable", False),
    "toner_critical": ("Toner critical", "red", "consumable", False),
    "drum_critical": ("Drum critical", "red", "consumable", False),
    "paper_low": ("Paper low", "yellow", "paper", False),
    "toner_low": ("Toner low", "yellow", "consumable", False),
    "incorrect_tray_size": ("Incorrect paper tray size", "yellow", "paper", False),
}

FIX_CATEGORIES = {
    "paper": "Refill / reseat paper",
    "jam": "Clear jam",
    "hardware": "Inspect printer",
    "power_cycle": "Power-cycle printer",
    "network": "Check network / restart station",
    "consumable": "Replace consumable",
    "other": "Investigate",
}


def normalize_code(raw: str) -> str:
    """'Alert_paper_out_error' -> 'paper_out_error'."""
    code = raw.strip()
    code = re.sub(r"^alert_", "", code, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "_", code.lower()).strip("_")


def code_label(code: str) -> str:
    if code in STATUS_CODES:
        return STATUS_CODES[code][0]
    return code.replace("_", " ").capitalize()


def code_fix_category(code: str) -> str:
    return STATUS_CODES.get(code, ("", "", "other", False))[2]


def fallback_severity(codes: list[str]) -> str:
    worst = "green"
    for code in codes:
        sev = STATUS_CODES.get(code, ("", "red", "", False))[1]
        if SEVERITY_ORDER[sev] > SEVERITY_ORDER[worst]:
            worst = sev
    return worst


# Printer-text messages arrive concatenated with no delimiter, e.g.
# "Paper Out Warning for Tray1Paper Out Warning for Tray2". A new message
# starts where a capital letter directly follows a lowercase letter or digit.
_MESSAGE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TRAY_EMPTY = re.compile(r"Paper Out Warning for (Tray\s*\d+|MP Tray|Bypass)", re.IGNORECASE)


def split_printer_text(text: str) -> list[str]:
    messages: list[str] = []
    for line in re.split(r"[\r\n|]+", text or ""):
        for part in _MESSAGE_BOUNDARY.split(line):
            part = " ".join(part.split())
            if part:
                messages.append(part)
    return messages


def empty_trays(messages: list[str]) -> list[str]:
    """Tray names reported empty, e.g. ['Tray1', 'Tray2']."""
    trays = []
    for msg in messages:
        m = _TRAY_EMPTY.search(msg)
        if m:
            trays.append(m.group(1).replace(" ", ""))
    return trays
