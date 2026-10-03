"""Which downtime actually cost students printing? Downtime weighted by how busy the printer usually is.

An hour down at 3 AM and an hour down at noon on a weekday are not the same loss. Each down hour is
weighted by the printing usually done on that printer at that hour of the week, measured from toner
use (Wepa doesn't publish page counts, so toner is the usage signal):

    weight(printer, slot) = campus_shape(slot) x printer_scale(printer)

campus_shape: average toner used per printer in each of the 168 hours of the week, divided by its
mean (1 = an average hour). printer_scale: the printer's toner use per observed hour divided by the
typical (median) printer's. The product is shrunk this way, rather than estimated per printer and
slot, because one printer's hour-of-week cells are too sparse to trust.

"Busy-weighted hours" = sum of down hours x weight, in units of "an hour down for a typical printer
at an average time". The weighting is a model of demand, not a count of people: it shows where
downtime landed on busy printers at busy times, not how many students were turned away.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, metrics as M


def _slot(ts: pd.Series) -> pd.Series:
    loc = ts.dt.tz_convert(config.LOCAL_TZ)
    return loc.dt.dayofweek * 24 + loc.dt.hour


def weights(ds: M.Dataset, history_days: int = 56) -> tuple[pd.Series, pd.Series] | None:
    """(campus_shape by slot 0-167, printer_scale by station) from recent toner use."""
    end = ds.as_of
    start = max(end - pd.Timedelta(days=history_days), ds.data_start) if ds.data_start is not None else end
    use = M.usage_in(ds, start, end)
    use = use[use["component"] == "toner_k"]
    hrs = M._hours(ds, start, end, None)
    if use.empty or hrs.empty or use["used"].sum() <= 0:
        return None
    hrs = hrs[hrs["covered_s"] > 0]
    slot_hours = hrs.assign(slot=_slot(hrs["hour"])).groupby("slot")["covered_s"].sum() / 3600
    slot_use = use.assign(slot=_slot(use["scrape_ts"])).groupby("slot")["used"].sum()
    per = (slot_use.reindex(range(168), fill_value=0) / slot_hours.reindex(range(168))).fillna(0.0)
    shape = per / per[per > 0].mean() if (per > 0).any() else pd.Series(1.0, index=range(168))
    st_hours = hrs.groupby("station_id")["covered_s"].sum() / 3600
    st_use = use.groupby("station_id")["used"].sum().reindex(st_hours.index, fill_value=0.0)
    rate = st_use / st_hours.replace(0, np.nan)
    med = float(rate[rate > 0].median()) if (rate > 0).any() else np.nan
    scale = (rate / med).fillna(0.0) if np.isfinite(med) and med > 0 else pd.Series(1.0, index=st_hours.index)
    return shape, scale


def by_station(ds: M.Dataset, start, end, ids=None) -> pd.DataFrame:
    """Per printer: plain down hours and busy-weighted down hours, worst (weighted) first."""
    w = weights(ds)
    hrs = M._hours(ds, start, end, ids)
    if w is None or hrs.empty:
        return pd.DataFrame()
    shape, scale = w
    hrs = hrs.assign(down_h=(hrs["covered_s"] - hrs["up_s"]).clip(lower=0) / 3600)
    hrs = hrs[hrs["down_h"] > 0]
    if hrs.empty:
        return pd.DataFrame()
    hrs = hrs.assign(weight=_slot(hrs["hour"]).map(shape).fillna(0).values * hrs["station_id"].map(scale).fillna(0).values)
    hrs["weighted_h"] = hrs["down_h"] * hrs["weight"]
    g = hrs.groupby("station_id").agg(down_h=("down_h", "sum"), weighted_h=("weighted_h", "sum")).reset_index()
    st = ds.stations.set_index("station_id")
    g["label"] = g["station_id"].map(st["label"])
    g["building"] = g["station_id"].map(st["building"])
    g["busy_share"] = g["weighted_h"] / g["weighted_h"].sum() if g["weighted_h"].sum() > 0 else np.nan
    g["plain_share"] = g["down_h"] / g["down_h"].sum()
    g["rank_plain"] = g["down_h"].rank(ascending=False, method="min").astype(int)
    g["rank_busy"] = g["weighted_h"].rank(ascending=False, method="min").astype(int)
    return g.sort_values("weighted_h", ascending=False).reset_index(drop=True)


def summary(t: pd.DataFrame) -> str:
    if t.empty:
        return "No downtime to weigh in this period."
    top = t.iloc[0]
    moved = t[(t["rank_busy"] <= 5) & (t["rank_plain"] > 5)]
    s = (f"Weighted by how busy each printer usually is at that hour, {top['label']} lost the most printing "
         f"({top['busy_share']:.0%} of the busy-weighted total, vs {top['plain_share']:.0%} of plain down hours).")
    if len(moved):
        s += (" " + ", ".join(moved["label"].head(3)) + (" rises" if len(moved) == 1 else " rise") +
              " into the top five once it's counted how busy the printer is and when it was down.")
    return s
