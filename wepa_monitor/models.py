"""Predictive and statistical models.

1. End-of-life regression: when will each consumable reach its replacement point?
   A Theil-Sen line (median of pairwise slopes, robust to sensor blips) is fitted
   to the part's readings since its last replacement. The slope's 90% confidence
   interval gives an earliest-latest window for the date.
2. Statistical process control: a c-chart of daily fault incidents with 3-sigma
   limits, plus per-station checks for days far above that station's own norm.
3. Time to fix: Kaplan-Meier survival curves of how long outages last, staffed hours
   vs overnight, compared with a log-rank test. Outages still open (or whose end
   wasn't observed) are kept as censored observations instead of being dropped.
4. Usage vs reliability: ordinary least squares of each station's failure rate on
   its usage (black toner burned per day), with R^2, p-value, a 95% confidence band,
   and the stations that fail more than their usage explains.

Every result carries its sample size, and callers show "not enough data" instead of
a fit when the evidence is thin.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

from . import config, metrics as M

DAY_S = 86400.0
MIN_POINTS_FOR_FIT = 4
MIN_SPAN_DAYS = 1.0
OVERNIGHT_HOURS = (23, 8)      # local time with no desk staffed (approximate)


def replace_point(component: str) -> float:
    return float(config.CONSUMABLE_REPLACE_PCT if component.startswith("toner") else 2)


# --- 1. End-of-life regression ----------------------------------------------------------------

def _current_life(ds: M.Dataset, ids=None) -> pd.DataFrame:
    c = ds.cons if ids is None else ds.cons[ds.cons["station_id"].isin(ids)]
    if c.empty:
        return c
    last_life = c.groupby(["station_id", "component"])["life"].transform("max")
    c = c[c["life"] == last_life]
    # Fit on the most recent stretch only, so an old usage pattern doesn't dominate.
    lookback = c["scrape_ts"] >= ds.as_of - pd.Timedelta(days=config.BURN_RATE_LOOKBACK_DAYS * 2)
    return c[lookback]


def fit_series(ts: pd.Series, level: pd.Series, as_of: pd.Timestamp, floor: float) -> dict | None:
    """Theil-Sen fit of level vs time. Returns the projected end-of-life date and its 90% window."""
    t = ((ts - as_of).dt.total_seconds() / DAY_S).to_numpy()
    y = level.to_numpy(dtype=float)
    if len(t) < MIN_POINTS_FOR_FIT or t.max() - t.min() < MIN_SPAN_DAYS or np.ptp(y) == 0:
        return None
    slope, intercept, lo, hi = stats.theilslopes(y, t, alpha=0.90)
    if slope >= 0:
        return None
    pred = intercept + slope * t
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum()) or 1.0
    level_now = intercept                      # the fitted level at as_of (t = 0)

    def days_to(s):
        if s >= 0:
            return np.inf
        return max(0.0, (floor - level_now) / s)

    return {"slope_per_day": slope, "intercept": intercept, "level_fit": level_now,
            "days": days_to(slope),
            "days_early": days_to(lo),          # steepest plausible slope -> earliest date
            "days_late": days_to(hi) if hi < 0 else np.inf,
            "r2": 1 - ss_res / ss_tot, "n": int(len(t)), "span_days": float(t.max() - t.min())}


def eol_forecast(ds: M.Dataset, ids=None) -> pd.DataFrame:
    """One row per station x part: projected days to the replacement point, with a 90% window.
    Falls back to the simple burn-rate estimate where a regression isn't possible."""
    simple = M.forecast(ds, ids).set_index(["station_id", "component"])
    life = _current_life(ds, ids)
    rows = []
    for (sid, comp), g in life.groupby(["station_id", "component"], sort=False):
        fit = fit_series(g["scrape_ts"], g["level"], ds.as_of, replace_point(comp))
        if fit:
            rows.append({"station_id": sid, "component": comp, "method": "regression", **fit})
    reg = pd.DataFrame(rows).set_index(["station_id", "component"]) if rows else pd.DataFrame()
    out = simple.copy()
    for col in ("days", "days_early", "days_late", "slope_per_day", "r2", "n", "method"):
        out[col] = reg[col] if col in reg else np.nan
    use_simple = out["days"].isna()
    out.loc[use_simple, "days"] = out.loc[use_simple, "days_to_replace"]
    out.loc[use_simple, "method"] = np.where(out.loc[use_simple, "days"].notna(), "burn rate", "not enough data")
    at_floor = out["level"] <= out.index.get_level_values("component").map(replace_point)
    out.loc[at_floor, ["days", "days_early", "days_late"]] = 0.0
    out["eol_date"] = ds.as_of + pd.to_timedelta(out["days"].clip(upper=3650).fillna(3650), unit="D")
    return out.reset_index()


