"""Fetch and parse the Wepa print-station status page.

Columns are located by their header text, not by position, so an added or
reordered column fails loudly (ParseError) instead of silently shifting
every value into the wrong field.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from . import config, rules


class ParseError(RuntimeError):
    pass


@dataclass
class ScrapeResult:
    attempt_ts: datetime
    ok: bool
    http_status: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    page_ts: datetime | None = None
    records: list[dict] = field(default_factory=list)
    html: str | None = field(default=None, repr=False)


# Header text (normalized) -> canonical field. Grouped headers ("Toner %"
# spanning K/C/M/Y) are combined with their sub-header first.
_COLUMN_MAP = {
    "p.s.#": "station_id",
    "description": "description",
    "status message": "status_message",
    "printer text": "printer_text",
    "toner % k": "toner_k", "toner % c": "toner_c",
    "toner % m": "toner_m", "toner % y": "toner_y",
    "drum % k": "drum_k", "drum % c": "drum_c",
    "drum % m": "drum_m", "drum % y": "drum_y",
    "belt %": "belt",
    "fuser %": "fuser",
}
_REQUIRED = set(_COLUMN_MAP.values())

_PAGE_TZ = {"CDT": "America/Chicago", "CST": "America/Chicago",
            "EDT": "America/New_York", "EST": "America/New_York"}


def _norm(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _header_names(table) -> list[str]:
    """Flatten a two-row header with rowspan/colspan into one name per column."""
    rows = table.find("thead").find_all("tr")
    top = rows[0].find_all("th")
    sub = [_norm(th.get_text()) for th in rows[1].find_all("th")] if len(rows) > 1 else []
    names, sub_i = [], 0
    for th in top:
        span = int(th.get("colspan", 1))
        label = _norm(th.get_text())
        if span == 1:
            names.append(label)
        else:
            for _ in range(span):
                names.append(f"{label} {sub[sub_i]}")
                sub_i += 1
    return names


def _to_pct(text: str):
    text = text.strip().rstrip("%")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    value = float(text)
    return int(round(value)) if 0 <= value <= 100 else None


def parse_page_timestamp(text: str) -> datetime | None:
    """'Thu Oct 01st, 2026 11:44 CDT' -> aware UTC datetime."""
    m = re.search(r"(\w{3}) (\d{1,2})(?:st|nd|rd|th)?, (\d{4}) (\d{1,2}):(\d{2}) ([A-Z]{3})", text)
    if not m:
        return None
    mon, day, year, hh, mm, tz = m.groups()
    try:
        naive = datetime.strptime(f"{mon} {day} {year} {hh}:{mm}", "%b %d %Y %H:%M")
    except ValueError:
        return None
    zone = ZoneInfo(_PAGE_TZ.get(tz, "America/Chicago"))
    return naive.replace(tzinfo=zone).astimezone(timezone.utc)


def parse_status_page(html: str) -> tuple[datetime | None, list[dict]]:
    """Return (page timestamp, one record per station)."""
    soup = BeautifulSoup(html, "lxml")

    page_ts = None
    header = soup.select_one(".content-header .search-div")
    if header:
        page_ts = parse_page_timestamp(header.get_text(" "))

    records: list[dict] = []
    tables = soup.select("table.ps-table")
    if not tables:
        raise ParseError("no table.ps-table found on page")

    for table in tables:
        section_h1 = table.find_previous("header", class_="topic-header")
        section = "Unknown"
        if section_h1 and section_h1.h1:
            section = section_h1.h1.get_text().split(":")[0].strip()

        names = _header_names(table)
        index = {_COLUMN_MAP[n]: i for i, n in enumerate(names) if n in _COLUMN_MAP}
        missing = _REQUIRED - index.keys()
        if missing:
            raise ParseError(f"section {section!r}: missing columns {sorted(missing)}; got {names}")

        for tr in table.find("tbody").find_all("tr", recursive=False):
            classes = tr.get("class") or []
            if "small-row" in classes:   # mobile-only duplicate of the row above
                continue
            cells = tr.find_all("td", recursive=False)
            if len(cells) < len(names):
                continue

            def cell(name: str) -> str:
                return cells[index[name]].get_text("\n").strip()

            codes = [rules.normalize_code(c) for c in cell("status_message").split(",") if c.strip()]
            messages = rules.split_printer_text(cell("printer_text"))
            row_class = next((c for c in classes if c in rules.SEVERITY_ORDER), None)

            record = {
                "section": section,
                "station_id": cell("station_id").strip(),
                "description": " – ".join(p.strip() for p in cell("description").splitlines() if p.strip()),
                "row_status": row_class or rules.fallback_severity(codes),
                "status_codes": ",".join(codes),
                "printer_text": " | ".join(messages),
            }
            for comp in config.COMPONENTS:
                record[comp] = _to_pct(cell(comp))
            records.append(record)

    if not records:
        raise ParseError("page parsed but contained no station rows")
    return page_ts, records


def fetch(url: str = config.STATUS_URL) -> ScrapeResult:
    """One scrape attempt. Never raises: failures are data, recorded in the log."""
    attempt = datetime.now(timezone.utc)
    t0 = time.monotonic()
    result = ScrapeResult(attempt_ts=attempt, ok=False)
    try:
        resp = requests.get(url, timeout=config.HTTP_TIMEOUT_S,
                            headers={"User-Agent": config.USER_AGENT})
        result.http_status = resp.status_code
        resp.raise_for_status()
        result.html = resp.text
        result.page_ts, result.records = parse_status_page(resp.text)
        result.ok = True
    except Exception as exc:  # noqa: BLE001 - every failure mode is logged the same way
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    result.duration_ms = int((time.monotonic() - t0) * 1000)
    return result
