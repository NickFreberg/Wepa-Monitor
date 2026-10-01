"""Building-level map data and Google Earth (KML) export.

Coordinates come from reference/buildings.csv (OpenStreetMap footprint
centroids). Each building's map state is its worst station: red beats yellow
beats "no data" beats green, so a building shows "down" if any printer in it is.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

import pandas as pd

from . import rules
from .metrics import Dataset
from .ops import current_status

STATE_RANK = {"red": 3, "yellow": 2, "stale": 1, "green": 0}
STATE_LABEL = {"red": "Down", "yellow": "Warning", "stale": "No data", "green": "Printing"}


def _station_line(r) -> str:
    issues = [rules.code_label(c) for c in str(r["status_codes"]).split(",") if c]
    issues += [m for m in str(r["printer_text"]).split(" | ") if m]
    text = f"{r['description']} (#{r['station_id']}): {STATE_LABEL.get(r['state'], r['state'])}"
    return text + (f" - {'; '.join(issues)}" if issues else "")


def building_points(ds: Dataset, ids=None) -> pd.DataFrame:
    """One row per building: coordinates, worst state, station count and a per-station summary."""
    cur = current_status(ds, ids)
    if cur.empty:
        return pd.DataFrame(columns=["building", "short_name", "lat", "lon", "campus", "state", "stations", "down",
                                     "lines", "area", "target"])
    geo = ds.stations.drop_duplicates("building").set_index("building")[["short_name", "lat", "lon", "campus"]]
    cur = cur.assign(rank=cur["state"].map(STATE_RANK).fillna(0), line=cur.apply(_station_line, axis=1))
    rows = []
    for building, g in cur.groupby("building", sort=True):
        worst = g.loc[g["rank"].idxmax(), "state"]
        rows.append({
            "building": building,
            "short_name": geo["short_name"].get(building) or building,
            "lat": geo["lat"].get(building), "lon": geo["lon"].get(building),
            "campus": geo["campus"].get(building, ""),
            "area": g["area"].iloc[0],
            "state": worst,
            "stations": len(g),
            "down": int((g["state"] == "red").sum()),
            "lines": list(g.sort_values("station_id")["line"]),
            # Clicking a building opens its station, or the station list for multi-printer buildings.
            "target": (f"/station/{g['station_id'].iloc[0]}" if len(g) == 1
                       else f"/stations?q={building}"),
        })
    return pd.DataFrame(rows)


# Google Earth paddle icons, one per state; labels carry the state too.
_KML_ICON = {
    "red": "http://maps.google.com/mapfiles/kml/paddle/red-circle.png",
    "yellow": "http://maps.google.com/mapfiles/kml/paddle/ylw-circle.png",
    "stale": "http://maps.google.com/mapfiles/kml/paddle/wht-circle.png",
    "green": "http://maps.google.com/mapfiles/kml/paddle/grn-circle.png",
}


def to_kml(points: pd.DataFrame, title: str) -> str:
    """A KML document that opens in Google Earth (desktop or web: Projects -> Import KML file)."""
    styles = "".join(
        f'<Style id="{state}"><IconStyle><scale>1.1</scale><Icon><href>{href}</href></Icon></IconStyle>'
        f'<LabelStyle><scale>0.8</scale></LabelStyle></Style>'
        for state, href in _KML_ICON.items())
    marks = []
    for _, p in points.dropna(subset=["lat", "lon"]).iterrows():
        desc = "<br/>".join(escape(line) for line in p["lines"])
        name = f"{p['building']} - {STATE_LABEL.get(p['state'], p['state'])}"
        marks.append(
            f"<Placemark><name>{escape(name)}</name><styleUrl>#{p['state']}</styleUrl>"
            f"<description><![CDATA[<b>{escape(p['building'])}</b> · {escape(str(p['area']))}"
            f"<br/>{p['stations']} print station(s)<br/><br/>{desc}]]></description>"
            f"<Point><coordinates>{p['lon']:.6f},{p['lat']:.6f},0</coordinates></Point></Placemark>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{escape(title)}</name>{styles}{''.join(marks)}</Document></kml>\n")
