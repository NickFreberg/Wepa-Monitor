"""KPIs and KRIs computed from the derived event tables.

Every function takes a window [start, end) and an optional set of station IDs,
so the same definitions power the fleet, building and station views. Results
carry their evidence (n, observed hours, coverage) and an `ok` flag; the
dashboard grays out anything that does not meet the gating rules in config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import campus, config, consumables, events, rollup, store, support
from .reference import load_stations, station_table


@dataclass
class Metric:
    value: float | None
    n: int = 0
    ok: bool = True
    note: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Dataset:
    data_dir: Path
    meta: dict
    is_demo: bool
    as_of: pd.Timestamp
    stations: pd.DataFrame
    latest: pd.DataFrame
    status: pd.DataFrame      # state-change rows (run starts/ends) for timelines
    hourly: pd.DataFrame      # station x hour: covered_s, up_s (+ local_date, section)
    bhourly: pd.DataFrame     # building x hour: minutes, any_up_min, all_up_min
    sev_inc: pd.DataFrame
    fault_inc: pd.DataFrame
    tray_inc: pd.DataFrame
    cons: pd.DataFrame
    repl: pd.DataFrame
    levels: pd.DataFrame
    log: pd.DataFrame
    data_start: pd.Timestamp | None
    # Results of expensive computations on this dataset (forecasts, models), keyed by their
    # arguments. A dataset never changes once loaded, so they stay valid until the next refresh
    # replaces the whole dataset; every viewer and page then shares one computation.
    memo: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def empty(self) -> bool:
        return self.hourly.empty

    def ids(self, section=None, area=None, building=None, owner=None) -> list[str]:
        s = self.stations
        if owner:
            s = s[s["owner"].isin(owner if isinstance(owner, list) else [owner])]
        if section:
            s = s[s["section"].isin(section if isinstance(section, list) else [section])]
        if area:
            s = s[s["area"].isin(area if isinstance(area, list) else [area])]
        if building:
            s = s[s["building"].isin(building if isinstance(building, list) else [building])]
        return s["station_id"].tolist()


def memo(ds: "Dataset", key: tuple, compute):
    """Compute once per dataset; DataFrames are returned as copies so callers can't alter the cache."""
    if key not in ds.memo:
        ds.memo[key] = compute()
    value = ds.memo[key]
    return value.copy() if isinstance(value, pd.DataFrame) else value


def _ids_key(ids) -> tuple | None:
    return None if ids is None else tuple(sorted(ids))


def _local(ts: pd.Series) -> pd.Series:
    return ts.dt.tz_convert(config.LOCAL_TZ)


def load(data_dir: Path, now: datetime | None = None, rollups: "rollup.RollupStore | None" = None) -> Dataset:
    """Build the dataset from day rollups. Pass the same RollupStore on every refresh so finished
    days are processed once and only the newest day is recomputed."""
    meta = store.read_meta(data_dir)
    is_demo = bool(meta.get("synthetic"))
    log = store.load_scrape_log(data_dir)

    # Demo data is frozen in time, so "now" is the end of the generated range.
    if is_demo and "end" in meta:
        as_of = pd.Timestamp(meta["end"])
    else:
        as_of = pd.Timestamp(now or datetime.now(timezone.utc))
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")

    if rollups is None:
        rollups = rollup.RollupStore(data_dir, building_map())
    r = rollups.refresh()
    status = r["status"].sort_values(["station_id", "scrape_ts"], kind="stable", ignore_index=True)
    for col in ("section", "station_id", "description", "row_status", "status_codes", "printer_text"):
        status[col] = status[col].astype(str)

    latest = status.groupby("station_id", sort=False).tail(1).reset_index(drop=True).drop(columns="brk")
    cons, repl, levels = _consumables(rollups)

    stations = station_table(status)
    ref = stations.set_index("station_id")
    hourly = r["hourly"].copy()
    hourly["station_id"] = hourly["station_id"].astype(str)
    hourly["local_date"] = _local(hourly["hour"]).dt.tz_localize(None).dt.normalize()
    hourly["section"] = hourly["station_id"].map(ref["section"])
    hourly["building"] = hourly["station_id"].map(ref["building"])

    return Dataset(
        data_dir=data_dir, meta=meta, is_demo=is_demo, as_of=as_of,
        stations=stations, latest=latest, status=status, hourly=hourly, bhourly=r["bhourly"],
        sev_inc=campus.annotate_exposure(support.annotate(events.severity_incidents(status, as_of), stations, as_of),
                                         stations, as_of),
        fault_inc=events.fault_incidents(status, as_of),
        tray_inc=events.tray_incidents(status, as_of),
        cons=cons, repl=repl, levels=levels, log=log,
        data_start=status["scrape_ts"].min() if not status.empty else None,
    )


