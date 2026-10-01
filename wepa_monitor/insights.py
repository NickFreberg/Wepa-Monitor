"""Period comparisons and plain-English observations for the executive summary.

Each observation is generated only when the evidence behind it clears the
same gating rules as the metrics; otherwise it is simply not shown.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config, metrics as M

OVERNIGHT = (23, 8)   # local hours when no desk is staffed (approximate)


@dataclass
class Period:
    label: str
    start: pd.Timestamp
    end: pd.Timestamp


def months(ds: M.Dataset) -> list[Period]:
    if ds.data_start is None:
        return []
    tz = config.LOCAL_TZ
    first = ds.data_start.tz_convert(tz).tz_localize(None).to_period("M")
    last = ds.as_of.tz_convert(tz).tz_localize(None).to_period("M")
    out = []
    for p in pd.period_range(first, last, freq="M"):
        start = max(p.start_time.tz_localize(tz).tz_convert("UTC"), ds.data_start)
        end = min((p + 1).start_time.tz_localize(tz).tz_convert("UTC"), ds.as_of)
        label = p.strftime("%B %Y") + (" (to date)" if end == ds.as_of else "")
        out.append(Period(label, start, end))
    return out


def ytd(ds: M.Dataset) -> Period:
    tz = config.LOCAL_TZ
    jan1 = pd.Timestamp(year=ds.as_of.tz_convert(tz).year, month=1, day=1, tz=tz).tz_convert("UTC")
    start = max(jan1, ds.data_start)
    since = "" if start == jan1 else f" (since {start.tz_convert(tz):%b %-d})"
    return Period(f"Year to date{since}", start, ds.as_of)


def scorecard(ds: M.Dataset, p: Period, ids=None) -> dict:
    burn = M.burn_rates(ds, p.start, p.end, ids).set_index("component")
    red = M._in(ds.sev_inc, "start", p.start, p.end, ids)
    return {
        "availability": M.availability(ds, p.start, p.end, ids),
        "red_incidents": int((red["severity"] == "red").sum()),
        "mttr_red": M.mttr(ds, "red", p.start, p.end, ids),
        "mttr_yellow": M.mttr(ds, "yellow", p.start, p.end, ids),
        "mtbf": M.mtbf(ds, p.start, p.end, ids),
        "paper": M.paper_refill_time(ds, p.start, p.end, ids),
        "dq": M.data_quality(ds, p.start, p.end),
        "units": burn["used_units"].to_dict(),
    }


def _hour(h: int) -> str:
    return f"{(h % 12) or 12} {'am' if h < 12 else 'pm'}"


def _fmt_dur(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


def observations(ds: M.Dataset, cur: Period, prev: Period | None, ids=None) -> list[str]:
    out: list[str] = []
    a = M.availability(ds, cur.start, cur.end, ids)
    if a.ok:
        line = f"Fleet availability was {a.value:.1f}% in {cur.label}"
        if prev:
            b = M.availability(ds, prev.start, prev.end, ids)
            if b.ok:
                d = a.value - b.value
                line += f", {'up' if d >= 0 else 'down'} {abs(d):.1f} pts from {prev.label.split(' (')[0]}"
        out.append(line + f" ({a.extra['down_h']:,.0f} printer-hours down).")

        sc = M.station_scorecard(ds, cur.start, cur.end, ids)
        sc = sc[sc["observed_h"] >= 24]
        if not sc.empty:
            worst = sc.sort_values("availability").iloc[0]
            if a.value - worst["availability"] >= 3:
                out.append(f"{worst['label']} had the lowest availability at {worst['availability']:.1f}% "
                           f"({worst['red_incidents']} red incidents, most often {str(worst['top_fault']).lower()}).")

    f = M.faults_in(ds, cur.start, cur.end, ids)
    if len(f) >= config.MIN_INCIDENTS_FOR_MEAN:
        top = f["label"].value_counts()
        hours = f.loc[f["label"] == top.index[0], "hour"]
        by_hour = hours.value_counts().reindex(range(24), fill_value=0).to_numpy()
        window = np.array([by_hour[[h, (h + 1) % 24, (h + 2) % 24]].sum() for h in range(24)])
        h0 = int(window.argmax())
        share = window[h0] / max(1, len(hours))
        line = f"{top.index[0]} was the most frequent fault ({top.iloc[0]} of {len(f)} fault incidents)"
        if len(hours) >= 10 and share >= 0.25:     # only mention a clear peak
            line += f"; {share:.0%} of them started between {_hour(h0)} and {_hour((h0 + 3) % 24)}"
        out.append(line + ".")

    red = M._in(ds.sev_inc, "start", cur.start, cur.end, ids)
    red = M._resolved(red[red["severity"] == "red"])
    if len(red):
        hour = red["start"].dt.tz_convert(config.LOCAL_TZ).dt.hour
        night = (hour >= OVERNIGHT[0]) | (hour < OVERNIGHT[1])
        if night.sum() >= 3 and (~night).sum() >= 3:
            n_med = red.loc[night, "duration_s"].median() / 60
            d_med = red.loc[~night, "duration_s"].median() / 60
            if n_med > 2 * d_med:
                out.append(f"Red incidents that start overnight (11 pm-8 am) take a median {_fmt_dur(n_med)} "
                           f"to clear vs {_fmt_dur(d_med)} during the day - the main driver of downtime.")

    burn = M.burn_rates(ds, cur.start, cur.end, ids).set_index("component")
    k = burn.loc["toner_k", "used_units"]
    if k > 0:
        line = f"The fleet used {k:.1f} black-toner cartridges' worth of toner"
        if prev:
            pk = M.burn_rates(ds, prev.start, prev.end, ids).set_index("component").loc["toner_k", "per_day"]
            ck = burn.loc["toner_k", "per_day"]
            if pk and pk > 0 and np.isfinite(ck):
                ratio = ck / pk
                change = (f"was {ratio:.1f}× the rate" if ratio >= 2 else
                          f"{'rose' if ratio >= 1 else 'fell'} {abs(ratio - 1):.0%} vs the rate")
                line += f"; the daily burn rate {change} in {prev.label.split(' (')[0]}"
        out.append(line + ".")

    r = M.replacements(ds, cur.start, cur.end, ids)
    toner = r[r["component"].str.startswith("toner")]
    if len(toner) >= config.MIN_INCIDENTS_FOR_MEAN:
        early = toner[toner["level_before"] > config.CONSUMABLE_REPLACE_PCT]
        out.append(f"{len(toner)} toner cartridges were replaced at an average of {toner['level_before'].mean():.0f}% "
                   f"remaining; {len(early)} were swapped above {config.CONSUMABLE_REPLACE_PCT}%, "
                   f"discarding about {(early['level_before'].sum() / 100):.1f} cartridges' worth of toner.")

    t = M._in(ds.tray_inc, "start", cur.start, cur.end, ids)
    if len(t):
        hrs = t.groupby("station_id")["duration_s"].sum() / 3600
        sid = hrs.idxmax()
        if hrs.max() >= 24:
            label = ds.stations.set_index("station_id").loc[sid, "label"]
            out.append(f"{label} had at least one empty paper tray for {hrs.max():,.0f} hours - "
                       "a candidate for a larger refill or an extra round.")

    dq = M.data_quality(ds, cur.start, cur.end)
    if dq.value is not None:
        out.append(f"The monitor captured {dq.extra['completeness']:.1f}% of expected snapshots "
                   f"({dq.extra['failures']:,} failed refreshes, {dq.extra['failure_rate']:.1f}%).")
    return out


def demand_forecast(ds: M.Dataset, days: int = 30, ids=None) -> pd.DataFrame:
    """Parts needed over the next `days`, from the recent burn rate."""
    start = ds.as_of - pd.Timedelta(days=config.BURN_RATE_LOOKBACK_DAYS)
    burn = M.burn_rates(ds, start, ds.as_of, ids)
    burn["next_units"] = burn["per_day"] * days / 100
    return burn[["component", "label", "per_day", "next_units", "ok"]]
