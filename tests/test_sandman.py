"""The Sandman easter egg: phrase matching without storing lyrics, the audio route, and the AI voice."""
from __future__ import annotations

import json

from wepa_monitor import ai, sandman


def test_matches_three_words_any_case_and_punctuation(tmp_path):
    assert sandman.matches("EXIT light, enter NIGHT!", tmp_path)
    assert sandman.matches("so... exit light enter", tmp_path)
    assert sandman.matches("off to never-never land", tmp_path)
    assert not sandman.matches("exit light", tmp_path)                    # only two words
    assert not sandman.matches("which station was down the longest?", tmp_path)
    assert not sandman.matches("", tmp_path)


def test_learn_stores_hashes_never_text(tmp_path):
    n = sandman.learn(tmp_path, "Purple printers hum softly tonight")
    blob = (tmp_path / "records" / "sandman.json").read_text()
    assert "purple" not in blob.lower() and "printers" not in blob.lower()
    assert n == 3 and len(json.loads(blob)["hashes"]) == 3
    assert sandman.matches("why do purple printers hum?", tmp_path)
    assert not sandman.matches("purple printers", tmp_path)


def test_audio_route_is_optional(tmp_path, monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    sandman.install(app, tmp_path)
    c = app.test_client()
    assert c.get("/_sandman/audio").status_code == 404
    (tmp_path / "sandman.mp3").write_bytes(b"ID3fake")
    r = c.get("/_sandman/audio")
    assert r.status_code == 200 and r.data == b"ID3fake"
    other = tmp_path / "elsewhere.ogg"
    other.write_bytes(b"OggS")
    monkeypatch.setenv("WEPA_SANDMAN_AUDIO", str(other))
    assert c.get("/_sandman/audio").data == b"OggS"


def test_voice_changes_tone_not_the_cache(monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "enabled", lambda: True)
    monkeypatch.setattr(ai, "_allow", lambda: True)
    monkeypatch.setattr(ai, "_complete", lambda message, tk, timeout, effort: seen.append(message) or "All fine.")
    ai._cache.clear()
    ai.ask("How are the printers?", "facts", cache_key="k")
    ai.ask("How are the printers?", "facts", cache_key="k", voice=sandman.VOICE)
    assert len(seen) == 2                               # a voiced answer is never served from the plain cache
    assert sandman.VOICE not in seen[0] and seen[1].endswith(sandman.VOICE)
    assert "Don't claim to be James Hetfield" in sandman.VOICE and "facts" in sandman.VOICE


def test_sandman_reply_and_voice_helper():
    from wepa_monitor.dashboard import app as A
    from wepa_monitor.dashboard.views import insights_view
    assert A._voice({"sandman": 1}) == sandman.VOICE and A._voice({}) is None and A._voice(None) is None
    assert "Exit Sandman" in str(insights_view.render_sandman())
