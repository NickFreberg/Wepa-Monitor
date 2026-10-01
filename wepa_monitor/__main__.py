"""Command line entry point.

    python -m wepa_monitor scrape              one scrape, appended to data/live
    python -m wepa_monitor collect             scrape every minute until stopped
    python -m wepa_monitor compact             convert finished days' CSV to parquet
    python -m wepa_monitor demo --days 120     generate synthetic history in data/demo
    python -m wepa_monitor dashboard [--demo]  run the web dashboard
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _scrape_once(data_dir: Path, archive_html: bool) -> bool:
    from . import scrape, store

    result = scrape.fetch()
    store.append_result(data_dir, result, result.html if archive_html else None)
    status = "ok" if result.ok else f"FAILED ({result.error})"
    print(f"{result.attempt_ts:%Y-%m-%d %H:%M:%S}Z  {len(result.records):>3} stations  {status}", flush=True)
    return result.ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wepa_monitor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, help="override the data directory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("scrape", "scrape once and append"),
                            ("collect", "scrape every minute until stopped")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--archive-html", action="store_true",
                       help="also keep a gzipped copy of each raw page (about 7 MB/day)")
    sub.add_parser("compact", help="compact finished days to parquet")
    demo = sub.add_parser("demo", help="generate synthetic demo history")
    demo.add_argument("--days", type=int, default=120)
    demo.add_argument("--seed", type=int, default=7)
    dash = sub.add_parser("dashboard", help="run the dashboard")
    dash.add_argument("--demo", action="store_true", help="use the synthetic demo data")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8050)
    dash.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "scrape":
        return 0 if _scrape_once(args.data_dir or config.LIVE_DATA_DIR, args.archive_html) else 1

    if args.cmd == "collect":
        data_dir = args.data_dir or config.LIVE_DATA_DIR
        print(f"Collecting every {config.EXPECTED_INTERVAL_S}s into {data_dir} (Ctrl+C to stop)")
        last_compact = None
        try:
            while True:
                _scrape_once(data_dir, args.archive_html)
                today = datetime.now(timezone.utc).date()
                if last_compact != today:
                    from . import store
                    store.compact(data_dir)
                    last_compact = today
                # Sleep to the next minute boundary so snapshots stay evenly spaced.
                time.sleep(config.EXPECTED_INTERVAL_S - time.time() % config.EXPECTED_INTERVAL_S)
        except KeyboardInterrupt:
            print("stopped")
        return 0

    if args.cmd == "compact":
        from . import store
        done = store.compact(args.data_dir or config.LIVE_DATA_DIR)
        print(f"compacted {len(done)} file(s)")
        return 0

    if args.cmd == "demo":
        from . import synth
        data_dir = args.data_dir or config.DEMO_DATA_DIR
        print(f"Generating {args.days} days of synthetic data into {data_dir} ...")
        t0 = time.time()
        meta = synth.generate(data_dir, days=args.days, seed=args.seed, progress=lambda m: None)
        print(f"done: {meta['rows']:,} snapshot rows in {time.time() - t0:.0f}s")
        return 0

    if args.cmd == "dashboard":
        from .dashboard.app import create_app
        data_dir = args.data_dir or (config.DEMO_DATA_DIR if args.demo else config.LIVE_DATA_DIR)
        app = create_app(data_dir)
        app.run(host=args.host, port=args.port, debug=args.debug)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