def _consumables(rollups) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Usage, replacements and current levels. Finished days are processed once and kept on the
    rollup store; each refresh only adds today's readings (consumables.usage_continued). Usage rows
    are kept in time order so a date range is a binary search (usage_in)."""
    order = ["station_id", "component", "scrape_ts"]
    cache = getattr(rollups, "_usage_cache", None)
    if cache is None or cache[0] != rollups.history_key:
        hist_pts = rollups.history["cons"].sort_values(order, kind="stable", ignore_index=True)
        h_cons, h_repl = consumables.usage(hist_pts)
        h_last = hist_pts.groupby(["station_id", "component"], sort=False).tail(1)
        cache = (rollups.history_key, h_cons, h_cons.sort_values("scrape_ts", kind="stable", ignore_index=True),
                 h_repl, h_last, consumables.life_seed(h_cons))
        rollups._usage_cache = cache
    _, h_cons, h_cons_by_time, h_repl, h_last, seed = cache
    today_pts = rollups.today["cons"]
    t_cons, t_repl = consumables.usage_continued(h_cons, today_pts, seed)
    parts = [f for f in (h_cons_by_time, t_cons.sort_values("scrape_ts", kind="stable")) if not f.empty]
    cons = pd.concat(parts, ignore_index=True) if parts else t_cons
    repl = pd.concat([f for f in (h_repl, t_repl) if not f.empty] or [t_repl], ignore_index=True)
    last = pd.concat([f for f in (h_last, today_pts) if not f.empty] or [today_pts], ignore_index=True)
    levels = consumables.current_levels(last.sort_values(order, kind="stable", ignore_index=True))
    return cons, repl, levels


def building_map() -> dict[str, str]:
    st = load_stations()
    return dict(zip(st["station_id"], st["building"]))


# --- window helpers ------------------------------------------------------------

def window(ds: Dataset, days: int | None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[start, end) ending now. Never starts before monitoring began, so coverage and
    completeness are measured against time the monitor could actually have seen."""
    end = ds.as_of
    first = ds.data_start or end - pd.Timedelta(days=1)
    start = first if days is None else max(first, end - pd.Timedelta(days=days))
    return start, end


def _resolved(inc: pd.DataFrame) -> pd.DataFrame:
    """Incidents with a known start and end - the only ones a mean duration can use."""
    return inc[(inc["status"] == "resolved") & ~inc["censored_start"].astype(bool)]


def _hours(ds: Dataset, start, end, ids) -> pd.DataFrame:
    """Station-hours overlapping [start, end); windows are resolved to whole hours."""
    h = ds.hourly
    m = (h["hour"] >= start.floor("h")) & (h["hour"] < end)
    if ids is not None:
        m &= h["station_id"].isin(ids)
    return h[m]


def _in(df: pd.DataFrame, col: str, start, end, ids) -> pd.DataFrame:
    m = (df[col] >= start) & (df[col] < end)
    if ids is not None:
        m &= df["station_id"].isin(ids)
    return df[m]


def _gate_mean(durations_s: pd.Series, note_unit="min") -> Metric:
    n = int(durations_s.size)
    if n == 0:
        return Metric(None, 0, False, "no incidents in window")
    value = float(durations_s.mean()) / 60
    ok = n >= config.MIN_INCIDENTS_FOR_MEAN
    return Metric(value, n, ok, "" if ok else f"only {n} incident(s) - low confidence",
                  {"median": float(durations_s.median()) / 60, "p90": float(durations_s.quantile(0.9)) / 60})


