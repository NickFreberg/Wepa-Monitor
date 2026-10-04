"""The app's own events (software updates, vulnerabilities, backup collectors), for the activity feed.

Append-only, one JSON object per line in records/system_events.jsonl, beside the reference numbers
and investigations. Like every other record they are never edited or deleted.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from .refs import _FileLock, records_dir

KINDS = {
    # kind: (label, severity)
    "software_updated": ("Software updated", "good"),
    "update_requested": ("Update requested", "info"),
    "update_prepared": ("Update package prepared", "info"),
    "vuln_found": ("Vulnerability found", "warning"),
    "vuln_resolved": ("Vulnerability fixed", "good"),
    "backup_active": ("Backup collector took over", "warning"),
    "backup_handover": ("Main collector back", "good"),
    "backup_updated": ("Backup collector updated", "info"),
}

_root: Path | None = None


def configure(data_dir: Path) -> None:
    """Where events are written when no data directory is passed (the dashboard sets this once)."""
    global _root
    _root = records_dir(data_dir)


def _path(data_dir: Path | None) -> Path | None:
    rd = records_dir(data_dir) if data_dir is not None else _root
    return rd / "system_events.jsonl" if rd is not None else None


def add(kind: str, title: str, detail: str = "", href: str = "", data_dir: Path | None = None,
        ts: float | None = None, **extra) -> dict | None:
    path = _path(data_dir)
    if path is None:
        return None
    label, severity = KINDS.get(kind, (kind, "info"))
    row = {"ts": ts if ts is not None else time.time(), "kind": kind, "severity": extra.pop("severity", severity),
           "title": title, "detail": detail, "href": href, **extra}
    with _FileLock(path.with_suffix(".lock")):
        with open(path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    return row


def load(data_dir: Path | None = None) -> list[dict]:
    path = _path(data_dir)
    if path is None or not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def frame(data_dir: Path | None = None, since: pd.Timestamp | None = None) -> pd.DataFrame:
    rows = load(data_dir)
    df = pd.DataFrame(rows, columns=["ts", "kind", "severity", "title", "detail", "href"]) if rows else \
        pd.DataFrame(columns=["ts", "kind", "severity", "title", "detail", "href"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    if since is not None:
        df = df[df["ts"] >= since]
    return df.fillna("")
