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
    "paper_low": ("Paper low", "yellow", "paper", True),
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


# Codes that carry a variable detail, e.g. 'Alert_Paper_Low_Letter_530_sheets_left' (seen live),
# are folded into one canonical code so fault counts don't split by sheet count.
_CANONICAL = [(re.compile(r"^paper_low(_|$)"), "paper_low")]


def normalize_code(raw: str) -> str:
    """'Alert_paper_out_error' -> 'paper_out_error'; 'Alert_Paper_Low_Letter_530_sheets_left' -> 'paper_low'."""
    code = raw.strip()
    code = re.sub(r"^alert_", "", code, flags=re.IGNORECASE)
    code = re.sub(r"[^a-z0-9]+", "_", code.lower()).strip("_")
    for pattern, canonical in _CANONICAL:
        if pattern.match(code):
            return canonical
    return code


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


def tray_label(tray: str) -> str:
    """'Tray1' -> 'Tray 1', 'MPTray' -> 'MP tray'."""
    m = re.match(r"tray\s*(\d+)$", tray, re.IGNORECASE)
    return f"Tray {m.group(1)}" if m else tray.replace("MPTray", "MP tray")


def empty_trays(messages: list[str]) -> list[str]:
    """Tray names reported empty, e.g. ['Tray1', 'Tray2']."""
    trays = []
    for msg in messages:
        m = _TRAY_EMPTY.search(msg)
        if m:
            trays.append(m.group(1).replace(" ", ""))
    return trays


# --- issues: what is actually wrong, in plain words ----------------------------------------------
# Wepa's codes say *that* a station is down ("printer_down"); the printer's own messages often say
# *why* ("Paper Feed Jam"). Issues combine both into one clean list of problem types, so fault
# counts read "Paper jam: 14" instead of "Printer down: 14", and every type has a plain-language
# phrase for stories ("jammed") and a fix.
#
# key -> (label, what happened (verb phrase), severity, fix category)
ISSUES: dict[str, tuple[str, str, str, str]] = {
    "paper_out": ("Out of paper", "ran out of paper", "red", "paper"),
    "paper_jam": ("Paper jam", "jammed", "red", "jam"),
    "tray_missing": ("Tray disengaged", "had a paper tray pulled out or not seated", "red", "paper"),
    "toner_empty": ("Toner depleted", "ran out of toner", "red", "consumable"),
    "drum_end": ("Drum expired", "needed a new drum", "red", "consumable"),
    "cover_open": ("Cover open", "had a cover or door open", "red", "hardware"),
    "service_call": ("Service required", "reported an error that needs a service call", "red", "power_cycle"),
    "fatal_error": ("System fault", "hit a printer error and needed a restart", "red", "power_cycle"),
    "not_reachable": ("Network unreachable", "dropped off the network", "red", "network"),
    "offline": ("Unresponsive printer", "stopped responding to Wepa", "red", "hardware"),
    "paper_low": ("Paper low", "was running low on paper", "yellow", "paper"),
    "toner_low": ("Toner low", "was running low on toner", "yellow", "consumable"),
    "drum_low": ("Drum near end of life", "had a drum nearing the end of its life", "yellow", "consumable"),
    "wrong_paper": ("Paper size mismatch", "had the wrong paper size in a tray", "yellow", "paper"),
}
# The same issues as a status word, for status lines ("Jammed", "Unreachable"); the nouns above are
# for counts and charts ("Paper jam was the most common fault").
ISSUE_STATUS = {"paper_jam": "Jammed", "not_reachable": "Unreachable", "offline": "Unresponsive"}
_CODE_ISSUE = {
    "paper_out_error": "paper_out", "paper_jam": "paper_jam", "tray_missing": "tray_missing",
    "toner_critical": "toner_empty", "toner_sensor_error": "toner_empty", "drum_critical": "drum_end",
    "cover_open": "cover_open", "service_call": "service_call", "fatal_error": "fatal_error",
    "not_reachable": "not_reachable", "printer_down": "offline", "paper_low": "paper_low",
    "toner_low": "toner_low", "incorrect_tray_size": "wrong_paper",
}
_COLOR = {"k": "black", "c": "cyan", "m": "magenta", "y": "yellow"}
_MESSAGE_ISSUES = [
    (re.compile(r"jam", re.I), "paper_jam"),
    (re.compile(r"\btray\s*\d*\s*(is\s*)?(missing|open|not (set|installed))", re.I), "tray_missing"),
    (re.compile(r"(cover|door).*open|open.*(cover|door)", re.I), "cover_open"),
    (re.compile(r"drum (life )?(end|replace|expired)", re.I), "drum_end"),
    (re.compile(r"drum life warning|drum (near|low)", re.I), "drum_low"),
    (re.compile(r"toner (empty|out|end)|replace toner|no toner", re.I), "toner_empty"),
    (re.compile(r"toner (low|near)", re.I), "toner_low"),
    (re.compile(r"service call|call service|SC\d{3}", re.I), "service_call"),
]
# A generic "printer down" is only the issue when nothing more specific explains it.
_EXPLAINS_DOWN = {"paper_out", "paper_jam", "tray_missing", "toner_empty", "drum_end", "cover_open",
                  "service_call", "fatal_error", "not_reachable"}


