"""The Sandman easter egg.

Type three or more words in a row from "Enter Sandman" into Ask the data (any case, punctuation optional) and
the app turns black: the Black Album look, a frontman's voice for the AI assistant, a typed "Exit light. Enter
night." banner, and the song, if this server has a copy.

What the repository holds (public, so no lyrics and no recording):
- a few short, well-known phrases from the song, to recognize out of the box;
- learned hashes: `python -m wepa_monitor sandman-learn lyrics.txt` reads a lyrics file you supply and stores
  only salted SHA-256 hashes of every three-word run in records/sandman.json. The text itself is never saved.
- the audio: WEPA_SANDMAN_AUDIO=/path/to/file.mp3, or a file named sandman.mp3 / .m4a / .ogg in the data
  folder. Use a copy you're entitled to play (e.g. one you bought). It is served only to signed-in users.
- otherwise, the official upload on YouTube in a small mini-player (YouTube's embed rules: the player stays
  visible and at least 200 x 200 px, so it's never hidden or audio-only). It uses youtube-nocookie.com and
  loads only when the egg fires. WEPA_SANDMAN_YOUTUBE=<video id> picks another video; =0 turns it off.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

SALT = "spo-sandman-1"
WINDOW = 3
# Short, widely quoted lines, so the egg works without a lyrics file.
PHRASES = ("exit light enter night", "take my hand", "off to never never land")
AUDIO_NAMES = ("sandman.mp3", "sandman.m4a", "sandman.ogg")
YOUTUBE_ID = "CD-E-LDc384"   # "Enter Sandman (Official Music Video)", Metallica's channel; the Topic upload blocks embedding
BANNER = "Exit light. Enter night."

VOICE = (
    "VOICE: The person found the Sandman easter egg, so answer in the voice of a 90s thrash-metal frontman, in "
    "the spirit of James Hetfield's stage banter: short punchy lines, the odd \"Yeah!\" or \"Yeah-heah!\", "
    "imagery of riffs, thunder, the pit and the road crew. Don't claim to be James Hetfield or any real person, "
    "don't quote song lyrics, keep it workplace-friendly with no profanity. Every rule above still holds: the "
    "same facts, figures and caution. Only the voice changes, never the substance.")


def words(text: str) -> list[str]:
    text = (text or "").lower().replace("’", "'").replace("'", "")
    return re.findall(r"[a-z0-9]+", text)


def _hash(ws: list[str] | tuple[str, ...]) -> str:
    return hashlib.sha256((SALT + " " + " ".join(ws)).encode()).hexdigest()[:24]


def shingles(text: str) -> set[str]:
    ws = words(text)
    return {_hash(ws[i:i + WINDOW]) for i in range(len(ws) - WINDOW + 1)}


def _store(data_dir: Path | None) -> Path | None:
    return Path(data_dir) / "records" / "sandman.json" if data_dir else None


def known(data_dir: Path | None) -> set[str]:
    out = set().union(*(shingles(p) for p in PHRASES))
    path = _store(data_dir)
    if path and path.exists():
        try:
            out |= set(json.loads(path.read_text()).get("hashes", []))
        except (OSError, ValueError):
            pass
    return out


def matches(text: str, data_dir: Path | None) -> bool:
    """True when the text contains three or more consecutive words of the song."""
    return bool(shingles(text) & known(data_dir))


def learn(data_dir: Path, lyrics: str) -> int:
    """Store hashes of every three-word run in the lyrics (never the lyrics). Returns how many are known now."""
    hashes = shingles(lyrics)
    path = _store(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = set()
    if path.exists():
        try:
            old = set(json.loads(path.read_text()).get("hashes", []))
        except (OSError, ValueError):
            pass
    every = sorted(old | hashes)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"salt": SALT, "window": WINDOW, "hashes": every}))
    tmp.replace(path)
    return len(every)


def audio_file(data_dir: Path | None) -> Path | None:
    env = os.environ.get("WEPA_SANDMAN_AUDIO", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    for name in AUDIO_NAMES:
        p = Path(data_dir) / name if data_dir else None
        if p and p.is_file():
            return p
    return None


def youtube_id() -> str | None:
    vid = os.environ.get("WEPA_SANDMAN_YOUTUBE", YOUTUBE_ID).strip()
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else None


def install(server, data_dir: Path) -> None:
    """GET /_sandman/audio streams the configured file (signed-in users only, like every non-public path)."""
    from flask import abort, send_file

    @server.route("/_sandman/audio")
    def _sandman_audio():
        path = audio_file(data_dir)
        if path is None:
            abort(404)
        return send_file(path, conditional=True, max_age=3600)
