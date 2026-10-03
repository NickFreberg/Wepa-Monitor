"""Weather vs jams: finds a real humidity effect, and isn't fooled when humid hours are just quiet hours."""
from __future__ import annotations

import numpy as np
import pandas as pd

from wepa_monitor import weather


def _world(monkeypatch, effect: float, humid_at_night: bool, seed: int = 1):
    rng = np.random.default_rng(seed)
    hours = pd.date_range("2026-09-01", periods=24 * 40, freq="h", tz="UTC")
    local_h = hours.tz_convert("America/New_York").hour
    busy = np.where((local_h >= 9) & (local_h <= 21), 20.0, 2.0)          # toner points per hour
    if humid_at_night:   # confounding: humid hours are mostly quiet night hours
        hum = np.where(busy < 5, rng.uniform(75, 95, len(hours)), rng.uniform(30, 60, len(hours)))
    else:
        hum = rng.uniform(30, 95, len(hours))
    rate = 0.02 * busy * np.where(hum >= 70, effect, 1.0)
    jams = rng.poisson(rate)
    use = pd.DataFrame({"scrape_ts": hours, "component": "toner_k", "used": busy, "station_id": "a"})
    flt = pd.DataFrame({"start": np.repeat(hours, jams), "code": "paper_jam", "station_id": "a"})
    monkeypatch.setattr(weather.M, "usage_in", lambda *_a, **_k: use)
    monkeypatch.setattr(weather.M, "_in", lambda *_a, **_k: flt)
    wx = pd.DataFrame({"hour": hours, "humidity": hum, "temp_c": 15.0})

    class DS:
        fault_inc = flt
        data_dir = "."
    return weather.jams_vs_humidity(DS(), hours[0], hours[-1], wx=wx)


def test_finds_a_real_humidity_effect(monkeypatch):
    r = _world(monkeypatch, effect=2.0, humid_at_night=False)
    assert r["status"] == "ok" and r["verdict"] == "humid" and r["ci"][0] > 1.3
    assert "more jams" in weather.sentence(r)


def test_quiet_humid_nights_are_not_mistaken_for_an_effect(monkeypatch):
    r = _world(monkeypatch, effect=1.0, humid_at_night=True)
    assert r["status"] == "ok" and r["verdict"] == "none" and r["ci"][0] < 1 < r["ci"][1]
    assert "No clear link" in weather.sentence(r)


def test_says_when_there_is_not_enough(monkeypatch):
    class DS:
        data_dir = "/nonexistent"
    assert "No weather data" in weather.sentence(weather.jams_vs_humidity(DS(), None, None))