def code_issue(code: str) -> str | None:
    """'printer_critical_toner_magenta' -> 'toner_empty'; unknown codes -> None."""
    if code in _CODE_ISSUE:
        return _CODE_ISSUE[code]
    if re.search(r"critical_toner|toner_(empty|out)", code):
        return "toner_empty"
    if re.search(r"critical_drum|drum_(end|out)", code):
        return "drum_end"
    if "jam" in code:
        return "paper_jam"
    return None


def diagnose(status_codes: str, printer_text: str) -> dict[str, list[str]]:
    """Issues present in one snapshot, each with its details (colors, trays, jam locations).

    >>> diagnose("printer_down", "Paper Feed Jam | Paper Jam for Duplex Unit")
    {'paper_jam': ['paper feed', 'duplex unit']}
    """
    found: dict[str, list[str]] = {}

    def add(issue, detail=None):
        lst = found.setdefault(issue, [])
        if detail and detail not in lst:
            lst.append(detail)

    for raw in str(status_codes or "").split(","):
        code = normalize_code(raw) if raw else ""
        if not code:
            continue
        issue = code_issue(code)
        if issue is None:
            add("other:" + code)
            continue
        color = re.search(r"_(black|cyan|magenta|yellow)$", code)
        add(issue, color.group(1) if color else None)
    for msg in split_printer_text(str(printer_text or "").replace(" | ", "\n")):
        if _TRAY_EMPTY.search(msg):
            continue            # one empty tray while others print: a tray incident, not a fault
        for pattern, issue in _MESSAGE_ISSUES:
            if pattern.search(msg):
                detail = None
                if issue == "paper_jam":
                    m = re.search(r"jam (?:for|in|at) (?:the )?(.+)$", msg, re.I)
                    detail = (m.group(1) if m else re.sub(r"\s*jam\s*", " ", msg, flags=re.I)).strip().lower()
                elif issue in ("drum_low", "drum_end", "toner_low", "toner_empty"):
                    m = re.search(r"(black|cyan|magenta|yellow)", msg, re.I)
                    detail = m.group(1).lower() if m else None
                elif issue == "tray_missing":
                    m = re.search(r"tray\s*(\d+)", msg, re.I)
                    detail = f"tray {m.group(1)}" if m else None
                add(issue, detail)
                break
    if "offline" in found and _EXPLAINS_DOWN & set(found):
        del found["offline"]
    return found


def issue_status(issue: str) -> str:
    return ISSUE_STATUS.get(issue) or issue_label(issue)


def issue_label(issue: str) -> str:
    if issue.startswith("other:"):
        return code_label(issue.split(":", 1)[1])
    return ISSUES.get(issue, (issue.replace("_", " ").capitalize(),))[0]


def issue_phrase(issue: str) -> str:
    """What happened, as a verb phrase: 'ran out of paper'."""
    if issue in ISSUES:
        return ISSUES[issue][1]
    return "reported " + issue_label(issue).lower()


def issue_severity(issue: str) -> str:
    return ISSUES.get(issue, ("", "", "red", ""))[2]


def issue_fix_category(issue: str) -> str:
    return ISSUES.get(issue, ("", "", "", "other"))[3]


def describe(status_codes: str, printer_text: str) -> list[str]:
    """Short status lines for a station card: ['Jammed (paper feed, duplex unit)', 'Tray 2 empty']."""
    out = []
    for issue, details in diagnose(status_codes, printer_text).items():
        label = issue_status(issue)
        out.append(f"{label} ({', '.join(details)})" if details else label)
    for msg in split_printer_text(str(printer_text or "").replace(" | ", "\n")):
        m = _TRAY_EMPTY.search(msg)
        if m:
            out.append(f"{tray_label(m.group(1).replace(' ', ''))} empty")
    return list(dict.fromkeys(out))
