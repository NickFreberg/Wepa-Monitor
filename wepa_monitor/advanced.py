"""Planning and statistics beyond the basics (GitHub issue #2).

Every function returns plain tables with honest uncertainty, and each needs a minimum amount of
data before it says anything:

* coverage / placement  spatial: walk to the nearest open backup printer; where one more printer
                        would save students the most walking.
* supplies_monte_carlo  how many of each part to stock for the next N days at 50/90/95% confidence.
* staffing_whatif       downtime an extra coverage window would have saved, replayed on real outages.
* bayes_rates           outages per week per printer, shrunk toward the campus rate (Gamma-Poisson),
                        with 90% credible intervals: honest numbers for printers with little history.
* warning_to_outage     how often a warning turns into an outage within a day (a two-step Markov view).
* recent_changes        printers whose fault rate shifted recently (exact Poisson rate-ratio test).
* before_after          difference-in-differences for changes logged in reference/changes.csv.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

from . import config, insights, metrics as M, nearby, ops, rules, support

TZ = config.LOCAL_TZ
WALK_ALERT_MIN = 5.0          # a backup more than this many minutes away is a coverage gap


# --- spatial --------------------------------------------------------------------------------------

def coverage(ds: M.Dataset, start, end, ids=None) -> pd.DataFrame:
    """Per printer: other printers in its building, the nearest printer anyone can walk into, the
    walk, and the outage hours that made students take that walk in the window."""
    cur = ops.current_status(ds).assign(state="green")         # static view: assume all others work
    st = cur if ids is None else cur[cur["station_id"].isin(ids)]
    drivers = insights.downtime_drivers(ds, start, end, ids)["station"].set_index("key")
    rows = []
    for _, r in st.iterrows():
        if r.get("campus", "Main") != "Main":
            continue
        alt = nearby.backups(cur, r["station_id"], limit=1)
        same = int((alt["kind"] == "same building").sum()) if len(alt) else 0
        pub = alt[alt["kind"] == "open to everyone"] if len(alt) else alt
        walk = float(pub.iloc[0]["seconds"]) / 60 if len(pub) else np.nan
        down_h = float(drivers["down_h"].get(r["station_id"], 0.0)) if len(drivers) else 0.0
        rows.append({"station_id": r["station_id"], "label": r["label"], "building": r["building"],
                     "station_type": r["station_type"], "same_building": same,
                     "nearest": pub.iloc[0]["building"] if len(pub) else "", "walk_min": walk,
                     "meters": float(pub.iloc[0]["meters"]) if len(pub) else np.nan, "down_h": down_h,
                     "stranded_min": 0.0 if same else down_h * walk if np.isfinite(walk) else 0.0,
                     "gap": (not same) and np.isfinite(walk) and walk > WALK_ALERT_MIN})
    return pd.DataFrame(rows).sort_values(["gap", "walk_min"], ascending=[False, False]).reset_index(drop=True)


def placement(ds: M.Dataset, start, end, ids=None, top: int = 5) -> pd.DataFrame:
    """Where one more printer would save the most walking: for each candidate building, the
    walking minutes saved when printers are down (weighted by how busy each printer is and how long
    it was down), as if a printer had been there during the window. A greedy one-step p-median."""
    cov = coverage(ds, start, end, ids)
    if cov.empty or not routing_ok():
        return pd.DataFrame()
    use = M.usage_by_station(ds, start, end, ids).set_index("station_id")["relative"].fillna(1.0)
    cov = cov.assign(w=cov["station_id"].map(use).fillna(1.0) * cov["down_h"])
    cov = cov[(cov["same_building"] == 0) & cov["walk_min"].notna() & (cov["w"] > 0)]
    if cov.empty:
        return pd.DataFrame()
    from . import reference
    cands = reference.load_buildings()
    cands = cands[(cands["campus"] == "Main") & cands["lat"].notna()]["building"]
    out = []
    for c in cands:
        new = cov["building"].map(lambda b: nearby.walk_seconds(b, c) / 60)
        saved = (cov["walk_min"] - np.minimum(cov["walk_min"], new)).clip(lower=0) * cov["w"]
        helped = cov.loc[saved > 0, "building"].unique()
        if saved.sum() > 0:
            weeks = max((end - start).total_seconds() / 604800, 1 / 7)
            out.append({"building": c, "minutes_saved_per_week": float(saved.sum()) / weeks,
                        "helps": ", ".join(sorted(helped)[:4]) + ("…" if len(helped) > 4 else ""),
                        "has_printer": c in set(ds.stations["building"])})
    return pd.DataFrame(out).sort_values("minutes_saved_per_week", ascending=False).head(top).reset_index(drop=True)


def routing_ok() -> bool:
    from . import routing
    return routing.network() is not None


# --- Monte Carlo: supplies ------------------------------------------------------------------------------

def _daily_use(ds: M.Dataset, days_back: int, ids=None) -> pd.DataFrame:
    end = ds.as_of
    start = end - pd.Timedelta(days=days_back)
    u = M.usage_in(ds, start, end, ids)
    u = u.assign(day=u["scrape_ts"].dt.tz_convert(TZ).dt.date)
    return u.groupby(["station_id", "component", "day"])["used"].sum().reset_index()


def supplies_monte_carlo(ds: M.Dataset, horizon_days: int = 30, ids=None, sims: int = 2000,
                         seed: int = 7) -> dict | None:
    """Parts needed over the next `horizon_days`, by resampling each printer's own recent days.

    For every printer and part: draw `horizon_days` days of use from its last 28 observed days (with
    replacement), add them up, and count how many times the part would hit its replacement point
    starting from today's level. Busy and quiet days, and printers, vary independently. Summed over
    printers, 2,000 runs give the distribution: P50 is a typical month, P95 the amount to stock to run
    out only 1 time in 20."""
    hist = _daily_use(ds, 28, ids)
    if hist.empty or hist["day"].nunique() < config.MIN_DAYS_FOR_BURN_RATE:
        return None
    rng = np.random.default_rng(seed)
    observed_days = sorted(hist["day"].unique())
    lv = ds.levels if ids is None else ds.levels[ds.levels["station_id"].isin(ids)]
    lv = lv.set_index(["station_id", "component"])["level"]
    from . import models
    totals = {c: np.zeros(sims) for c in config.COMPONENTS}
    for (sid, comp), g in hist.groupby(["station_id", "component"]):
        daily = g.set_index("day")["used"].reindex(observed_days, fill_value=0.0).to_numpy()
        if daily.sum() <= 0:
            continue
        floor = models.replace_point(comp)
        level = float(lv.get((sid, comp), 100.0))
        draws = rng.choice(daily, size=(sims, horizon_days), replace=True).sum(axis=1)
        first = max(level - floor, 0.0)
        span = max(100.0 - floor, 1.0)
        need = np.where(draws <= first, 0, 1 + np.floor((draws - first) / span))
        totals[comp] += need
    rows = []
    for comp in config.COMPONENTS:
        t = totals[comp]
        if t.max() == 0 and not (hist["component"] == comp).any():
            continue
        rows.append({"component": comp, "label": config.COMPONENT_LABELS[comp], "mean": float(t.mean()),
                     "p50": float(np.percentile(t, 50)), "p90": float(np.percentile(t, 90)),
                     "p95": float(np.percentile(t, 95)), "max": float(t.max())})
    return {"table": pd.DataFrame(rows), "horizon": horizon_days, "sims": sims,
            "history_days": len(observed_days), "draws": {c: totals[c] for c in ("toner_k",)}}


# --- Monte Carlo: staffing what-ifs ---------------------------------------------------------------------

SCENARIOS = {
    "evening": ("Evening check until 9 PM, Monday-Thursday", {d: [(18, 21)] for d in range(4)}),
    "weekend": ("Weekend check 12-2 PM, Saturday and Sunday", {5: [(12, 14)], 6: [(12, 14)]}),
    "morning": ("Early start at 8 AM, weekdays", {d: [(8, 10)] for d in range(5)}),
}


def _next_cover(owner: str, ts: pd.Timestamp, extra: dict) -> pd.Timestamp | None:
    """First covered moment at or after ts: the desk's own hours, or an extra window."""
    best = support.next_open(owner, ts)
    local = ts.tz_convert(TZ)
    closed = support.closed_dates()
    for i in range(8):
        day = (local + pd.Timedelta(days=i)).normalize()
        if day.strftime("%Y-%m-%d") in closed:
            continue
        for a, b in extra.get(day.dayofweek, []):
            s = day + pd.Timedelta(hours=a)
            e = day + pd.Timedelta(hours=b)
            if e > local:
                cand = max(s, local)
                if best is None or cand < best:
                    best = cand
    return best


