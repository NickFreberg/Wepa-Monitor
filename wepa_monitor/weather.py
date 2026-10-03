"""Does humid weather go with more paper jams? Outdoor weather from Open-Meteo vs the jams we record.

Paper absorbs moisture and curls, a well-known cause of jams, so it's a fair question. The answer
must come from the data: humid hours are compared with dry hours on jams per unit of printing (toner
used), within the same time of day and weekday/weekend (Mantel-Haenszel rate ratio), so a humid
afternoon is compared with dry afternoons, not with dry nights when nobody prints.

Caveats, stated wherever the result is shown: weather is outdoor (heating and air conditioning
change indoor humidity), it's one campus-wide reading, and an association is not proof of a cause.

Data: Open-Meteo (open-meteo.com, free, CC BY 4.0), hourly relative humidity and temperature for
Bridgewater. The historical archive (ERA5 reanalysis) lags a few days, so recent days come from the
forecast endpoint's past days. Cached in <data>/weather/hourly.parquet; refreshed daily by the
collector, fetching only missing days.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import config, metrics as M

LAT, LON = 41.9904, -70.9751          # Bridgewater State University
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
RECENT = "https://api.open-meteo.com/v1/forecast"
VARS = "relative_humidity_2m,temperature_2m"
MIN_DAYS, MIN_JAMS = 14, 30
JAM_CODES = ("paper_jam",)


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / "weather" / "hourly.parquet"


def load(data_dir: Path) -> pd.DataFrame:
    p = _path(data_dir)
    try:
        return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=["hour", "humidity", "temp_c"])
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["hour", "humidity", "temp_c"])


def _get(url: str, params: dict) -> pd.DataFrame:
    r = requests.get(url, params={**params, "latitude": LAT, "longitude": LON, "hourly": VARS, "timezone": "UTC"},
                     timeout=30, headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    h = r.json()["hourly"]
    return pd.DataFrame({"hour": pd.to_datetime(h["time"], utc=True), "humidity": h["relative_humidity_2m"],
                         "temp_c": h["temperature_2m"]}).dropna()


def refresh(data_dir: Path, start: pd.Timestamp, end: pd.Timestamp, log=print) -> str:
    """Fetch hours between start and end that aren't cached yet. Failures keep the old cache."""
    have = load(data_dir)
    hours = pd.date_range(start.floor("h"), end.floor("h") - pd.Timedelta(hours=1), freq="h")
    missing = hours.difference(pd.DatetimeIndex(have["hour"])) if len(have) else hours
    if len(missing) == 0:
        return "fresh"
    first, last = missing.min(), missing.max()
    frames = []
    try:
        cutoff = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=6)
        if first < cutoff:
            frames.append(_get(ARCHIVE, {"start_date": f"{first:%Y-%m-%d}",
                                         "end_date": f"{min(last, cutoff - pd.Timedelta(days=1)):%Y-%m-%d}"}))
        if last >= cutoff:
            frames.append(_get(RECENT, {"past_days": 14, "forecast_days": 1}))
    except Exception as exc:  # noqa: BLE001 - weather is optional context
        log(f"weather: fetch failed ({type(exc).__name__}: {exc}); keeping the cached hours")
        return "failed"
    new = pd.concat([have] + frames, ignore_index=True)
    new = new[new["hour"] <= pd.Timestamp.now(tz="UTC")].drop_duplicates("hour", keep="last").sort_values("hour")
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    new.to_parquet(p, index=False)
    log(f"weather: cached {len(new):,} hours")
    return "updated"


