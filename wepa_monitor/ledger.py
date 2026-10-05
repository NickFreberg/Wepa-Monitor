"""An append-only, hash-chained event log: the evidence trail behind inventory and the key log.

Every change is one JSON line: who, when, what, and why. Nothing is edited or deleted; a correction is a new
event. Each event carries the SHA-256 of the one before it, so an edit anywhere in the file breaks the chain
and verify() says where (the same scheme as investigations.py). A file lock keeps two app revisions from
writing at once during a rolling deploy.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from . import refs

GENESIS = "0" * 64
_lock = threading.Lock()


class Ledger:
    def __init__(self, data_dir, name: str):
        self.dir = refs.records_dir(data_dir)
        self.path = self.dir / f"{name}.jsonl"
        self.lockfile = self.dir / f"{name}.lock"

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    @staticmethod
    def _canonical(e: dict) -> str:
        return json.dumps({k: v for k, v in e.items() if k != "hash"}, sort_keys=True, separators=(",", ":"),
                          default=str)

    def append(self, user: dict, action: str, data: dict, note: str = "") -> dict:
        with _lock, refs._FileLock(self.lockfile):
            events = self.read()
            prev = events[-1]["hash"] if events else GENESIS
            e = {"seq": len(events) + 1, "ts": time.time(), "user": user.get("username", "system"),
                 "user_name": user.get("name", user.get("username", "system")), "action": action, "data": data,
                 "note": (note or "").strip()[:1000], "prev": prev}
            e["hash"] = hashlib.sha256(self._canonical(e).encode()).hexdigest()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(e, default=str) + "\n")
            return e

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        events = self.read()
        for i, e in enumerate(events, 1):
            if e.get("prev") != prev:
                return False, f"Chain broken at entry {i}: it doesn't follow the entry before it."
            if hashlib.sha256(self._canonical(e).encode()).hexdigest() != e.get("hash"):
                return False, f"Entry {i} was changed after it was written."
            prev = e["hash"]
        return True, f"All {len(events):,} entries are intact."
