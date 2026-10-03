"""The every-minute collector, shared by the CLI (`collect`, `start`) and the production server."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from . import courses, campus, config, scrape, store


def scrape_once(data_dir: Path, archive_html: bool = False, quiet: bool = False) -> bool:
    """One scrape, appended to the data directory. Failures are logged as data, never raised."""
    result = scrape.fetch()
    store.append_result(data_dir, result, result.html if archive_html else None)
    if not quiet or not result.ok:
        status = "ok" if result.ok else f"FAILED ({result.error})"
        print(f"{result.attempt_ts:%Y-%m-%d %H:%M:%S}Z  {len(result.records):>3} stations  {status}", flush=True)
    return result.ok


def _refresh_weather(data_dir: Path) -> None:
    """Daily: cache any missing hours of Bridgewater weather (for the humidity vs jams analysis)."""
    try:
        import pandas as pd

        from . import metrics, weather
        ds = metrics.load(data_dir)
        if ds.data_start is not None:
            weather.refresh(data_dir, ds.data_start, pd.Timestamp.now(tz="UTC"),
                            log=lambda m: print(m, flush=True))
    except Exception as exc:  # noqa: BLE001 - optional context
        print(f"weather refresh skipped: {type(exc).__name__}: {exc}", flush=True)


def _retrain_risk(data_dir: Path) -> None:
    """Nightly: retrain the outage-risk model in the background (it decides whether it's good enough)."""
    try:
        from . import metrics, risk
        ds = metrics.load(data_dir)
        risk.ensure_fresh(ds, log=lambda m: print(m, flush=True))
    except Exception as exc:  # noqa: BLE001 - optional; the collector carries on
        print(f"outage-risk retrain skipped: {type(exc).__name__}: {exc}", flush=True)


def collect_forever(data_dir: Path, archive_html: bool = False, quiet: bool = False) -> None:
    """Scrape on every minute boundary; once a day, compact finished days and refresh campus data."""
    last_compact = None
    while True:
        # Sleep to the next minute boundary so snapshots stay evenly spaced.
        time.sleep(config.EXPECTED_INTERVAL_S - time.time() % config.EXPECTED_INTERVAL_S)
        try:
            scrape_once(data_dir, archive_html, quiet)
            today = datetime.now(timezone.utc).date()
            if last_compact != today:
                store.compact(data_dir)
                # Campus context from bridgew.edu: a no-op unless something is due
                # (academic calendar yearly, library hours weekly); failures keep the old files.
                campus.refresh(log=lambda m: print(m, flush=True))
                courses.refresh(log=lambda m: print(m, flush=True))      # monthly; a no-op otherwise
                _refresh_weather(data_dir)
                _retrain_risk(data_dir)
                last_compact = today
        except Exception as exc:  # noqa: BLE001 - a disk hiccup must not kill the collector thread
            print(f"collector error: {type(exc).__name__}: {exc}", flush=True)
