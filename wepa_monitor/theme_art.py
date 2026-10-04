"""Artwork a theme can use that this public repository doesn't ship.

The Cup theme's background is the 90s paper-cup design. It belongs to its owner, so the repository only has a
drawn stand-in (assets/cup-art.svg). An operator with the right to use the real image can drop it in the data
folder as cup.png / cup.jpg / cup.webp, or point WEPA_CUP_IMAGE at it. GET /_theme/cup serves that file to
signed-in users, or the stand-in when there is none.
"""
from __future__ import annotations

import os
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "dashboard" / "assets"
CUP_NAMES = ("cup.png", "cup.jpg", "cup.jpeg", "cup.webp")


def _find(data_dir: Path | None, env: str, names: tuple[str, ...]) -> Path | None:
    value = os.environ.get(env, "").strip()
    if value:
        p = Path(value)
        return p if p.is_file() else None
    for name in names:
        p = Path(data_dir) / name if data_dir else None
        if p and p.is_file():
            return p
    return None


def cup_image(data_dir: Path | None) -> Path | None:
    return _find(data_dir, "WEPA_CUP_IMAGE", CUP_NAMES)


def install(server, data_dir: Path) -> None:
    from flask import send_file

    @server.route("/_theme/cup")
    def _theme_cup():
        path = cup_image(data_dir) or ASSETS / "cup-art.svg"
        return send_file(path, conditional=True, max_age=3600)