def eol_points(ds: M.Dataset, station_id: str, component: str) -> pd.DataFrame:
    life = _current_life(ds, [station_id])
    return life[life["component"] == component][["scrape_ts", "level"]]


# --- 2. Statistical process control -------------------------------------------------------------

@dataclass
class ControlChart:
    daily: pd.DataFrame                 # local_date, count, out (bool)
    center: float
    ucl: float
    lcl: float
    signals: list[str] = field(default_factory=list)


def fault_control_chart(ds: M.Dataset, start, end, ids=None) -> ControlChart | None:
    f = M.faults_in(ds, start, end, ids)
    days = pd.date_range(start.tz_convert(config.LOCAL_TZ).normalize(), end.tz_convert(config.LOCAL_TZ).normalize(),
                         freq="D").tz_localize(None)
    if len(days) < 7:
        return None
    local = f["start"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None).dt.normalize()
    daily = local.value_counts().reindex(days, fill_value=0).rename_axis("local_date").reset_index(name="count")
    center = float(daily["count"].mean())
    ucl = center + 3 * np.sqrt(center)
    lcl = max(0.0, center - 3 * np.sqrt(center))
    daily["out"] = (daily["count"] > ucl) | (daily["count"] < lcl)
    signals = [f"{d:%a %b %-d}: {c} faults (limit {ucl:.1f})" for d, c in
               daily.loc[daily["out"], ["local_date", "count"]].itertuples(index=False)]
    # Western Electric rule 4: eight consecutive days on one side of the center line.
    side = np.sign(daily["count"].to_numpy() - center)
    run, prev = 0, 0
    for i, s in enumerate(side):
        run = run + 1 if s == prev and s != 0 else 1
        prev = s
        if run == 8:
            word = "above" if s > 0 else "below"
            signals.append(f"Eight days in a row {word} average ending {daily['local_date'].iloc[i]:%b %-d}: "
                           "a sustained shift, not noise")
    return ControlChart(daily, center, ucl, lcl, signals)


def station_anomalies(ds: M.Dataset, start, end, ids=None) -> pd.DataFrame:
    """Station-days with far more faults than that station's own daily average (Poisson tail, p < 0.001)."""
    f = M.faults_in(ds, start, end, ids)
    if f.empty:
        return pd.DataFrame(columns=["station_id", "station", "local_date", "count", "expected", "p"])
    n_days = max(1.0, (end - start).total_seconds() / DAY_S)
    f = f.assign(local_date=f["start"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None).dt.normalize())
    per_day = f.groupby(["station_id", "station", "local_date"]).size().rename("count").reset_index()
    rate = f.groupby("station_id").size() / n_days
    per_day["expected"] = per_day["station_id"].map(rate)
    per_day["p"] = stats.poisson.sf(per_day["count"] - 1, per_day["expected"])
    return per_day[per_day["p"] < 0.001].sort_values("p").reset_index(drop=True)


# --- 3. Time to fix: Kaplan-Meier + log-rank ----------------------------------------------------------

def kaplan_meier(durations: np.ndarray, observed: np.ndarray) -> pd.DataFrame:
    """Share of outages still unresolved after t hours (product-limit estimator)."""
    order = np.argsort(durations)
    d, e = durations[order], observed[order]
    times = np.unique(d[e == 1])
    surv, s = [], 1.0
    for t in times:
        at_risk = (d >= t).sum()
        events = ((d == t) & (e == 1)).sum()
        s *= 1 - events / at_risk
        surv.append(s)
    return pd.DataFrame({"t": np.r_[0.0, times], "survival": np.r_[1.0, surv]})


def logrank(d1, e1, d2, e2) -> tuple[float, float]:
    """Log-rank chi-square statistic and p-value for two groups."""
    d = np.r_[d1, d2]
    e = np.r_[e1, e2]
    g = np.r_[np.zeros(len(d1)), np.ones(len(d2))]
    o_minus_e, var = 0.0, 0.0
    for t in np.unique(d[e == 1]):
        at_risk = d >= t
        n, n1 = at_risk.sum(), (at_risk & (g == 0)).sum()
        events = (d == t) & (e == 1)
        k, k1 = events.sum(), (events & (g == 0)).sum()
        o_minus_e += k1 - k * n1 / n
        if n > 1:
            var += k * (n1 / n) * (1 - n1 / n) * (n - k) / (n - 1)
    if var == 0:
        return 0.0, 1.0
    chi2 = o_minus_e ** 2 / var
    return float(chi2), float(stats.chi2.sf(chi2, 1))


