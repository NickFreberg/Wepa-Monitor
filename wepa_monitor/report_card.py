"""Station report card: one grade per printer, for "this printer is great, that one is a problem".

A busy printer is not a bad printer. The grade rewards being able to print and penalizes the
things that make a printer a burden: going down, faulting more than its workload explains, and
wearing out parts faster than its workload explains. Usage itself never lowers a grade. No prices are
used: BSU's printer models and part sources aren't known here, so wear is measured in parts, not dollars.

    score = 35% availability            (share of observed time it could print)
          + 25% outage frequency        (red incidents per week)
          + 15% faults for its workload (fault incidents per black-toner point, vs the campus rate)
          + 15% parts wear for its workload (drums, belt and fuser used per black-toner point, vs campus)
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

WEIGHTS = {"availability": 0.35, "outages": 0.25, "faults": 0.15, "wear": 0.15, "warnings": 0.10}
PART_LABEL = {"availability": "Availability", "outages": "Outage frequency", "faults": "Faults for its workload",
              "wear": "Parts wear for its workload", "warnings": "Time in warning"}
MIN_DAYS = 3
PRIOR_USE = 25.0          # toner points of pseudo-usage at the campus rate (shrinkage strength)
GRADES = [(90, "A", "Great"), (80, "B", "Good"), (70, "C", "Fair"), (60, "D", "Needs attention"), (0, "F", "Problem")]


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
    # Drums, belt and fuser wear with every page but aren't swapped as often as toner: using them up
    # faster than the printer's black-toner use explains points at a mechanical problem.
    wear_parts = [c for c in config.COMPONENTS if not c.startswith("toner")]
    parts_used = per[wear_parts].sum(axis=1) / 100
    faults = M._in(ds.fault_inc, "start", start, end, ids).groupby("station_id").size()
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    yel = inc[inc["severity"] == "yellow"]
    yel_s = yel.assign(d=yel["duration_s"].fillna(0).clip(lower=0)).groupby("station_id")["d"].sum()

    out = sc.copy()
    sid = out["station_id"]
    out["observed_days"] = sid.map(days).fillna(0.0)
    out["use_k"] = sid.map(per["toner_k"]).fillna(0.0)
    out["faults"] = sid.map(faults).fillna(0).astype(int)
    out["parts_used"] = sid.map(parts_used).fillna(0.0)
    out["outages_per_week"] = out["red_incidents"] / out["observed_days"].replace(0, np.nan) * 7
    out["warning_share"] = (sid.map(yel_s).fillna(0.0) / (out["observed_h"] * 3600).replace(0, np.nan)).clip(0, 1)
    out["parts_per_month"] = out["parts_used"] / out["observed_days"].replace(0, np.nan) * 30.44
    usage = out["use_k"] / out["observed_days"].replace(0, np.nan)
    med_use = float(np.nanmedian(usage)) if usage.notna().any() else np.nan
    out["usage_relative"] = usage / med_use if med_use and np.isfinite(med_use) and med_use > 0 else np.nan

    # Workload-adjusted rates, shrunk toward the campus rate.
    tot_use = out["use_k"].sum()
    camp_fault = out["faults"].sum() / tot_use if tot_use > 0 else np.nan
    camp_wear = out["parts_used"].sum() / tot_use if tot_use > 0 else np.nan
    if np.isfinite(camp_fault) and camp_fault > 0:
        f_rate = (out["faults"] + PRIOR_USE * camp_fault) / (out["use_k"] + PRIOR_USE)
        out["faults_ratio"] = f_rate / camp_fault
    else:
        out["faults_ratio"] = np.where(out["faults"] > 0, 2.0, 1.0)
    if np.isfinite(camp_wear) and camp_wear > 0:
        w_rate = (out["parts_used"] + PRIOR_USE * camp_wear) / (out["use_k"] + PRIOR_USE)
        out["wear_ratio"] = w_rate / camp_wear
    else:
        out["wear_ratio"] = 1.0

    parts = pd.DataFrame({
        "availability": _linear(out["availability"].fillna(0), 85, 99.5),
        "outages": _linear(out["outages_per_week"].fillna(0), 4, 0),
        "faults": _ratio_score(out["faults_ratio"]),
        "wear": _ratio_score(out["wear_ratio"]),
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
    if d == "wear":
        return f"Wears out drums, belt or fuser {r['wear_ratio']:.1f}× as fast as the campus average for its workload."
    return f"In a warning state {r['warning_share']:.0%} of the time."


def summary(card: pd.DataFrame) -> dict:
    graded = card[card["score"].notna()]
    return {"graded": len(graded), "counts": graded["grade"].value_counts().to_dict(),
            "median": float(graded["score"].median()) if len(graded) else np.nan}