def staffing_whatif(ds: M.Dataset, start, end, ids=None, scenario: str = "evening", owner: str | None = None,
                    sims: int = 1000, seed: int = 11) -> dict | None:
    """Replay the window's real outages with extra coverage. An outage that began while nobody was
    on would instead be picked up at the next covered moment, then take a desk-time fix drawn from
    the fixes actually observed during desk hours. It can only get shorter, never longer."""
    name, extra = SCENARIOS[scenario]
    inc = M._resolved(M._in(ds.sev_inc, "start", start, end, ids))
    inc = inc[inc["severity"] == "red"]
    if owner:
        inc = inc[inc["owner"] == owner]
    fixes = inc.loc[inc["in_hours"].astype(bool), "staffed_s"].to_numpy(dtype=float)
    fixes = fixes[fixes > 0]
    if len(inc) < config.MIN_INCIDENTS_FOR_MEAN or len(fixes) < 3:
        return None
    rng = np.random.default_rng(seed)
    after = inc[~inc["in_hours"].astype(bool)]
    waits = []
    for r in after.itertuples(index=False):
        cover = _next_cover(r.owner, r.start, extra)
        waits.append((cover - r.start).total_seconds() if cover is not None else r.duration_s)
    waits = np.array(waits, dtype=float)
    actual = after["duration_s"].to_numpy(dtype=float)
    if len(actual) == 0:
        return {"name": name, "outages": 0, "affected": 0, "saved_h": (0.0, 0.0, 0.0), "total_h": 0.0}
    # Uncertainty from both sides: which outages a similar stretch would bring (bootstrap the
    # outages) and how long each desk fix takes (bootstrap the observed desk-hours fixes).
    pick = rng.integers(0, len(actual), size=(sims, len(actual)))
    draws = rng.choice(fixes, size=(sims, len(actual)), replace=True)
    new = np.minimum(actual[pick], waits[pick] + draws)
    saved = (actual[pick] - new).sum(axis=1) / 3600
    weeks = max((end - start).total_seconds() / 604800, 1 / 7)
    affected = int((waits < actual - 60).sum())
    return {"name": name, "outages": int(len(inc)), "after_hours": int(len(after)), "affected": affected,
            "saved_h": tuple(float(np.percentile(saved, q)) for q in (10, 50, 90)),
            "saved_h_per_week": float(np.median(saved)) / weeks, "total_h": float(inc["duration_s"].sum() / 3600)}


