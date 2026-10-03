"""Helpers shared by every view."""
from __future__ import annotations

import pandas as pd
from dash import dcc, html

from ... import config, events, metrics as M, rules
from ..components import level_bar, status_pill

AREA_ORDER = ["Upper Great Hill", "University Park", "West Side", "TBD", "Academic", "Satellite", "Unassigned"]
PERIODS = {"7": "7 days", "30": "30 days", "90": "90 days", "all": "All time"}


def scope_ids(ds: M.Dataset, scope: dict | None):
    """Station IDs the header filter selects (sections AND areas AND picked stations), or None
    for every station."""
    scope = scope or {}
    sections, areas, picked = scope.get("sections") or [], scope.get("areas") or [], scope.get("stations") or []
    if not (sections or areas or picked):
        return None
    ids = ds.ids(section=sections or None, area=areas or None)
    if picked:
        keep = set(picked)
        ids = [i for i in ids if i in keep]
    return ids


def scope_label(ds: M.Dataset, scope: dict | None) -> str:
    """How sentences name the selection: 'BSU print stations', 'residence hall stations', 'Weygand Hall'."""
    scope = scope or {}
    sections, areas, picked = scope.get("sections") or [], scope.get("areas") or [], scope.get("stations") or []
    if picked:
        if len(picked) == 1:
            st = ds.stations.set_index("station_id")
            return st.loc[picked[0], "description"] if picked[0] in st.index else "the selected station"
        return "the selected stations"
    chosen = sections + areas
    if not chosen:
        return "BSU print stations"
    if len(chosen) == 1:
        c = chosen[0]
        return {"ResNet": "residence hall stations", "Student Computer Labs": "lab stations",
                "Satellite Campuses": "satellite stations"}.get(c, f"{c} stations")
    return "the selected stations"


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


def _unstaffed(row) -> str:
    """'Support unavailable until …' for a printer that isn't operational while its desk is closed."""
    if row.get("state") not in ("red", "yellow"):
        return ""
    from ... import vocab
    return vocab.support_substate(row.get("owner") or config.DEFAULT_OWNER, pd.Timestamp(row["scrape_ts"]))


def station_card(row, levels: dict, in_group: bool = False) -> dcc.Link:
    """A station tile on the Stations page; the whole card opens the drill-through. Toner and drum
    levels sit side by side, one line per color, so a single worn drum can't hide behind the others."""
    msgs = station_messages(row)
    head = [html.Div([html.Div(row["description"], className="station__name"),
                      html.Div(f"#{row['station_id']}" + ("" if in_group else f" · {row['building']}"),
                               className="station__meta")]),
            status_pill(row["state"])]
    sub = _unstaffed(row)
    return dcc.Link(href=f"/station/{row['station_id']}", className=f"station station--{row['state']}", children=[
        html.Div(className="station__head", children=head),
        html.Div(sub, className="station__sub") if sub else None,
        html.Ul([html.Li(m) for m in msgs], className="station__msgs") if msgs else
        html.Div("No alerts", className="station__ok"),
        html.Div(className="station__levels", children=[
            html.Div([html.Div("Toner", className="station__lvl-head"),
                      *[level_bar(c, levels.get(f"toner_{c.lower()}")) for c in "KCMY"]], className="station__col"),
            html.Div([html.Div("Drums", className="station__lvl-head"),
                      *[level_bar(c, levels.get(f"drum_{c.lower()}")) for c in "KCMY"]], className="station__col"),
            html.Div([level_bar("Belt", levels.get("belt"), low=5, critical=2),
                      level_bar("Fuser", levels.get("fuser"), low=5, critical=2)], className="station__col station__col--wide"),
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
