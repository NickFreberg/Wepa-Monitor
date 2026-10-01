"""The every-minute collector, shared by the CLI (`collect`, `start`) and the production server."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, scrape, store


def scrape_once(data_dir: Path, archive_html: bool = False, quiet: bool = False) -> bool:
    """One scrape, appended to the data directory. Failures are logged as data, never raised."""
    result = scrape.fetch()
    store.append_result(data_dir, result, result.html if archive_html else None)
    if not quiet or not result.ok:
        status = "ok" if result.ok else f"FAILED ({result.error})"
        print(f"{result.attempt_ts:%Y-%m-%d %H:%M:%S}Z  {len(result.records):>3} stations  {status}", flush=True)
    return result.ok


def collect_forever(data_dir: Path, archive_html: bool = False, quiet: bool = False) -> None:
    """Scrape on every minute boundary, compacting finished days once a day."""
    last_compact = None
    while True:
        # Sleep to the next minute boundary so snapshots stay evenly spaced.
        time.sleep(config.EXPECTED_INTERVAL_S - time.time() % config.EXPECTED_INTERVAL_S)
        try:
            scrape_once(data_dir, archive_html, quiet)
            today = datetime.now(timezone.utc).date()
            if last_compact != today:
                store.compact(data_dir)
                last_compact = today
        except Exception as exc:  # noqa: BLE001 - a disk hiccup must not kill the collector thread
            print(f"collector error: {type(exc).__name__}: {exc}", flush=True)