# --- Bayesian rates ------------------------------------------------------------------------------------

def bayes_rates(ds: M.Dataset, start, end, ids=None) -> pd.DataFrame:
    """Outages per week per printer with 90% credible intervals (empirical-Bayes Gamma-Poisson).

    The prior is fitted to all printers by the method of moments, so each printer's estimate is its
    own record, pulled toward the campus rate in proportion to how little history it has."""
    days = M.observed_days(ds, start, end, ids)
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    n = inc[inc["severity"] == "red"].groupby("station_id").size()
    t = (days / 7).rename("weeks")
    df = pd.DataFrame({"weeks": t}).assign(n=lambda d: d.index.map(n).fillna(0).astype(int))
    df = df[df["weeks"] >= 1 / 7]
    if len(df) < 5:
        return pd.DataFrame()
    raw = df["n"] / df["weeks"]
    m = float(df["n"].sum() / df["weeks"].sum())
    var_between = max(float(raw.var(ddof=1) - (m / df["weeks"]).mean()), (m * 0.1) ** 2 + 1e-9)
    alpha, beta = m * m / var_between, m / var_between
    a_post, b_post = alpha + df["n"], beta + df["weeks"]
    out = df.assign(raw=raw, mean=a_post / b_post, lo=stats.gamma.ppf(0.05, a_post, scale=1 / b_post),
                    hi=stats.gamma.ppf(0.95, a_post, scale=1 / b_post),
                    shrink=(beta / (beta + df["weeks"])))
    out["above_campus"] = out["lo"] > m
    out["below_campus"] = out["hi"] < m
    out = out.reset_index().rename(columns={"index": "station_id"})
    out = out.merge(ds.stations[["station_id", "label"]], on="station_id", how="left")
    out.attrs.update(campus=m, alpha=alpha, beta=beta)
    return out.sort_values("mean", ascending=False).reset_index(drop=True)


