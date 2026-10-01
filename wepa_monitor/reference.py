"""Station reference data: building, area and (optional) coordinates.

The status page only knows station IDs and free-text descriptions. Grouping
by building and area comes from reference/stations.csv, which you maintain.
Stations that appear on the page but not in the CSV still work: they are
grouped under their own description with area "Unassigned".
"""
from __future__ import annotations

import pandas as pd

from . import config


def load_buildings() -> pd.DataFrame:
    """Building coordinates (OpenStreetMap footprint centroids; see reference/buildings.csv)."""
    path = config.REFERENCE_DIR / "buildings.csv"
    if not path.exists():
        return pd.DataFrame(columns=["building", "short_name", "lat", "lon", "campus", "source", "osm_ref", "notes"])
    text = {c: str for c in ("building", "short_name", "campus", "source", "osm_ref", "notes")}
    df = pd.read_csv(path, dtype=text)
    df = df.fillna({"campus": "", "source": "", "osm_ref": "", "notes": ""})
    df["short_name"] = df["short_name"].fillna(df["building"])
    return df


def load_stations() -> pd.DataFrame:
    df = pd.read_csv(config.REFERENCE_DIR / "stations.csv", dtype=str).fillna("")
    geo = load_buildings()[["building", "short_name", "lat", "lon", "campus"]]
    return df.merge(geo, on="building", how="left")


def station_table(snap: pd.DataFrame) -> pd.DataFrame:
    """Every station seen in the data, with its latest description and reference fields."""
    cols = ["station_id", "section", "description"]
    if snap.empty:
        latest = pd.DataFrame(columns=cols)
    else:
        latest = (snap.groupby("station_id", sort=False, observed=True)[["section", "description"]]
                  .last().reset_index().astype(str))
    ref = load_stations()
    out = latest.merge(ref, on="station_id", how="outer")
    out["description"] = out["description"].fillna(out["building"])
    out["building"] = out["building"].where(out["building"].fillna("") != "", out["description"])
    out["area"] = out["area"].where(out["area"].fillna("") != "", "Unassigned")
    out["section"] = out["section"].fillna("Unknown")
    out["label"] = out["description"] + " (" + out["station_id"] + ")"
    return out[out["station_id"].isin(latest["station_id"]) | latest.empty].reset_index(drop=True)
