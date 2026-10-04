"""Special themes: the football schedule behind Go Bears, and the appearance slots Cosmic, Cup and Go Bears take."""
from __future__ import annotations

import json
import time
from datetime import date

from wepa_monitor import football
from wepa_monitor.dashboard import app as A
from wepa_monitor.dashboard.theme import THEMES, TOKENS, ink, is_dark

FEED = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
DTSTART:20261003T200000Z\r
SUMMARY:(Football) Worcester St. vs. Bridgewater St. (34-35)\r
LOCATION:Mazzaferro Field\\, Bridgewater\\, MA\r
DESCRIPTION:Football: (Football) Worcester St. vs. Bridgewater St. (34-35)\\, Homecoming 2026\\, Final\\, 34-35\r
STATUS:CONFIRMED\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART:20260926T190000Z\r
SUMMARY:(Football) Framingham St. vs. Bridgewater St.\r
LOCATION:Mazzaferro Field\\, Bridgewater\\, MA\r
DESCRIPTION:Football: (Football) Framingham St. vs. Bridgewater St.\\, Postponed\r
STATUS:TENTATIVE\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART:20261017T160000Z\r
SUMMARY:(Football) Bridgewater St. at Dean\r
LOCATION:Dale Lippert Field\\, Fra\r
 nklin\\, MA\r
DESCRIPTION:Football: (Football) Bridgewater St. at Dean\r
STATUS:CONFIRMED\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_feed_parsing_skips_postponed_and_reads_home_and_away():
    games = football.parse_ical(FEED)
    assert [g["opponent"] for g in games] == ["Worcester St.", "Dean"]
    home, away = games
    assert home["home"] and home["date"] == "2026-10-03" and home["kickoff"].startswith("2026-10-03T16:00")
    assert home["note"] == "Homecoming 2026"
    assert not away["home"] and away["location"] == "Dale Lippert Field, Franklin, MA"   # folded line joined
    assert football.headline(home) == "Game day: Bears vs. Worcester St., kickoff 4:00 PM (Homecoming 2026). Go Bears!"
    assert football.headline(away).startswith("Game day: Bears at Dean")


def test_season_label():
    assert football.season(date(2026, 10, 4)) == "2026-27"
    assert football.season(date(2027, 3, 1)) == "2026-27"


def test_today_game_uses_cache_then_reference(tmp_path, monkeypatch):
    monkeypatch.delenv("WEPA_GAMEDAY")
    assert football.today_game(tmp_path, date(2026, 11, 14))["opponent"] == "Mass. Maritime"   # reference file
    assert football.today_game(tmp_path, date(2026, 11, 15)) is None
    cache = tmp_path / "records" / "football.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"fetched": time.time(), "games": football.parse_ical(FEED)}))
    assert football.today_game(tmp_path, date(2026, 10, 17))["opponent"] == "Dean"
    assert football.today_game(tmp_path, date(2026, 11, 14)) is None                       # cache wins


def test_gameday_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WEPA_GAMEDAY", "1")
    assert football.headline(football.today_game(tmp_path, date(2026, 1, 1))) == "Game day. Go Bears!"
    monkeypatch.setenv("WEPA_GAMEDAY", "0")
    assert football.today_game(tmp_path, date(2026, 11, 14)) is None


def test_refresh_writes_cache(tmp_path):
    class Resp:
        text = FEED
        def raise_for_status(self):
            pass

    class Session:
        def get(self, url, **kw):
            assert url == "https://bsubears.com/sports/fball/2026-27/schedule?print=ical"
            return Resp()

    games = football.refresh(tmp_path, Session(), date(2026, 10, 4))
    assert len(games) == 2
    assert json.loads((tmp_path / "records" / "football.json").read_text())["season"] == "2026-27"


def _slots(options):
    return {o["value"]: o["label"].children[1].children[0].children for o in options}


def test_theme_slots_keep_their_values():
    assert _slots(A.theme_options({}, False)) == {"auto": "Match my device", "light": "Light", "dark": "Dark",
                                                  "crimson": "BSU"}
    assert _slots(A.theme_options({"cosmic": True}, False))["dark"] == "Cosmic"
    both = _slots(A.theme_options({"cosmic": True, "cup": True}, True))
    assert both == {"auto": "Match my device", "light": "Cup", "dark": "Cosmic", "crimson": "Go Bears"}
    desc = A.theme_options({"cosmic": True, "cup": True}, False)[0]["label"].children[1].children[1].children
    assert desc.startswith("Cup or Cosmic")


def test_every_theme_has_complete_chart_tokens():
    keys = set(TOKENS["light"])
    for t in THEMES:
        assert set(TOKENS[t]) == keys, t
        assert len(TOKENS[t]["series"]) == 8
        ink(t, "toner_k")
    assert is_dark("cosmic") and is_dark("dark") and not is_dark("cup") and not is_dark("gobears")


def test_cup_art_is_the_installed_image_or_the_drawn_stand_in(tmp_path, monkeypatch):
    from flask import Flask

    from wepa_monitor import theme_art
    app = Flask(__name__)
    theme_art.install(app, tmp_path)
    c = app.test_client()
    r = c.get("/_theme/cup")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<svg" in r.data
    (tmp_path / "cup.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    r = c.get("/_theme/cup")
    assert r.mimetype == "image/jpeg" and r.data.startswith(b"\xff\xd8")
    monkeypatch.setenv("WEPA_CUP_IMAGE", str(tmp_path / "missing.png"))
    assert c.get("/_theme/cup").mimetype == "image/svg+xml"
