"""Command line entry point.

    python -m wepa_monitor scrape              one scrape, appended to data/live
    python -m wepa_monitor collect             scrape every minute until stopped
    python -m wepa_monitor compact             convert finished days' CSV to parquet
    python -m wepa_monitor demo --days 120     generate synthetic history in data/demo
    python -m wepa_monitor dashboard [--demo]  run the web dashboard
    python -m wepa_monitor kml [--demo]        building pins with current status, for Google Earth
    python -m wepa_monitor start               collect + dashboard together, opens your browser
    python -m wepa_monitor campus [--force]    refresh calendar, hall and library data from bridgew.edu
    python -m wepa_monitor network             rebuild the campus walking/driving network from OpenStreetMap
    python -m wepa_monitor export --tag mac    package local data for import into another collector's folder
    python -m wepa_monitor backup --primary URL  stand by as a backup collector (takes over if the main one stops)
    python -m wepa_monitor selfcheck           the app's check of itself (add --vulns to look up packages now)
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import config
from .collector import collect_forever as _collect_forever, scrape_once as _scrape_once


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
    sub.add_parser("train-risk", help="train and evaluate the outage-risk model now")
    from . import cli_users, peer
    cli_users.add_parser(sub)
    peer.add_parser(sub)
    sc = sub.add_parser("selfcheck", help="check every part of the app: collector, data, records, security, versions")
    sc.add_argument("--vulns", action="store_true", help="also look up installed packages in OSV.dev now")
    sub.add_parser("network", help="rebuild the campus walking/driving network from OpenStreetMap")
    exp = sub.add_parser("export", help="package local data as import files for another collector's data folder")
    exp.add_argument("--tag", default="import", help="letters/digits naming this source, e.g. mac")
    exp.add_argument("-o", "--output", type=Path, default=Path("wepa-export"))
    camp = sub.add_parser("campus", help="refresh academic calendar, residence-hall, library and class-schedule data from bridgew.edu")
    camp.add_argument("--force", action="store_true", help="refresh even if the files are recent")
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
    dash.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
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
        print(f"\nBSU Student Printing Ops is running at {url}\n"
              "Collecting a snapshot every minute; leave this window open. Press Ctrl+C to stop.\n", flush=True)
        if not args.no_browser:
            threading.Timer(1.5, webbrowser.open, args=(url,)).start()
        try:
            create_app(data_dir, preload=True).run(host="127.0.0.1", port=args.port)
        except KeyboardInterrupt:
            pass
        print("stopped")
        return 0

    if args.cmd == "export":
        from . import store
        files = store.export_for_import(args.data_dir or config.LIVE_DATA_DIR, args.output, args.tag)
        print("\n".join(files) if files else "No local data to export.")
        print(f"\nWrote {len(files)} file(s) to {args.output}/ (upload its contents into the other data folder)")
        return 0

    if args.cmd == "network":
        from . import routing
        net = routing.fetch_network()
        print(f"campus network: {len(net['nodes']):,} points, {len(net['walk']):,} walking and "
              f"{len(net['drive']):,} driving segments, {len(net['parking'])} parking areas")
        return 0

    if args.cmd == "campus":
        from . import campus, courses
        result = campus.refresh(force=args.force)
        result["courses"] = courses.refresh(force=args.force)
        for name, outcome in result.items():
            print(f"{name}: {'up to date' if outcome == 'fresh' else outcome}")
        return 1 if any(v.startswith("failed") for v in result.values()) else 0

    if args.cmd == "compact":
        from . import store
        done = store.compact(args.data_dir or config.LIVE_DATA_DIR)
        print(f"compacted {len(done)} file(s)")
        return 0

    if args.cmd == "users":
        return cli_users.run(args)

    if args.cmd == "backup":
        return peer.run_cli(args)

    if args.cmd == "selfcheck":
        from . import metrics, selfcheck, vulns
        data_dir = args.data_dir or config.LIVE_DATA_DIR
        if args.vulns:
            vulns.refresh(data_dir, force=True)
        ds = metrics.load(data_dir) if (data_dir / "snapshots").exists() else None
        checks = selfcheck.run(ds, data_dir)
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "note"}
        for c in checks:
            print(f"  {mark[c.status]}  {c.name:<28} {c.detail}" + (f"\n        -> {c.fix}" if c.fix else ""))
        tone, sentence = selfcheck.summary(checks)
        print(f"\n{sentence}")
        return 1 if tone == "critical" else 0

    if args.cmd == "train-risk":
        from . import metrics, risk
        card = risk.train_and_save(metrics.load(args.data_dir or config.LIVE_DATA_DIR))
        for name, m in card.get("models", {}).items():
            if "brier" in m:
                print(f"  {risk.MODEL_NAMES.get(name, name):<42} Brier {m['brier']:.4f}  AUC {m['auc']:.3f}"
                      + (f"  skill {m['skill']:+.1%}" if "skill" in m else ""))
        return 0

    if args.cmd == "demo":
        from . import synth
        data_dir = args.data_dir or config.DEMO_DATA_DIR
        print(f"Generating {args.days} days of synthetic data into {data_dir} ...")
        t0 = time.time()
        meta = synth.generate(data_dir, days=args.days, seed=args.seed, progress=lambda m: None)
        print(f"done: {meta['rows']:,} snapshot rows in {time.time() - t0:.0f}s")
        print("\nNext, start the dashboard (it opens in your browser):\n"
              "    python -m wepa_monitor dashboard --demo")
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
        if not (data_dir / "snapshots").exists():
            hint = "python -m wepa_monitor demo" if args.demo else "python -m wepa_monitor start"
            print(f"No data in {data_dir} yet. Run `{hint}` first.")
            return 1
        print(f"Loading data from {data_dir} (this can take ~15 s for the 120-day demo) ...", flush=True)
        app = create_app(data_dir, preload=True)
        local = ("127.0.0.1", "0.0.0.0", "localhost")  # nosec B104 - comparing names, not binding
        url = f"http://127.0.0.1:{args.port}" if args.host in local else f"http://{args.host}:{args.port}"
        print(f"\nDashboard ready: open {url} in your browser. Press Ctrl+C to stop.\n", flush=True)
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        if not args.no_browser and not args.debug:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        try:
            app.run(host=args.host, port=args.port, debug=args.debug)
        except KeyboardInterrupt:
            pass
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
