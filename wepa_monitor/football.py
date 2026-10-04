"""BSU football game days, for the "Go Bears" theme.

On a day the Bridgewater State Bears play football (home or away), the BSU appearance becomes "Go Bears".
The schedule comes from the public iCalendar feed on bsubears.com (PrestoSports), refreshed at most every
12 hours in the background and cached in records/football.json. reference/football.csv is the fallback when
the feed can't be reached.

    WEPA_GAMEDAY=1         force a game day (for a demo or a test)
    WEPA_GAMEDAY=0         never a game day
    WEPA_FOOTBALL_FEED=0   don't contact bsubears.com; use the cache or reference/football.csv
"""
from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

FEED = "https://bsubears.com/sports/fball/{season}/schedule?print=ical"
REFERENCE = config.ROOT / "reference" / "football.csv"
MAX_AGE = 12 * 3600
_TZ = ZoneInfo(config.LOCAL_TZ)
_lock = threading.Lock()
_busy = False


def season(day: date) -> str:
    """PrestoSports season label: the 2026 fall season is "2026-27"."""
    y = day.year if day.month >= 7 else day.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _clean(value: str) -> str:
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()


def parse_ical(text: str) -> list[dict]:
    """Games from the feed: one dict per game that is on (postponed and cancelled games are left out)."""
    games, ev = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            ev = {}
        elif line == "END:VEVENT" and ev is not None:
            game = _game(ev)
            if game:
                games.append(game)
            ev = None
        elif ev is not None and ":" in line:
            key, value = line.split(":", 1)
            ev[key.split(";", 1)[0]] = _clean(value)
    return sorted(games, key=lambda g: g["kickoff"])


def _game(ev: dict) -> dict | None:
    desc, status = ev.get("DESCRIPTION", ""), ev.get("STATUS", "CONFIRMED")
    if status == "CANCELLED" or re.search(r"\b(postponed|cancel+ed)\b", desc, re.I) or "DTSTART" not in ev:
        return None
    raw = ev["DTSTART"]
    try:
        if raw.endswith("Z"):
            start = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(_TZ)
        elif "T" in raw:
            start = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=_TZ)
        else:
            start = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=_TZ)
    except ValueError:
        return None
    summary = re.sub(r"^\(Football\)\s*", "", ev.get("SUMMARY", ""))
    summary = re.sub(r"\s*\(\d+-\d+\)\s*$", "", summary)
    location = ev.get("LOCATION", "")
    m = re.match(r"(.+?)\s+(?:vs\.|at)\s+(.+)$", summary)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        opponent = b if a.startswith("Bridgewater") else a
    else:
        opponent = summary
    home = "Bridgewater, MA" in location
    extras = [p.strip() for p in desc.split(",")[1:]]
    note = next((p for p in extras if p and not re.match(r"(Final|W|L|T)\b|\d+-\d+$", p)), "")
    return {"date": start.date().isoformat(), "kickoff": start.isoformat(), "opponent": opponent, "home": home,
            "location": location, "note": note}


def _reference() -> list[dict]:
    if not REFERENCE.exists():
        return []
    with REFERENCE.open(newline="", encoding="utf-8") as fh:
        return [{**r, "home": r.get("home", "").lower() == "yes"} for r in csv.DictReader(fh)]


def _cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "records" / "football.json"


def refresh(data_dir: Path, session=None, today: date | None = None) -> list[dict]:
    """Fetch this season's feed and cache it. Raises on a network or parse failure."""
    import requests
    today = today or datetime.now(_TZ).date()
    http = session or requests
    resp = http.get(FEED.format(season=season(today)), timeout=15,
                    headers={"User-Agent": "BSU-Student-Printing-Ops (game-day theme)"})
    resp.raise_for_status()
    games = parse_ical(resp.text)
    if not games:
        raise ValueError("the football feed had no games")
    path = _cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched": time.time(), "season": season(today), "games": games}, indent=1))
    tmp.replace(path)
    return games


def _refresh_quietly(data_dir: Path) -> None:
    global _busy
    try:
        refresh(data_dir)
    except Exception:  # noqa: BLE001 - a missing schedule only means no game-day theme
        pass
    finally:
        _busy = False


def games(data_dir: Path | None) -> list[dict]:
    """The schedule: the cached feed, else the reference file. Starts a background refresh when stale."""
    global _busy
    cached, age = None, None
    if data_dir is not None:
        path = _cache_path(data_dir)
        try:
            blob = json.loads(path.read_text())
            cached, age = blob.get("games") or None, time.time() - float(blob.get("fetched", 0))
        except (OSError, ValueError):
            pass
        stale = age is None or age > MAX_AGE
        if stale and os.environ.get("WEPA_FOOTBALL_FEED", "1") != "0":
            with _lock:
                if not _busy:
                    _busy = True
                    threading.Thread(target=_refresh_quietly, args=(data_dir,), daemon=True).start()
    return cached or _reference()


def today_game(data_dir: Path | None, today: date | None = None) -> dict | None:
    """Today's game, or None. Honors WEPA_GAMEDAY (1 forces a game day, 0 turns the theme off)."""
    force = os.environ.get("WEPA_GAMEDAY", "").strip()
    if force == "0":
        return None
    if force == "1":
        return {"date": (today or datetime.now(_TZ).date()).isoformat(), "opponent": "", "home": True,
                "location": "Mazzaferro Field, Bridgewater, MA", "note": "", "kickoff": ""}
    day = (today or datetime.now(_TZ).date()).isoformat()
    return next((g for g in games(data_dir) if g["date"] == day), None)


def headline(game: dict) -> str:
    """'Game day: Bears vs. Fitchburg St. at Mazzaferro Field, 12:00 PM. Go Bears!'"""
    if not game.get("opponent"):
        return "Game day. Go Bears!"
    where = "vs." if game.get("home") else "at"
    text = f"Game day: Bears {where} {game['opponent']}"
    if game.get("kickoff"):
        try:
            k = datetime.fromisoformat(game["kickoff"])
            text += f", kickoff {k.strftime('%I:%M %p').lstrip('0')}"
        except ValueError:
            pass
    if game.get("note"):
        text += f" ({game['note']})"
    return text + ". Go Bears!"
