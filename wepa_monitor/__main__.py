"""Command line entry point.

    python -m wepa_monitor scrape              one scrape, appended to data/live
    python -m wepa_monitor collect             scrape every minute until stopped
    python -m wepa_monitor compact             convert finished days' CSV to parquet
    python -m wepa_monitor demo --days 120     generate synthetic history in data/demo
    python -m wepa_monitor dashboard [--demo]  run the web dashboard
    python -m wepa_monitor kml [--demo]        building pins with current status, for Google Earth
    python -m wepa_monitor start               collect + dashboard together, opens your browser
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser
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


def _collect_forever(data_dir: Path, archive_html: bool, quiet: bool = False) -> None:
    """Scrape on every minute boundary, compacting finished days once a day."""
    from . import store

    last_compact = None
    while True:
        # Sleep to the next minute boundary so snapshots stay evenly spaced.
        time.sleep(config.EXPECTED_INTERVAL_S - time.time() % config.EXPECTED_INTERVAL_S)
        if quiet:
            from . import scrape
            result = scrape.fetch()
            store.append_result(data_dir, result, result.html if archive_html else None)
            if not result.ok:
                print(f"{result.attempt_ts:%H:%M:%S}Z  scrape failed: {result.error}", flush=True)
        else:
            _scrape_once(data_dir, archive_html)
        today = datetime.now(timezone.utc).date()
        if last_compact != today:
            store.compact(data_dir)
            last_compact = today


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
    kml = sub.add_parser("kml", help="export building pins with current status as KML (Google Earth)")
    kml.add_argument("--demo", action="store_true", help="use the synthetic demo data")
    kml.add_argument("-o", "--output", type=Path, default=Path("bsu-print-stations.kml"))
    start = sub.add_parser("start", help="collect live data and run the dashboard in one window")
    start.add_argument("--port", type=int, default=8050)
    start.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
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
        try:
            _scrape_once(data_dir, args.archive_html)
            _collect_forever(data_dir, args.archive_html)
        except KeyboardInterrupt:
            print("stopped")
        return 0

    if args.cmd == "start":
        from .dashboard.app import create_app
        data_dir = args.data_dir or config.LIVE_DATA_DIR
        url = f"http://127.0.0.1:{args.port}"
        print("Taking the first snapshot ...", flush=True)
        _scrape_once(data_dir, False)
        threading.Thread(target=_collect_forever, args=(data_dir, False, True), daemon=True).start()
        logging.getLogger("werkzeug").setLevel(logging.WARNING)   # hide per-request log lines
        print(f"\nResNet Print Ops is running at {url}\n"
              "Collecting a snapshot every minute; leave this window open. Press Ctrl+C to stop.\n", flush=True)
        if not args.no_browser:
            threading.Timer(1.5, webbrowser.open, args=(url,)).start()
        try:
            create_app(data_dir).run(host="127.0.0.1", port=args.port)
        except KeyboardInterrupt:
            pass
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

    if args.cmd == "kml":
        from . import geo, metrics
        ds = metrics.load(args.data_dir or (config.DEMO_DATA_DIR if args.demo else config.LIVE_DATA_DIR))
        title = f"BSU print stations{' (DEMO DATA)' if ds.is_demo else ''}"
        args.output.write_text(geo.to_kml(geo.building_points(ds), title), encoding="utf-8")
        print(f"wrote {args.output} - open it in Google Earth Pro (File > Open) or import it from Projects in Google Earth on the web")
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
