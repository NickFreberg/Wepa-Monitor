"""Production entry point: `gunicorn wepa_monitor.wsgi:server`.

Serves the dashboard and, unless WEPA_COLLECT=0, runs the every-minute collector
in a background thread of the same process. Run gunicorn with ONE worker
(threads are fine), so there is one collector. A lock file in the data directory
also stops a second copy of the app (e.g. during an Azure redeploy) from
collecting at the same time; any brief overlap is deduplicated on load anyway.

Environment:
    WEPA_DATA_DIR   where snapshots live (Azure: /home/data/live, which persists)
    WEPA_COLLECT    1 (default) to collect in this process, 0 for dashboard only
"""
from __future__ import annotations

import fcntl
import os
import threading

from . import config
from .collector import collect_forever, scrape_once
from .dashboard.app import create_app

DATA_DIR = config.LIVE_DATA_DIR
_lock_handle = None


def _collector() -> None:
    try:
        scrape_once(DATA_DIR, quiet=True)
    except Exception as exc:  # noqa: BLE001
        print(f"first scrape failed: {exc}", flush=True)
    collect_forever(DATA_DIR, quiet=True)


def start_collector() -> bool:
    global _lock_handle
    if os.environ.get("WEPA_COLLECT", "1") != "1":
        return False
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _lock_handle = open(DATA_DIR / "collector.lock", "w")
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another process holds the collector lock; this one serves the dashboard only.", flush=True)
        return False
    except OSError:
        pass   # the file system doesn't support locks: collect anyway (duplicates are dropped on load)
    threading.Thread(target=_collector, name="collector", daemon=True).start()
    print(f"Collector running: one snapshot per minute into {DATA_DIR}", flush=True)
    return True


app = create_app(DATA_DIR)
server = app.server
start_collector()
