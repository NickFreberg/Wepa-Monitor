"""Outage-risk model: honest features, a fair walk-forward test, and a gate it must pass to be shown."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wepa_monitor import metrics, risk

FIXTURE = Path(__file__).parent / "fixtures" / "live_first_minutes"


@pytest.fixture()
def ds(tmp_path):
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    return metrics.load(tmp_path, now=datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc))


def test_short_history_stays_learning_and_shows_nothing(ds):
    card = risk.train_and_save(ds, log=lambda m: None)
    assert card["status"] == "learning" and card["reasons"]
    assert risk.load_card(ds)["status"] == "learning"
    assert risk.predict_now(ds).table.empty


def _world(days: int, signal: float, seed: int = 0) -> pd.DataFrame:
    """A made-up feature table: 20 printers x hourly rows; outages depend on jams_7d by `signal`."""
    rng = np.random.default_rng(seed)
    hours = pd.date_range("2026-01-01", periods=days * 24, freq="h", tz="UTC")
    rows = []
    for k in range(20):
        jams = rng.poisson(1.0, len(hours)).astype(float)
        p = 1 / (1 + np.exp(-(-2.6 + signal * (jams - 1))))
        f = pd.DataFrame({"ts": hours, "station_id": f"{k:05d}", "label": (rng.random(len(hours)) < p).astype(float),
                          "labelled": True})
        for c in risk.FEATURES:
            f[c] = rng.normal(size=len(hours))
        f["jams_7d"] = jams
        f["past_rate"] = 0.1 + rng.random() * 0.01
        rows.append(f)
    return pd.concat(rows, ignore_index=True)


def test_goes_live_only_when_it_beats_the_baseline(ds, monkeypatch):
    monkeypatch.setattr(risk, "_candidates", lambda: {k: v for k, v in _all().items() if k != "neural"})
    monkeypatch.setattr(risk, "build", lambda *_a, **_k: _world(40, signal=1.5))
    card = risk.train(ds)
    assert card["status"] == "live", card["reasons"]
    assert card["models"][card["champion"]]["skill"] > 0.02 and card["importance"][0]["group"] == "faults"

    monkeypatch.setattr(risk, "build", lambda *_a, **_k: _world(40, signal=0.0))
    card = risk.train(ds)
    assert card["status"] == "learning" and any("baseline" in r or "AUC" in r for r in card["reasons"])


_ALL = risk._candidates


def _all():
    return _ALL()
