"""Station report card: one grade per printer, for "this printer is great, that one is a problem".

A busy printer is not a bad printer. The grade rewards being able to print and penalizes the
things that make a printer a burden: going down, faulting more than its workload explains, and
eating supplies faster than its workload explains. Usage itself never lowers a grade.

    score = 35% availability            (share of observed time it could print)
          + 25% outage frequency        (red incidents per week)
          + 15% faults for its workload (fault incidents per black-toner point, vs the campus rate)
          + 15% supply cost for its workload (all supplies, $ per black-toner point, vs campus)
          + 10% time in warning         (share of observed time yellow)

Each part is scored 0-100 (curves below, all documented on the page), weighted, and turned into a
letter: A 90+, B 80+, C 70+, D 60+, F below 60. Printers observed for fewer than
MIN_DAYS days get no grade rather than a noisy one.

Workload-adjusted rates use a little Bayesian shrinkage: each printer's rate is pulled toward the
campus rate with the weight of PRIOR_USE points of toner, so a rarely used printer with one fault
doesn't look like the worst on campus.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, metrics as M

WEIGHTS = {"availability": 0.35, "outages": 0.25, "faults": 0.15, "cost": 0.15, "warnings": 0.10}
PART_LABEL = {"availability": "Availability", "outages": "Outage frequency", "faults": "Faults for its workload",
              "cost": "Supply cost for its workload", "warnings": "Time in warning"}
MIN_DAYS = 3
PRIOR_USE = 25.0          # toner points of pseudo-usage at the campus rate (shrinkage strength)
GRADES = [(90, "A", "Great"), (80, "B", "Good"), (70, "C", "Fair"), (60, "D", "Needs attention"), (0, "F", "Problem")]


def costs() -> dict[str, float]:
    """$ per part, from reference/consumable_costs.csv (estimates until replaced with real prices)."""
    path = config.REFERENCE_DIR / "consumable_costs.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return dict(zip(df["component"], df["unit_cost_usd"].astype(float)))


def prices_are_estimates() -> bool:
    path = config.REFERENCE_DIR / "consumable_costs.csv"
    return path.exists() and pd.read_csv(path)["source"].astype(str).str.contains("estimate").any()


def _linear(x, zero_at, full_at):
    """0 at `zero_at`, 100 at `full_at`, clipped (works in either direction)."""
    return np.clip((np.asarray(x, dtype=float) - zero_at) / (full_at - zero_at), 0, 1) * 100


def _ratio_score(ratio):
    """Workload-adjusted rate vs campus: half the campus rate or better = 100, the campus rate = 80,
    twice = 40, three times = 0."""
    return np.clip((3 - np.asarray(ratio, dtype=float)) / 2.5, 0, 1) * 100


def grade_of(score: float) -> tuple[str, str]:
    for cut, letter, word in GRADES:
        if score >= cut:
            return letter, word
    return "F", "Problem"


def build(ds: M.Dataset, start, end, ids=None) -> pd.DataFrame:
    sc = M.station_scorecard(ds, start, end, ids)
    if sc.empty:
        return sc
    days = M.observed_days(ds, start, end, ids)
    use = M.usage_in(ds, start, end, ids)
    per = use.groupby(["station_id", "component"])["used"].sum().unstack(fill_value=0.0)
    per = per.reindex(columns=config.COMPONENTS, fill_value=0.0)
    price = costs()
    dollars = sum(per[c] / 100 * price.get(c, 0.0) for c in config.COMPONENTS)
    faults = M._in(ds.fault_inc, "start", start, end, ids).groupby("station_id").size()
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    yel = inc[inc["severity"] == "yellow"]
    yel_s = yel.assign(d=yel["duration_s"].fillna(0).clip(lower=0)).groupby("station_id")["d"].sum()

    out = sc.copy()
    sid = out["station_id"]
    out["observed_days"] = sid.map(days).fillna(0.0)
    out["use_k"] = sid.map(per["toner_k"]).fillna(0.0)
    out["faults"] = sid.map(faults).fillna(0).astype(int)
    out["supply_cost"] = sid.map(dollars).fillna(0.0)
    out["outages_per_week"] = out["red_incidents"] / out["observed_days"].replace(0, np.nan) * 7
    out["warning_share"] = (sid.map(yel_s).fillna(0.0) / (out["observed_h"] * 3600).replace(0, np.nan)).clip(0, 1)
    out["cost_per_month"] = out["supply_cost"] / out["observed_days"].replace(0, np.nan) * 30.44
    usage = out["use_k"] / out["observed_days"].replace(0, np.nan)
    med_use = float(np.nanmedian(usage)) if usage.notna().any() else np.nan
    out["usage_relative"] = usage / med_use if med_use and np.isfinite(med_use) and med_use > 0 else np.nan

    # Workload-adjusted rates, shrunk toward the campus rate.
    tot_use = out["use_k"].sum()
    camp_fault = out["faults"].sum() / tot_use if tot_use > 0 else np.nan
    camp_cost = out["supply_cost"].sum() / tot_use if tot_use > 0 else np.nan
    if np.isfinite(camp_fault) and camp_fault > 0:
        f_rate = (out["faults"] + PRIOR_USE * camp_fault) / (out["use_k"] + PRIOR_USE)
        out["faults_ratio"] = f_rate / camp_fault
    else:
        out["faults_ratio"] = np.where(out["faults"] > 0, 2.0, 1.0)
    if np.isfinite(camp_cost) and camp_cost > 0:
        c_rate = (out["supply_cost"] + PRIOR_USE * camp_cost) / (out["use_k"] + PRIOR_USE)
        out["cost_ratio"] = c_rate / camp_cost
    else:
        out["cost_ratio"] = 1.0

    parts = pd.DataFrame({
        "availability": _linear(out["availability"].fillna(0), 85, 99.5),
        "outages": _linear(out["outages_per_week"].fillna(0), 4, 0),
        "faults": _ratio_score(out["faults_ratio"]),
        "cost": _ratio_score(out["cost_ratio"]),
        "warnings": _linear(out["warning_share"].fillna(0) * 100, 25, 0),
    }, index=out.index)
    for k in WEIGHTS:
        out[f"score_{k}"] = parts[k]
    out["score"] = sum(parts[k] * w for k, w in WEIGHTS.items())
    enough = out["observed_days"] >= MIN_DAYS
    out.loc[~enough, "score"] = np.nan
    out["grade"] = [grade_of(s)[0] if np.isfinite(s) else "—" for s in out["score"]]
    out["verdict"] = [grade_of(s)[1] if np.isfinite(s) else "Not enough data yet" for s in out["score"]]
    shortfall = pd.DataFrame({k: (100 - parts[k]) * w for k, w in WEIGHTS.items()})
    out["drag"] = shortfall.idxmax(axis=1).where(shortfall.max(axis=1) >= 5, "")
    out["why"] = [_why(r) if np.isfinite(r["score"]) else "" for _, r in out.iterrows()]
    return out.sort_values(["score", "availability"], ascending=[True, True], na_position="last").reset_index(drop=True)


def _why(r) -> str:
    """The one-line reason a manager would want: what drags this printer's grade down most."""
    d = r["drag"]
    if not d:
        return "No weak spots: reliable, and efficient for its workload."
    if d == "availability":
        return f"Could print only {r['availability']:.1f}% of the time."
    if d == "outages":
        return f"Went down {r['outages_per_week']:.1f} times a week."
    if d == "faults":
        return f"Faults {r['faults_ratio']:.1f}× as often as the campus average for its workload."
    if d == "cost":
        return f"Supplies cost {r['cost_ratio']:.1f}× the campus average for its workload."
    return f"In a warning state {r['warning_share']:.0%} of the time."


def summary(card: pd.DataFrame) -> dict:
    graded = card[card["score"].notna()]
    return {"graded": len(graded), "counts": graded["grade"].value_counts().to_dict(),
            "median": float(graded["score"].median()) if len(graded) else np.nan}
