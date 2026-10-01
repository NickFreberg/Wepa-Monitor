"""Append-only snapshot storage.

Layout under a data directory:

    snapshots/YYYY-MM-DD.csv       today's snapshots, appended every minute
    snapshots/YYYY-MM-DD.parquet   finished days, compacted to columnar files
    scrape_log/YYYY-MM-DD.csv|.parquet   one row per scrape attempt, ok or not
    html/YYYY-MM-DD/HHMMSS.html.gz optional raw page archive (re-parse later)
    meta.json                      {"synthetic": true} for generated demo data

Raw snapshots are never edited. Every metric is derived from them, so a
business-rule change can be re-applied to the full history.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config
from .scrape import ScrapeResult

SNAPSHOT_COLUMNS = [
    "scrape_ts", "page_ts", "section", "station_id", "description",
    "row_status", "status_codes", "printer_text", *config.COMPONENTS,
]
LOG_COLUMNS = ["attempt_ts", "ok", "http_status", "duration_ms", "n_stations", "error"]


def _day(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _append_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, mode="a", header=not path.exists(), index=False)


def append_result(data_dir: Path, result: ScrapeResult, raw_html: str | None = None) -> None:
    day = _day(result.attempt_ts)
    log = pd.DataFrame([{
        "attempt_ts": result.attempt_ts.isoformat(),
        "ok": result.ok,
        "http_status": result.http_status,
        "duration_ms": result.duration_ms,
        "n_stations": len(result.records),
        "error": result.error,
    }], columns=LOG_COLUMNS)
    _append_csv(data_dir / "scrape_log" / f"{day}.csv", log)

    if result.ok and result.records:
        snap = pd.DataFrame(result.records)
        snap["scrape_ts"] = result.attempt_ts.isoformat()
        snap["page_ts"] = result.page_ts.isoformat() if result.page_ts else None
        _append_csv(data_dir / "snapshots" / f"{day}.csv", snap[SNAPSHOT_COLUMNS])

    if raw_html is not None:
        path = data_dir / "html" / day / f"{result.attempt_ts:%H%M%S}.html.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(raw_html)


def _typed_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    df["scrape_ts"] = pd.to_datetime(df["scrape_ts"], utc=True, format="ISO8601")
    df["page_ts"] = pd.to_datetime(df["page_ts"], utc=True, format="ISO8601")
    for col in ("section", "station_id", "description", "row_status", "status_codes", "printer_text"):
        df[col] = df[col].fillna("").astype(str)
    for comp in config.COMPONENTS:
        df[comp] = pd.to_numeric(df[comp], errors="coerce").astype("Float32")
    return df


def _typed_log(df: pd.DataFrame) -> pd.DataFrame:
    df["attempt_ts"] = pd.to_datetime(df["attempt_ts"], utc=True, format="ISO8601")
    df["ok"] = df["ok"].astype(str).str.lower().isin(["true", "1"])
    df["n_stations"] = pd.to_numeric(df["n_stations"], errors="coerce").fillna(0).astype(int)
    df["error"] = df["error"].fillna("").astype(str)
    return df


def _read_partitions(folder: Path, str_cols: dict) -> pd.DataFrame | None:
    if not folder.exists():
        return None
    frames = []
    days = sorted({p.stem for p in folder.iterdir() if p.suffix in (".csv", ".parquet")})
    for day in days:
        pq, csv = folder / f"{day}.parquet", folder / f"{day}.csv"
        if pq.exists():
            frames.append(pd.read_parquet(pq))
        elif csv.exists():
            frames.append(pd.read_csv(csv, dtype=str_cols))
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


_SNAP_STR = {"station_id": str, "status_codes": str, "printer_text": str, "description": str}


def snapshot_days(data_dir: Path) -> list[str]:
    folder = data_dir / "snapshots"
    if not folder.exists():
        return []
    return sorted({p.stem for p in folder.iterdir() if p.suffix in (".csv", ".parquet")})


def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """One snapshot per station per minute, even if two collectors briefly overlap (e.g. during a redeploy)."""
    minute = df["scrape_ts"].dt.floor("min")
    return df[~pd.DataFrame({"s": df["station_id"], "m": minute}).duplicated()]


def load_day(data_dir: Path, day: str) -> pd.DataFrame:
    """One day's snapshots, typed, deduplicated and sorted by station then time."""
    folder = data_dir / "snapshots"
    pq, csv = folder / f"{day}.parquet", folder / f"{day}.csv"
    if pq.exists():
        df = pd.read_parquet(pq)
    elif csv.exists():
        df = pd.read_csv(csv, dtype=_SNAP_STR)
    else:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    df = _dedupe(_typed_snapshots(df))
    return df.sort_values(["station_id", "scrape_ts"], ignore_index=True)


def load_snapshots(data_dir: Path) -> pd.DataFrame:
    df = _read_partitions(data_dir / "snapshots", {"station_id": str, "status_codes": str,
                                                   "printer_text": str, "description": str})
    if df is None:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    df = _dedupe(_typed_snapshots(df))
    # Low-cardinality text as categories: ~5x less memory on months of history.
    for col in ("section", "station_id", "description", "row_status", "status_codes", "printer_text"):
        df[col] = df[col].astype("category")
    return df.sort_values(["station_id", "scrape_ts"], ignore_index=True)


def load_scrape_log(data_dir: Path) -> pd.DataFrame:
    df = _read_partitions(data_dir / "scrape_log", {"error": str})
    if df is None:
        return pd.DataFrame(columns=LOG_COLUMNS)
    return _typed_log(df).sort_values("attempt_ts", ignore_index=True)


def write_day(data_dir: Path, day: str, snapshots: pd.DataFrame, log: pd.DataFrame) -> None:
    """Write one finished day directly as parquet (used by compaction and the demo generator)."""
    for sub, df in (("snapshots", snapshots), ("scrape_log", log)):
        folder = data_dir / sub
        folder.mkdir(parents=True, exist_ok=True)
        df.to_parquet(folder / f"{day}.parquet", index=False)


def compact(data_dir: Path) -> list[str]:
    """Convert finished days' CSVs to parquet. Today's file stays CSV for appends."""
    today = _day(datetime.now(timezone.utc))
    done = []
    for sub, typer, str_cols in (
        ("snapshots", _typed_snapshots, {"station_id": str, "status_codes": str,
                                         "printer_text": str, "description": str}),
        ("scrape_log", _typed_log, {"error": str}),
    ):
        folder = data_dir / sub
        if not folder.exists():
            continue
        for csv in sorted(folder.glob("*.csv")):
            if csv.stem >= today:
                continue
            typer(pd.read_csv(csv, dtype=str_cols)).to_parquet(csv.with_suffix(".parquet"), index=False)
            csv.unlink()
            done.append(f"{sub}/{csv.stem}")
    return done


def read_meta(data_dir: Path) -> dict:
    path = data_dir / "meta.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_meta(data_dir: Path, meta: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