# --- warning -> outage -------------------------------------------------------------------------------------

def warning_to_outage(ds: M.Dataset, start, end, ids=None, within_h: float = 24) -> dict | None:
    """How often a warning is followed by an outage at the same printer within `within_h` hours,
    overall and by what the warning was about."""
    inc = ds.sev_inc if ids is None else ds.sev_inc[ds.sev_inc["station_id"].isin(ids)]
    yel = M._in(inc, "start", start, end, ids)
    yel = yel[yel["severity"] == "yellow"]
    red = inc[inc["severity"] == "red"]
    if len(yel) < 5:
        return None
    reds = {s: pd.DatetimeIndex(g["start"].sort_values()) for s, g in red.groupby("station_id")}
    win = pd.Timedelta(hours=within_h)
    hit, lag = [], []
    for r in yel.itertuples(index=False):
        arr = reds.get(r.station_id)
        i = arr.searchsorted(r.start, side="right") if arr is not None else 0
        nxt = arr[i] if arr is not None and i < len(arr) else None
        ok = nxt is not None and nxt - r.start <= win
        hit.append(bool(ok))
        if ok:
            lag.append((nxt - r.start).total_seconds() / 3600)
    yel = yel.assign(hit=hit)
    from . import narrative as N
    causes = [N.outage_causes(ds, s, t) for s, t in zip(yel["station_id"], yel["start"])]
    yel = yel.assign(cause=[rules.issue_label(c[0][0]) if c else "Other warning" for c in causes])
    by = yel.groupby("cause")["hit"].agg(warnings="size", led_to_outage="sum").reset_index()
    by["share"] = by["led_to_outage"] / by["warnings"]
    lo, hi = stats.beta.ppf([0.05, 0.95], 1 + sum(hit), 1 + len(hit) - sum(hit))
    return {"warnings": len(yel), "share": float(np.mean(hit)), "ci": (float(lo), float(hi)),
            "median_lag_h": float(np.median(lag)) if lag else np.nan,
            "by_cause": by.sort_values(["share", "warnings"], ascending=False).reset_index(drop=True)}


# --- recent change detection ----------------------------------------------------------------------------