# --- availability, MTTR, MTBF ----------------------------------------------------

def availability(ds: Dataset, start, end, ids=None) -> Metric:
    sp = _hours(ds, start, end, ids)
    observed = sp["covered_s"].sum()
    n_st = len(ids) if ids is not None else ds.stations.shape[0]
    expected = max(1.0, (end - start.floor("h")).total_seconds() * max(n_st, 1))
    coverage = min(1.0, observed / expected)   # the latest snapshot's minute can spill past `end`
    if observed == 0:
        return Metric(None, 0, False, "no observations in window")
    up = sp["up_s"].sum()
    ok = coverage >= config.MIN_COVERAGE_FOR_RATE
    return Metric(up / observed * 100, int(len(sp)), ok,
                  "" if ok else f"only {coverage:.0%} of the window observed",
                  {"observed_h": observed / 3600, "coverage": coverage, "down_h": (observed - up) / 3600})


def availability_daily(ds: Dataset, start, end, ids=None, by: str | None = None) -> pd.DataFrame:
    sp = _hours(ds, start, end, ids)
    keys = ["local_date"] + ([by] if by else [])
    g = sp.groupby(keys, observed=True)[["up_s", "covered_s"]].sum().reset_index()
    g["availability"] = g["up_s"] / g["covered_s"] * 100
    g["observed_h"] = g["covered_s"] / 3600
    return g