def _median_survival(km: pd.DataFrame) -> float:
    below = km[km["survival"] <= 0.5]
    return float(below["t"].iloc[0]) if len(below) else np.inf


@dataclass
class TimeToFix:
    curves: dict[str, pd.DataFrame]
    medians: dict[str, float]
    within: dict[str, dict[int, float]]
    n: dict[str, int]
    p_value: float


def time_to_fix(ds: M.Dataset, start, end, ids=None) -> TimeToFix | None:
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    inc = inc[(inc["severity"] == "red") & ~inc["censored_start"].astype(bool)]
    if len(inc) < 10:
        return None
    resolved = inc["status"] == "resolved"
    dur_s = np.where(resolved, inc["duration_s"],
                     np.where(inc["status"] == "open", inc["duration_s"],
                              (inc["last_seen"] - inc["start"]).dt.total_seconds()))
    hours = np.asarray(dur_s, dtype=float) / 3600
    observed = resolved.to_numpy().astype(int)
    h = inc["start"].dt.tz_convert(config.LOCAL_TZ).dt.hour.to_numpy()
    night = (h >= OVERNIGHT_HOURS[0]) | (h < OVERNIGHT_HOURS[1])
    groups = {"Started during staffed hours": ~night, "Started overnight (11 pm-8 am)": night}
    curves, medians, within, n = {}, {}, {}, {}
    for name, mask in groups.items():
        if mask.sum() < 3:
            continue
        km = kaplan_meier(hours[mask], observed[mask])
        curves[name], medians[name], n[name] = km, _median_survival(km), int(mask.sum())
        within[name] = {k: float(1 - km.loc[km["t"] <= k, "survival"].iloc[-1]) for k in (1, 4, 12)}
    if len(curves) < 2:
        return None
    _, p = logrank(hours[~night], observed[~night], hours[night], observed[night])
    return TimeToFix(curves, medians, within, n, p)


# --- 4. Usage vs reliability -----------------------------------------------------------------------

@dataclass
class UsageFit:
    points: pd.DataFrame                # station, usage, failures_per_week, fitted, resid, outlier
    slope: float
    intercept: float
    r2: float
    p_value: float
    n: int
    band: pd.DataFrame                  # x, lo, hi (95% confidence band for the mean)


def usage_vs_reliability(ds: M.Dataset, start, end, ids=None) -> UsageFit | None:
    days = M.observed_days(ds, start, end, ids)
    days = days[days >= config.MIN_DAYS_FOR_BURN_RATE]
    if len(days) < 6:
        return None
    use = M.usage_in(ds, start, end, ids)
    k_used = use[use["component"] == "toner_k"].groupby("station_id")["used"].sum()
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    fails = inc[inc["severity"] == "red"].groupby("station_id").size()
    pts = pd.DataFrame({"usage": k_used.reindex(days.index, fill_value=0) / days,
                        "failures_per_week": fails.reindex(days.index, fill_value=0) / days * 7}).reset_index()
    pts = pts.rename(columns={"index": "station_id"})
    pts = pts.merge(ds.stations[["station_id", "label"]], on="station_id", how="left")
    x, y = pts["usage"].to_numpy(), pts["failures_per_week"].to_numpy()
    if np.ptp(x) == 0:
        return None
    lr = stats.linregress(x, y)
    pts["fitted"] = lr.intercept + lr.slope * x
    pts["resid"] = y - pts["fitted"]
    s = float(np.sqrt((pts["resid"] ** 2).sum() / max(1, len(x) - 2)))
    pts["outlier"] = pts["resid"] > 2 * s
    grid = np.linspace(x.min(), x.max(), 50)
    t = stats.t.ppf(0.975, len(x) - 2)
    se = s * np.sqrt(1 / len(x) + (grid - x.mean()) ** 2 / ((x - x.mean()) ** 2).sum())
    fit = lr.intercept + lr.slope * grid
    band = pd.DataFrame({"x": grid, "lo": fit - t * se, "hi": fit + t * se, "fit": fit})
    return UsageFit(pts, float(lr.slope), float(lr.intercept), float(lr.rvalue ** 2), float(lr.pvalue),
                    len(x), band)