def jams_vs_humidity(ds: M.Dataset, start, end, ids=None, wx: pd.DataFrame | None = None) -> dict | None:
    """Jams per 100 toner points in humid vs dry hours, stratified by time of day and weekend."""
    wx = load(ds.data_dir) if wx is None else wx
    if wx.empty:
        return {"status": "no_weather"}
    use = M.usage_in(ds, start, end, ids)
    use = use[use["component"] == "toner_k"]
    use = use.assign(hour=use["scrape_ts"].dt.floor("h")).groupby("hour")["used"].sum()
    f = M._in(ds.fault_inc, "start", start, end, ids)
    jams = f[f["code"].isin(JAM_CODES)]
    jams = jams.assign(hour=jams["start"].dt.floor("h")).groupby("hour").size()
    d = pd.DataFrame({"used": use}).join(jams.rename("jams"), how="outer").fillna(0.0)
    d = d.join(wx.set_index("hour")[["humidity", "temp_c"]], how="inner")
    d = d[d["used"] > 0]
    days = d.index.normalize().nunique() if len(d) else 0
    if days < MIN_DAYS or d["jams"].sum() < MIN_JAMS:
        return {"status": "not_enough", "days": int(days), "jams": int(d["jams"].sum()),
                "need_days": MIN_DAYS, "need_jams": MIN_JAMS}
    lo, hi = d["humidity"].quantile([1 / 3, 2 / 3])
    d["band"] = np.where(d["humidity"] <= lo, "dry", np.where(d["humidity"] >= hi, "humid", "middle"))
    loc = d.index.tz_convert(config.LOCAL_TZ)
    d["stratum"] = (loc.hour // 6).astype(str) + np.where(loc.dayofweek >= 5, "we", "wd")
    bands = (d.groupby("band").agg(jams=("jams", "sum"), used=("used", "sum"), hours=("jams", "size"),
                                   humidity=("humidity", "mean"))
             .reindex(["dry", "middle", "humid"]).reset_index())
    bands["per100"] = bands["jams"] / bands["used"] * 100
    # Mantel-Haenszel rate ratio, humid vs dry, with Greenland-Robins variance.
    num = den = var_num = 0.0
    for _, g in d[d["band"].isin(["dry", "humid"])].groupby("stratum"):
        a, t1 = g.loc[g["band"] == "humid", "jams"].sum(), g.loc[g["band"] == "humid", "used"].sum()
        b, t0 = g.loc[g["band"] == "dry", "jams"].sum(), g.loc[g["band"] == "dry", "used"].sum()
        t = t1 + t0
        if t1 <= 0 or t0 <= 0:
            continue
        num += a * t0 / t
        den += b * t1 / t
        var_num += (a + b) * t1 * t0 / t ** 2
    if num <= 0 or den <= 0:
        return {"status": "not_enough", "days": int(days), "jams": int(d["jams"].sum()),
                "need_days": MIN_DAYS, "need_jams": MIN_JAMS}
    rr = num / den
    se = np.sqrt(var_num / (num * den))
    ci = (float(np.exp(np.log(rr) - 1.96 * se)), float(np.exp(np.log(rr) + 1.96 * se)))
    if ci[0] > 1:
        verdict = "humid"
    elif ci[1] < 1:
        verdict = "dry"
    else:
        verdict = "none"
    return {"status": "ok", "rr": float(rr), "ci": ci, "verdict": verdict, "bands": bands, "dry_max": float(lo),
            "humid_min": float(hi), "days": int(days), "jams": int(d["jams"].sum()), "hours": int(len(d))}


def sentence(r: dict | None) -> str:
    if not r or r.get("status") == "no_weather":
        return "No weather data cached yet (it's fetched once a day from Open-Meteo)."
    if r["status"] == "not_enough":
        return (f"Not enough data yet: {r['days']} days and {r['jams']} jams with weather "
                f"(needs {r['need_days']} days and {r['need_jams']} jams).")
    lo, hi = r["ci"]
    if r["verdict"] == "none":
        return (f"No clear link: jams per unit of printing in humid hours were {r['rr']:.2f}× the dry-hour rate "
                f"at the same times of day (likely {lo:.2f}–{hi:.2f}×, which includes 1 = no difference).")
    word = "more" if r["verdict"] == "humid" else "fewer"
    return (f"Humid hours had {word} jams for the same amount of printing: {r['rr']:.2f}× the dry-hour rate at the "
            f"same times of day (likely {lo:.2f}–{hi:.2f}×). An association with outdoor humidity, not proof of a "
            "cause.")