def recent_changes(ds: M.Dataset, ids=None, recent_days: int = 14, min_history_days: int = 28,
                   alpha: float = 0.01) -> pd.DataFrame:
    """Printers whose problem rate in the last `recent_days` differs from their own earlier rate.
    Exact test: given the total count, recent faults ~ Binomial(n, recent exposure share)."""
    end = ds.as_of
    mid = end - pd.Timedelta(days=recent_days)
    start = max(ds.data_start or end, end - pd.Timedelta(days=90))
    if (mid - start).days < min_history_days - recent_days:
        return pd.DataFrame()
    f = M._in(ds.fault_inc, "start", start, end, ids)
    exp_before = M.observed_days(ds, start, mid, ids)
    exp_after = M.observed_days(ds, mid, end, ids)
    rows = []
    for sid in sorted(set(exp_before.index) | set(exp_after.index)):
        b, a = float(exp_before.get(sid, 0)), float(exp_after.get(sid, 0))
        if b < 7 or a < 3:
            continue
        fs = f[f["station_id"] == sid]
        nb, na = int((fs["start"] < mid).sum()), int((fs["start"] >= mid).sum())
        n = nb + na
        if n < 4:
            continue
        p = a / (a + b)
        test = stats.binomtest(na, n, p)
        ratio = (na / a) / max(nb / b, 1e-9) if nb else math.inf
        if test.pvalue < alpha:
            top = fs[fs["start"] >= mid]["label"].value_counts()
            rows.append({"station_id": sid, "before_per_week": nb / b * 7, "recent_per_week": na / a * 7,
                         "ratio": ratio, "p": float(test.pvalue), "direction": "worse" if na / a > nb / b else "better",
                         "main_recent": top.index[0] if len(top) else ""})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.merge(ds.stations[["station_id", "label"]], on="station_id", how="left")
    return out.sort_values(["direction", "ratio"], ascending=[False, False]).reset_index(drop=True)


# --- before / after (difference in differences) ------------------------------------------------------------

CHANGES_PATH = config.REFERENCE_DIR / "changes.csv"


def changes() -> pd.DataFrame:
    if not CHANGES_PATH.exists():
        return pd.DataFrame(columns=["date", "stations", "what"])
    df = pd.read_csv(CHANGES_PATH, dtype=str).fillna("")
    df = df[df["date"].str.match(r"\d{4}-\d{2}-\d{2}")]
    return df


def before_after(ds: M.Dataset, window_days: int = 28) -> pd.DataFrame:
    """For each logged change: outages per week before vs after at the changed printers, minus the
    same before/after difference at the other printers of the same team (the comparison group).
    The difference-in-differences removes campus-wide swings like finals or breaks."""
    rows = []
    for c in changes().itertuples(index=False):
        when = pd.Timestamp(c.date).tz_localize(TZ).tz_convert("UTC")
        treated = [s.strip() for s in str(c.stations).replace(";", ",").split(",") if s.strip()]
        treated = [s for s in treated if s in set(ds.stations["station_id"])] or \
            ds.stations.loc[ds.stations["building"].isin(treated), "station_id"].tolist()
        if not treated:
            continue
        owners = ds.stations.loc[ds.stations["station_id"].isin(treated), "owner"].unique()
        control = ds.stations.loc[ds.stations["owner"].isin(owners) & ~ds.stations["station_id"].isin(treated),
                                  "station_id"].tolist()
        b0, a1 = when - pd.Timedelta(days=window_days), min(ds.as_of, when + pd.Timedelta(days=window_days))

        def rate(ids_, s, e):
            if e <= s:
                return np.nan
            inc = M._in(ds.sev_inc, "start", s, e, ids_)
            days = M.observed_days(ds, s, e, ids_).sum()
            return (inc["severity"] == "red").sum() / days * 7 if days > 0 else np.nan

        tb, ta = rate(treated, b0, when), rate(treated, when, a1)
        cb, ca = rate(control, b0, when), rate(control, when, a1)
        after_days = (a1 - when).total_seconds() / 86400
        rows.append({"date": c.date, "what": c.what, "stations": len(treated), "before": tb, "after": ta,
                     "control_before": cb, "control_after": ca, "did": (ta - tb) - (ca - cb),
                     "after_days": after_days, "enough": after_days >= 14 and (ds.as_of - b0).days >= 28})
    return pd.DataFrame(rows)
