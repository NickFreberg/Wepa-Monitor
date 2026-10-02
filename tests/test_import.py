"""Importing history collected elsewhere (e.g. a laptop) into a collector's data folder."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from wepa_monitor import metrics, rollup, store, synth

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


def _split_today(tmp_path):
    """'mac' has every minute so far; 'cloud' started later and has only the last few minutes."""
    mac, cloud = tmp_path / "mac", tmp_path / "cloud"
    shutil.copytree(FIXTURE, mac)
    shutil.copytree(FIXTURE, cloud)
    for sub in ("snapshots", "scrape_log"):
        f = next((cloud / sub).glob("*.csv"))
        df = pd.read_csv(f, dtype=str)
        ts = "scrape_ts" if sub == "snapshots" else "attempt_ts"
        keep = df[df[ts] >= sorted(df[ts].unique())[-3]]
        keep.to_csv(f, index=False)
    return mac, cloud


def test_import_merges_today_without_touching_live_file(tmp_path):
    mac, cloud = _split_today(tmp_path)
    full = store.load_day(mac, "2026-10-02")
    assert len(store.load_day(cloud, "2026-10-02")) < len(full)
    live_csv = (cloud / "snapshots" / "2026-10-02.csv").read_bytes()

    out = tmp_path / "export"
    written = store.export_for_import(mac, out, "mac")
    assert any("imports/snapshots/2026-10-02.mac.parquet" in w for w in written)
    shutil.copytree(out, cloud, dirs_exist_ok=True)

    assert store.snapshot_days(cloud) == ["2026-10-02"]                 # still one day, not two
    merged = store.load_day(cloud, "2026-10-02")
    assert len(merged) == len(full)                                     # overlap de-duplicated
    assert (cloud / "snapshots" / "2026-10-02.csv").read_bytes() == live_csv
    log = store.load_scrape_log(cloud)
    assert log["attempt_ts"].dt.floor("min").is_unique                  # one attempt per minute
    ds = metrics.load(cloud, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))
    assert ds.status["scrape_ts"].min() == full["scrape_ts"].min()      # history starts with the mac's


def test_import_into_finished_day_rebuilds_rollups(tmp_path):
    full, part = tmp_path / "full", tmp_path / "part"
    synth.generate(full, days=3, end=datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc), seed=2,
                   progress=lambda m: None)
    shutil.copytree(full, part)
    day = store.snapshot_days(part)[1]                                 # a finished middle day
    f = part / "snapshots" / f"{day}.parquet"
    df = pd.read_parquet(f)
    df[df["scrape_ts"] >= df["scrape_ts"].min() + pd.Timedelta(hours=6)].to_parquet(f, index=False)

    rs = rollup.RollupStore(part, metrics.building_map())
    before = metrics.load(part, rollups=rs).hourly
    want = metrics.load(full).hourly
    covered = lambda h: h[h["local_date"] == h["local_date"].iloc[0]]["covered_s"].sum()   # noqa: E731
    assert before["covered_s"].sum() < want["covered_s"].sum()

    out = tmp_path / "export"
    store.export_for_import(full, out, "laptop")
    (part / "imports" / "snapshots").mkdir(parents=True)
    for p in (out / "imports" / "snapshots").glob(f"{day}.*"):          # import just that day
        shutil.copy(p, part / "imports" / "snapshots" / p.name)

    after = metrics.load(part, rollups=rs).hourly                      # same process: noticed
    assert after["covered_s"].sum() == want["covered_s"].sum()
    fresh = metrics.load(part).hourly                                   # restart: disk cache rebuilt
    assert fresh["covered_s"].sum() == want["covered_s"].sum()
    assert covered(after) > 0


def test_import_of_a_day_the_collector_never_saw(tmp_path):
    """The laptop's earlier days (before the cloud existed) show up as days of their own."""
    mac, cloud = tmp_path / "mac", tmp_path / "cloud"
    shutil.copytree(FIXTURE, mac)
    (cloud / "snapshots").mkdir(parents=True)
    store.export_for_import(mac, tmp_path / "out", "mac")
    shutil.copytree(tmp_path / "out", cloud, dirs_exist_ok=True)
    assert store.snapshot_days(cloud) == ["2026-10-02"]
    assert len(store.load_day(cloud, "2026-10-02")) == len(store.load_day(mac, "2026-10-02"))
    assert not list((cloud / "snapshots").iterdir())                    # live folder untouched
