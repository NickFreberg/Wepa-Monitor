"""Helpers shared by every view."""
from __future__ import annotations

import pandas as pd
from dash import dcc, html

from ... import config, events, metrics as M, rules
from ..components import level_bar, status_pill

AREA_ORDER = ["Upper Great Hill", "University Park", "West Side", "TBD", "Academic", "Satellite", "Unassigned"]
PERIODS = {"7": "7 days", "30": "30 days", "90": "90 days", "all": "All time"}


def scope_ids(ds: M.Dataset, sections, areas):
    if not sections and not areas:
        return None
    return ds.ids(section=sections or None, area=areas or None)


def period_window(ds: M.Dataset, period) -> tuple[pd.Timestamp, pd.Timestamp]:
    return M.window(ds, None if period in (None, "all") else int(period))


def period_label(period) -> str:
    return PERIODS.get(str(period), "30 days").lower() if period != "all" else "all recorded time"


def empty(msg: str, big: bool = True) -> html.Div:
    return html.Div(msg, className="empty" + (" empty--page" if big else ""))


def needs_days(frame: pd.DataFrame, fig_fn, min_days: int = 2):
    """(figure, body) for chart_card: a trend only once there are enough days to draw it."""
    if frame is None or frame.empty or frame["local_date"].nunique() < min_days:
        return None, empty(f"Collecting data. This trend appears once {min_days} days are recorded.", big=False)
    return fig_fn(), None


def area_key(area: str) -> int:
    return AREA_ORDER.index(area) if area in AREA_ORDER else 99


def station_messages(row) -> list[str]:
    """What's wrong, in plain words: 'Paper jam (paper feed)', 'Tray 2 empty'."""
    return rules.describe(row["status_codes"], row["printer_text"])


def station_card(row, levels: dict) -> dcc.Link:
    """A station tile on the Stations page; the whole card opens the drill-through."""
    msgs = station_messages(row)
    drum = [levels.get(c) for c in ("drum_k", "drum_c", "drum_m", "drum_y") if levels.get(c) is not None]
    return dcc.Link(href=f"/station/{row['station_id']}", className=f"station station--{row['state']}", children=[
        html.Div(className="station__head", children=[
            html.Div([html.Div(row["description"], className="station__name"),
                      html.Div(f"#{row['station_id']} · {row['building']}", className="station__meta")]),
            status_pill(row["state"]),
        ]),
        html.Ul([html.Li(m) for m in msgs], className="station__msgs") if msgs else
        html.Div("No alerts", className="station__ok"),
        html.Div(className="station__levels", children=[
            level_bar("K", levels.get("toner_k")), level_bar("C", levels.get("toner_c")),
            level_bar("M", levels.get("toner_m")), level_bar("Y", levels.get("toner_y")),
            level_bar("Drum", min(drum) if drum else None),
            level_bar("Belt", levels.get("belt"), low=5, critical=2),
            level_bar("Fuser", levels.get("fuser"), low=5, critical=2),
        ]),
    ])


def status_segments(ds: M.Dataset, station_id: str, start, end) -> pd.DataFrame:
    """Contiguous (start, end, state) pieces for one station, with 'nodata' filling gaps."""
    st = ds.status[ds.status["station_id"] == station_id]
    if st.empty:
        return pd.DataFrame(columns=["start", "end", "state"])
    r = events.runs(st["station_id"], st["scrape_ts"], st["row_status"], ds.as_of, st["brk"].to_numpy(dtype=bool))
    interval = pd.Timedelta(seconds=config.EXPECTED_INTERVAL_S)
    seg_end = r["end"].where(r["status"] == "resolved",
                             r["last_seen"].where(r["status"] != "open", ds.as_of) + interval)
    segs = pd.DataFrame({"start": r["start"], "end": seg_end, "state": r["state"]})
    gaps = pd.DataFrame({"start": segs["end"].iloc[:-1].values, "end": segs["start"].iloc[1:].values,
                         "state": "nodata"})
    gaps = gaps[(pd.to_datetime(gaps["end"], utc=True) - pd.to_datetime(gaps["start"], utc=True))
                > pd.Timedelta(seconds=config.MAX_OBSERVED_GAP_S)]
    out = pd.concat([segs, gaps], ignore_index=True)
    out["start"] = pd.to_datetime(out["start"], utc=True).clip(lower=start, upper=end)
    out["end"] = pd.to_datetime(out["end"], utc=True).clip(lower=start, upper=end)
    return out[out["end"] > out["start"]].sort_values("start", ignore_index=True)
