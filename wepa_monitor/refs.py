"""Permanent reference numbers for every outage, and the records folder they live in.

Every time a station goes out of service it gets a number, by its main cause:

    OUT#########   unreachable or unresponsive (the printer isn't talking to Wepa)
    JAM#########   jammed
    PAP#########   out of paper, or a tray disengaged
    SUP#########   consumable out: toner depleted or drum expired (warnings never get a number)
    ERR#########   hardware or system: system fault, service required, cover open, or no cause reported

(Investigations are INV#########, numbered in investigations.py from the same counter file.)

Numbers are assigned once and kept forever in an append-only registry (records/refs.jsonl), keyed by
station and start time. They are never reused or renumbered: if a detection rule is improved later
and an outage's start moves, its old number stays on record and the new reading gets a new one. One
outage gets one number, named for its most specific cause. A file lock makes assignment safe even
while two app revisions overlap during a rolling deploy.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pandas as pd

PREFIX = {"not_reachable": "OUT", "offline": "OUT", "paper_jam": "JAM", "paper_out": "PAP",
          "tray_missing": "PAP", "toner_empty": "SUP", "drum_end": "SUP"}
DEFAULT_PREFIX = "ERR"
KINDS = {"OUT": "Unreachable", "JAM": "Jammed", "PAP": "Paper", "SUP": "Consumable out", "ERR": "Hardware or system",
         "INV": "Investigation", "UPD": "Software update", "REQ": "Feature request",
         "STK": "Inventory", "KEY": "Kiosk key"}
DIGITS = 9

_lock = threading.Lock()
_cache: dict[str, dict] = {}          # records dir -> {"by_key": {...}, "counters": {...}, "mtime": float}


def records_dir(data_dir: Path) -> Path:
    """Records sit beside the live data folder (data/records), or inside a demo/test folder."""
    d = Path(data_dir)
    return d.parent / "records" if d.name == "live" else d / "records"


def fmt(prefix: str, n: int) -> str:
    return f"{prefix}{n:0{DIGITS}d}"


class _FileLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, *exc):
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self.f.close()


def _state(rd: Path) -> dict:
    path = rd / "refs.jsonl"
    st = _cache.setdefault(str(rd), {"by_key": {}, "counters": {}, "mtime": -1.0, "rows": []})
    m = path.stat().st_mtime if path.exists() else 0.0
    if m != st["mtime"]:
        by_key, counters, rows = {}, {}, []
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                rows.append(r)
                if r.get("key"):
                    by_key[r["key"]] = r["ref"]
                p, n = r["ref"][:3], int(r["ref"][3:])
                counters[p] = max(counters.get(p, 0), n)
        st.update(by_key=by_key, counters=counters, mtime=m, rows=rows)
    return st


def next_number(data_dir: Path, prefix: str, record: dict) -> str:
    """Reserve the next number for `prefix` (used for INV), recording what it was for."""
    rd = records_dir(data_dir)
    with _lock, _FileLock(rd / "refs.lock"):
        st = _state(rd)
        n = st["counters"].get(prefix, 0) + 1
        ref = fmt(prefix, n)
        with open(rd / "refs.jsonl", "a") as f:
            f.write(json.dumps({"ref": ref, "created": time.time(), **record}) + "\n")
        st["mtime"] = -1.0
        return ref


def _key(sid: str, start: pd.Timestamp) -> str:
    return f"{sid}|{pd.Timestamp(start).tz_convert('UTC').floor('s').isoformat()}"


def prefix_for(issue: str | None) -> str:
    return PREFIX.get(issue or "", DEFAULT_PREFIX)


def sync(ds) -> pd.Series:
    """Give every outage (red incident) its number, assigning new ones as needed. Returns the refs
    aligned with ds.sev_inc (empty string for warnings, which never get a number)."""
    from . import narrative as N
    inc = ds.sev_inc
    if inc is None or inc.empty:
        return pd.Series(dtype=str)
    rd = records_dir(ds.data_dir)
    red = inc[inc["severity"] == "red"]
    keys = {i: _key(s, t) for i, s, t in zip(red.index, red["station_id"], red["start"])}
    with _lock:
        st = _state(rd)
        missing = [i for i, k in keys.items() if k not in st["by_key"]]
    if missing:
        with _lock, _FileLock(rd / "refs.lock"):
            st = _state(rd)
            new_rows = []
            for i in sorted(missing, key=lambda i: red.at[i, "start"]):
                k = keys[i]
                if k in st["by_key"]:
                    continue
                causes = N.outage_causes(ds, red.at[i, "station_id"], red.at[i, "start"])
                issue = causes[0][0] if causes else None
                p = prefix_for(issue)
                n = st["counters"].get(p, 0) + 1
                st["counters"][p] = n
                ref = fmt(p, n)
                st["by_key"][k] = ref
                new_rows.append({"ref": ref, "key": k, "station_id": red.at[i, "station_id"],
                                 "start": pd.Timestamp(red.at[i, "start"]).isoformat(), "issue": issue or "",
                                 "created": time.time()})
            if new_rows:
                with open(rd / "refs.jsonl", "a") as f:
                    f.writelines(json.dumps(r) + "\n" for r in new_rows)
                st["mtime"] = (rd / "refs.jsonl").stat().st_mtime
                st["rows"] += new_rows
    with _lock:
        st = _state(rd)
        out = pd.Series("", index=inc.index, dtype=object)
        for i, k in keys.items():
            out.at[i] = st["by_key"].get(k, "")
    return out


def lookup(data_dir: Path, ref: str) -> dict | None:
    with _lock:
        st = _state(records_dir(data_dir))
        return next((r for r in st["rows"] if r.get("ref") == ref), None)
