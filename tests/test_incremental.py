"""The incremental refresh paths must give exactly what a full recompute gives."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from wepa_monitor import consumables, metrics, rollup, store, synth


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    d = tmp_path_factory.mktemp("inc")
    synth.generate(d, days=6, end=datetime(2026, 9, 12, 15, 37, tzinfo=timezone.utc), seed=3,
                   progress=lambda m: None)
    rs = rollup.RollupStore(d, metrics.building_map())
    return d, rs, metrics.load(d, rollups=rs)


def test_usage_matches_full_recompute(loaded):
    d, rs, ds = loaded
    pts = rs.refresh()["cons"].sort_values(["station_id", "component", "scrape_ts"], kind="stable",
                                           ignore_index=True)
    full, repl = consumables.usage(pts)
    key = ["station_id", "component", "scrape_ts", "level"]
    a, b = full.sort_values(key, ignore_index=True), ds.cons.sort_values(key, ignore_index=True)
    assert len(a) == len(b)
    assert np.allclose(a["used"], b["used"]) and (a["life"].to_numpy() == b["life"].to_numpy()).all()
    assert len(repl) == len(ds.repl)
    assert ds.cons["scrape_ts"].is_monotonic_increasing          # required by usage_in's binary search


def test_usage_in_matches_a_scan(loaded):
    _, _, ds = loaded
    s, e = ds.as_of - pd.Timedelta(days=2, hours=5), ds.as_of - pd.Timedelta(hours=3)
    scan = ds.cons[(ds.cons["scrape_ts"] >= s) & (ds.cons["scrape_ts"] < e)]
    assert metrics.usage_in(ds, s, e)["used"].sum() == pytest.approx(scan["used"].sum())
    ids = ds.stations["station_id"].tolist()[:3]
    assert len(metrics.usage_in(ds, s, e, ids)) == scan["station_id"].isin(ids).sum()


def test_cached_scrape_log_matches_disk(loaded):
    d, _, _ = loaded
    first = store.load_scrape_log(d)
    again = store.load_scrape_log(d)              # served from the per-process cache
    assert first.equals(again) and first["attempt_ts"].is_monotonic_increasing