def availability_by_hour(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    """Availability by local hour of day, for weekdays and weekends: when in the day printers are
    least likely to work. Columns: hour (0-23), daytype ('Weekdays' | 'Weekends'), availability,
    down_h (printer-hours lost in that hour slot across the period), observed_h."""
    sp = _hours(ds, start, end, ids)
    if sp.empty:
        return pd.DataFrame(columns=["hour", "daytype", "availability", "down_h", "observed_h"])
    local = sp["hour"].dt.tz_convert(config.LOCAL_TZ)
    g = sp.assign(h=local.dt.hour, daytype=np.where(local.dt.dayofweek >= 5, "Weekends", "Weekdays")) \
        .groupby(["daytype", "h"])[["up_s", "covered_s"]].sum().reset_index().rename(columns={"h": "hour"})
    g["availability"] = g["up_s"] / g["covered_s"] * 100
    g["down_h"] = (g["covered_s"] - g["up_s"]) / 3600
    g["observed_h"] = g["covered_s"] / 3600
    return g[["hour", "daytype", "availability", "down_h", "observed_h"]]


def building_availability(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    """Share of observed minutes in which at least one printer in the building could print."""
    b = ds.bhourly
    m = (b["hour"] >= start.floor("h")) & (b["hour"] < end)
    if ids is not None:
        m &= b["building"].isin(set(ds.stations.loc[ds.stations["station_id"].isin(ids), "building"]))
    b = b[m]
    if b.empty:
        return pd.DataFrame(columns=["building", "stations", "any_up", "all_up"])
    g = b.groupby("building")[["minutes", "any_up_min", "all_up_min"]].sum()
    out = pd.DataFrame({"any_up": g["any_up_min"] / g["minutes"] * 100,
                        "all_up": g["all_up_min"] / g["minutes"] * 100}).reset_index()
    counts = ds.stations.groupby("building")["station_id"].nunique().rename("stations")
    return out.merge(counts, on="building", how="left").sort_values("any_up")


def mttr(ds: Dataset, severity: str, start, end, ids=None) -> Metric:
    inc = _in(ds.sev_inc, "start", start, end, ids)
    inc = _resolved(inc[inc["severity"] == severity])
    return _gate_mean(inc["duration_s"])


def mtbf(ds: Dataset, start, end, ids=None) -> Metric:
    up_h = _hours(ds, start, end, ids)["up_s"].sum() / 3600
    inc = _in(ds.sev_inc, "start", start, end, ids)
    failures = int((inc["severity"] == "red").sum())
    if up_h == 0:
        return Metric(None, 0, False, "no observations in window")
    if failures == 0:
        return Metric(None, 0, False, f"no failures in {up_h:,.0f} printer-hours", {"up_h": up_h})
    n_st = len(ids) if ids is not None else ds.stations.shape[0]
    days_per_station = up_h / 24 / max(n_st, 1)
    ok = failures >= config.MIN_INCIDENTS_FOR_MEAN and days_per_station >= config.MIN_DAYS_FOR_BURN_RATE
    note = "" if ok else (f"only {failures} failure(s)" if failures < config.MIN_INCIDENTS_FOR_MEAN
                          else f"only {days_per_station:.1f} days observed - low confidence")
    return Metric(up_h / failures, failures, ok, note, {"up_h": up_h})


def station_scorecard(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    g = _hours(ds, start, end, ids).groupby("station_id")[["up_s", "covered_s"]].sum()
    inc = _in(ds.sev_inc, "start", start, end, ids)
    red = inc[inc["severity"] == "red"]
    res = _resolved(red)
    yel = _resolved(inc[inc["severity"] == "yellow"])
    faults = _in(ds.fault_inc, "start", start, end, ids)
    top = faults.groupby("station_id")["label"].agg(lambda s: s.value_counts().index[0])
    out = pd.DataFrame({
        "availability": g["up_s"] / g["covered_s"] * 100,
        "observed_h": g["covered_s"] / 3600,
        "red_incidents": red.groupby("station_id").size(),
        "mttr_red_min": res.groupby("station_id")["duration_s"].mean() / 60,
        "mttr_yellow_min": yel.groupby("station_id")["duration_s"].mean() / 60,
        "top_fault": top,
    })
    out["red_incidents"] = out["red_incidents"].fillna(0).astype(int)
    out["mtbf_h"] = np.where(out["red_incidents"] > 0, g["up_s"] / 3600 / out["red_incidents"].replace(0, np.nan), np.nan)
    out = out.reset_index().rename(columns={"index": "station_id"})
    return out.merge(ds.stations[["station_id", "label", "building", "area", "section", "owner"]], on="station_id", how="left")


# --- faults ---------------------------------------------------------------------

def faults_in(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    f = _in(ds.fault_inc, "start", start, end, ids).copy()
    local = _local(f["start"])
    f["hour"] = local.dt.hour
    f["weekday"] = local.dt.dayofweek
    return f.merge(ds.stations[["station_id", "label", "building"]].rename(columns={"label": "station"}),
                   on="station_id", how="left")


def paper_refill_time(ds: Dataset, start, end, ids=None) -> Metric:
    f = _in(ds.fault_inc, "start", start, end, ids)
    return _gate_mean(_resolved(f[f["code"] == "paper_out"])["duration_s"])


def tray_empty_time(ds: Dataset, start, end, ids=None) -> Metric:
    t = _in(ds.tray_inc, "start", start, end, ids)
    m = _gate_mean(_resolved(t)["duration_s"])
    m.extra["total_h"] = float(t["duration_s"].sum() / 3600)
    return m


# --- consumables -------------------------------------------------------------------

def observed_days(ds: Dataset, start, end, ids=None) -> pd.Series:
    return _hours(ds, start, end, ids).groupby("station_id")["covered_s"].sum() / 86400


def usage_in(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    """Usage rows in [start, end). ds.cons is in time order, so this is a binary search, not a scan."""
    ts = ds.cons["scrape_ts"]
    lo, hi = ts.searchsorted(pd.Timestamp(start)), ts.searchsorted(pd.Timestamp(end))
    out = ds.cons.iloc[lo:hi]
    return out[out["station_id"].isin(ids)] if ids is not None else out


def burn_rates(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    """Per component: points used, part-equivalents, and burn per day/week/month/year."""
    u = usage_in(ds, start, end, ids)
    days = observed_days(ds, start, end, ids)
    per_station = u.groupby(["station_id", "component"])["used"].sum().unstack(fill_value=0.0)
    per_station = per_station.reindex(columns=config.COMPONENTS, fill_value=0.0)
    valid = days[days >= config.MIN_DAYS_FOR_BURN_RATE]
    rate = per_station.reindex(valid.index, fill_value=0.0).div(valid, axis=0)   # pts/day/station
    rows = []
    for comp in config.COMPONENTS:
        used = float(per_station[comp].sum()) if comp in per_station else 0.0
        per_day = float(rate[comp].sum()) if len(rate) else np.nan
        rows.append({
            "component": comp, "label": config.COMPONENT_LABELS[comp],
            "used_pts": used, "used_units": used / 100,
            "per_day": per_day, "per_week": per_day * 7, "per_month": per_day * 30.44,
            "per_year": per_day * 365, "stations": int(len(valid)),
            "ok": len(valid) > 0,
        })
    return pd.DataFrame(rows)


def usage_by_station(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    """How much each printer is used, from the toner it burns (Wepa doesn't publish page counts).

    usage_per_day: black-toner points per observed day (nearly every page uses black, so this
    tracks pages printed); color_per_day: cyan+magenta+yellow points per day; relative: usage vs
    the median printer in scope (2.0 = twice as busy). Printers observed for less than
    MIN_DAYS_FOR_BURN_RATE days get no rate."""
    u = usage_in(ds, start, end, ids)
    days = observed_days(ds, start, end, ids)
    per = u.groupby(["station_id", "component"])["used"].sum().unstack(fill_value=0.0)
    per = per.reindex(columns=config.COMPONENTS, fill_value=0.0)
    st = ds.stations if ids is None else ds.stations[ds.stations["station_id"].isin(ids)]
    out = st[["station_id", "label", "description", "building", "area", "section", "owner", "station_type"]].copy()
    out["observed_days"] = out["station_id"].map(days).fillna(0.0)
    ok = out["observed_days"] >= config.MIN_DAYS_FOR_BURN_RATE
    k = out["station_id"].map(per["toner_k"]).fillna(0.0)
    color = out["station_id"].map(per[["toner_c", "toner_m", "toner_y"]].sum(axis=1)).fillna(0.0)
    out["usage_per_day"] = np.where(ok, k / out["observed_days"].replace(0, np.nan), np.nan)
    out["color_per_day"] = np.where(ok, color / out["observed_days"].replace(0, np.nan), np.nan)
    out["cartridges_per_month"] = out["usage_per_day"] * 30.44 / 100
    med = float(np.nanmedian(out["usage_per_day"])) if out["usage_per_day"].notna().any() else np.nan
    out["relative"] = out["usage_per_day"] / med if med and np.isfinite(med) and med > 0 else np.nan
    out["printers_in_building"] = out["building"].map(ds.stations.groupby("building")["station_id"].nunique())
    return out.sort_values("usage_per_day", ascending=False, na_position="last").reset_index(drop=True)


def cumulative_usage(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    u = usage_in(ds, start, end, ids)
    u = u.assign(local_date=_local(u["scrape_ts"]).dt.tz_localize(None).dt.normalize())
    daily = u.groupby(["local_date", "component"])["used"].sum().unstack(fill_value=0.0)
    daily = daily.reindex(columns=config.COMPONENTS, fill_value=0.0)
    return (daily.cumsum() / 100).reset_index()


def forecast(ds: Dataset, ids=None) -> pd.DataFrame:
    """Days until each part reaches its replacement point, from the recent burn rate."""
    start = ds.as_of - pd.Timedelta(days=config.BURN_RATE_LOOKBACK_DAYS)
    u = usage_in(ds, start, ds.as_of, ids)
    days = observed_days(ds, start, ds.as_of, ids)
    used = u.groupby(["station_id", "component"])["used"].sum().rename("used").reset_index()
    lv = ds.levels if ids is None else ds.levels[ds.levels["station_id"].isin(ids)]
    out = lv.merge(used, on=["station_id", "component"], how="left").fillna({"used": 0.0})
    out["observed_days"] = out["station_id"].map(days).fillna(0.0)
    out["burn_per_day"] = np.where(out["observed_days"] >= config.MIN_DAYS_FOR_BURN_RATE,
                                   out["used"] / out["observed_days"].replace(0, np.nan), np.nan)
    floor = np.where(out["component"].str.startswith("toner"), config.CONSUMABLE_REPLACE_PCT, 2)
    out["days_to_replace"] = np.where(out["burn_per_day"] > 0,
                                      np.maximum(out["level"] - floor, 0) / out["burn_per_day"], np.nan)
    out["label"] = out["component"].map(config.COMPONENT_LABELS)
    return out.merge(ds.stations[["station_id", "label", "building", "area"]].rename(
        columns={"label": "station"}), on="station_id", how="left")


def replacements(ds: Dataset, start, end, ids=None) -> pd.DataFrame:
    r = _in(ds.repl, "ts", start, end, ids).copy()
    r["label"] = r["component"].map(config.COMPONENT_LABELS)
    return r.merge(ds.stations[["station_id", "label"]].rename(columns={"label": "station"}),
                   on="station_id", how="left").sort_values("ts", ascending=False)


# --- data quality --------------------------------------------------------------------

def data_quality(ds: Dataset, start, end, now: pd.Timestamp | None = None) -> Metric:
    now = now or ds.as_of
    log = ds.log[(ds.log["attempt_ts"] >= start) & (ds.log["attempt_ts"] < end)]
    attempts = len(log)
    if attempts == 0:
        return Metric(None, 0, False, "no scrape attempts logged")
    ok = int(log["ok"].sum())
    expected = max(1.0, (end - start).total_seconds() / config.EXPECTED_INTERVAL_S)
    completeness = min(1.0, ok / expected)
    last_ok = ds.log.loc[ds.log["ok"], "attempt_ts"].max()
    age_min = (now - last_ok).total_seconds() / 60 if pd.notna(last_ok) else np.inf
    freshness = float(np.clip(1 - (age_min - 2) / 58, 0, 1))
    sp = _hours(ds, start, end, None)
    good = log.loc[log["ok"], "n_stations"]
    station_cov = float((good / good.max()).mean()) if len(good) and good.max() > 0 else 0.0
    cons = _in(ds.cons, "scrape_ts", start, end, None)
    validity = 1.0 if cons.empty else float(cons["level"].between(0, 100).mean())
    score = 100 * (0.3 * freshness + 0.4 * completeness + 0.2 * validity + 0.1 * station_cov)
    return Metric(score, attempts, True, "", {
        "freshness": freshness * 100, "age_min": age_min, "completeness": completeness * 100,
        "validity": validity * 100, "station_coverage": station_cov * 100,
        "failure_rate": (attempts - ok) / attempts * 100, "attempts": attempts,
        "failures": attempts - ok, "observed_h": float(sp["covered_s"].sum() / 3600),
    })


def quality_daily(ds: Dataset, start, end) -> pd.DataFrame:
    log = ds.log[(ds.log["attempt_ts"] >= start) & (ds.log["attempt_ts"] < end)]
    log = log.assign(local_date=_local(log["attempt_ts"]).dt.tz_localize(None).dt.normalize())
    g = log.groupby("local_date").agg(attempts=("ok", "size"), ok=("ok", "sum")).reset_index()
    g["failure_rate"] = (g["attempts"] - g["ok"]) / g["attempts"] * 100
    # Expected snapshots per day = the part of that day inside the window (first/last days are partial).
    tz = config.LOCAL_TZ
    day_start = g["local_date"].dt.tz_localize(tz, nonexistent="shift_forward", ambiguous=False).dt.tz_convert("UTC")
    day_end = (g["local_date"] + pd.Timedelta(days=1)).dt.tz_localize(
        tz, nonexistent="shift_forward", ambiguous=False).dt.tz_convert("UTC")
    secs = (day_end.clip(upper=end) - day_start.clip(lower=start)).dt.total_seconds()
    g["completeness"] = (g["ok"] / (secs / config.EXPECTED_INTERVAL_S) * 100).clip(upper=100)
    return g
