"""Shared outages: a real network blip across buildings is flagged; independent outages are not."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from wepa_monitor import correlated, metrics

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


def _ds(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    return metrics.load(tmp_path, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))


def _plant(ds, n, code, gap_min=1.0):
    sids = list(ds.stations.drop_duplicates("building")["station_id"][:n])
    t0 = ds.data_start + pd.Timedelta(minutes=1)
    sev, flt = [], []
    for k, sid in enumerate(sids):
        s = t0 + pd.Timedelta(minutes=gap_min * k)
        e = t0 + pd.Timedelta(minutes=30 + k)
        sev.append({"station_id": sid, "severity": "red", "start": s, "end": e, "duration_s": (e - s).total_seconds(),
                    "censored_start": False})
        flt.append({"station_id": sid, "start": s, "end": e, "code": code, "detail": "", "severity": "red",
                    "duration_s": (e - s).total_seconds(), "censored_start": False})
    ds.sev_inc = pd.concat([ds.sev_inc, pd.DataFrame(sev)], ignore_index=True)
    ds.fault_inc = pd.concat([ds.fault_inc, pd.DataFrame(flt)], ignore_index=True)
    return sids


def test_network_blip_across_buildings_is_flagged(tmp_path):
    ds = _ds(tmp_path)
    _plant(ds, 4, "not_reachable", gap_min=0.5)
    c = correlated.clusters(ds, ds.data_start, ds.data_start + pd.Timedelta(days=7))
    assert len(c) == 1
    r = c.iloc[0]
    assert r["stations"] == 4 and r["kind"] == "Network or Wepa service" and r["together"]
    assert r["chance_windows"] < 0.05 and "unlikely" in r["verdict"]


def test_spread_out_outages_are_not_clustered(tmp_path):
    ds = _ds(tmp_path)
    _plant(ds, 4, "not_reachable", gap_min=20)
    assert correlated.clusters(ds, ds.data_start, ds.data_start + pd.Timedelta(days=7)).empty
