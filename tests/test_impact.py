"""Busy-weighted downtime: the same down hour counts more on a busy printer at a busy time."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from wepa_monitor import impact, metrics

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


def test_weights_follow_usage(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    ds = metrics.load(tmp_path, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))
    shape = pd.Series(0.1, index=range(168))
    shape[2 * 24 + 12] = 3.0                                         # Wednesday noon is busy
    a, b = ds.stations["station_id"][:2]
    scale = pd.Series({a: 2.0, b: 0.5})
    monkeypatch.setattr(impact, "weights", lambda _ds: (shape, scale))
    noon = pd.Timestamp("2026-09-30 12:00", tz="America/New_York").tz_convert("UTC")
    night = pd.Timestamp("2026-09-30 03:00", tz="America/New_York").tz_convert("UTC")
    hrs = pd.DataFrame({"station_id": [a, b], "hour": [noon, night], "covered_s": [3600, 3600], "up_s": [0, 0]})
    monkeypatch.setattr(impact.M, "_hours", lambda *_a, **_k: hrs)
    t = impact.by_station(ds, noon, noon).set_index("station_id")
    assert t.loc[a, "down_h"] == t.loc[b, "down_h"] == 1.0
    assert t.loc[a, "weighted_h"] == 6.0 and abs(t.loc[b, "weighted_h"] - 0.05) < 1e-9
    assert t.index[0] == a and "lost the most printing" in impact.summary(t.reset_index())
