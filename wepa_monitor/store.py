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
import os
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


# Finished days are parquet and never change, so each is read from disk once per process
# (keyed by path and modification time). Only today's CSV is re-read on every refresh.
_PARQUET_CACHE: dict[Path, tuple[int, pd.DataFrame]] = {}


def _read_parquet_cached(path: Path) -> pd.DataFrame:
    mtime = path.stat().st_mtime_ns
    hit = _PARQUET_CACHE.get(path)
    if hit is None or hit[0] != mtime:
        hit = _PARQUET_CACHE[path] = (mtime, pd.read_parquet(path))
    return hit[1]


# A day's data is its own file (<day>.parquet once finished, <day>.csv while still being appended)
# plus any number of imported files, imports/<snapshots|scrape_log>/<day>.<tag>.parquet: history
# collected on another computer before this one started (`python -m wepa_monitor export`). They
# are merged on load and duplicate minutes are dropped, so importing never overwrites or conflicts
# with live files, and code that predates imports simply doesn't see them.
def _day_of(p: Path) -> str:
    return p.name.split(".", 1)[0]


def _import_dir(folder: Path) -> Path:
    return folder.parent / "imports" / folder.name


def _days(folder: Path) -> list[str]:
    found = {_day_of(p) for p in folder.iterdir() if p.suffix in (".csv", ".parquet")} if folder.exists() else set()
    imp = _import_dir(folder)
    if imp.exists():
        found |= {_day_of(p) for p in imp.glob("*.parquet")}
    return sorted(found)


def _imports(folder: Path, day: str) -> list[Path]:
    imp = _import_dir(folder)
    return sorted(imp.glob(f"{day}.*.parquet")) if imp.exists() else []


def snapshot_listing(data_dir: Path) -> dict[str, int]:
    """name -> modification time of every snapshot file, in one pass (cheap on network storage)."""
    out = {}
    for folder, prefix in ((data_dir / "snapshots", ""), (data_dir / "imports" / "snapshots", "imports/")):
        if folder.exists():
            out.update({prefix + e.name: e.stat().st_mtime_ns for e in os.scandir(folder)
                        if e.name.endswith((".csv", ".parquet"))})
    return out


def day_signature(data_dir: Path, day: str, listing: dict[str, int] | None = None) -> tuple:
    """Which files make up a day, and when they changed (to notice imports into finished days)."""
    listing = snapshot_listing(data_dir) if listing is None else listing
    return tuple(sorted((n, m) for n, m in listing.items() if n.rsplit("/", 1)[-1].split(".", 1)[0] == day))


def _read_partitions(folder: Path, str_cols: dict) -> pd.DataFrame | None:
    if not folder.exists() and not _import_dir(folder).exists():
        return None
    frames = []
    for day in _days(folder):
        pq, csv = folder / f"{day}.parquet", folder / f"{day}.csv"
        if pq.exists():
            frames.append(_read_parquet_cached(pq))
        elif csv.exists():
            frames.append(pd.read_csv(csv, dtype=str_cols))
        frames += [_read_parquet_cached(f) for f in _imports(folder, day)]
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


_SNAP_STR = {"station_id": str, "status_codes": str, "printer_text": str, "description": str}


def snapshot_days(data_dir: Path) -> list[str]:
    return _days(data_dir / "snapshots")


def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """One snapshot per station per minute, even if two collectors briefly overlap (e.g. during a redeploy)."""
    minute = df["scrape_ts"].dt.floor("min")
    return df[~pd.DataFrame({"s": df["station_id"], "m": minute}).duplicated()]


def load_day(data_dir: Path, day: str) -> pd.DataFrame:
    """One day's snapshots, typed, deduplicated and sorted by station then time."""
    folder = data_dir / "snapshots"
    pq, csv = folder / f"{day}.parquet", folder / f"{day}.csv"
    frames = [pd.read_parquet(pq)] if pq.exists() else [pd.read_csv(csv, dtype=_SNAP_STR)] if csv.exists() else []
    frames += [pd.read_parquet(f) for f in _imports(folder, day)]
    if not frames:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    df = _dedupe(_typed_snapshots(pd.concat(frames, ignore_index=True)))
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


_LOG_CACHE: dict[Path, tuple[tuple, pd.DataFrame]] = {}


def load_scrape_log(data_dir: Path) -> pd.DataFrame:
    """The scrape log, typed and in time order. Finished (parquet) days are typed and combined once
    per process; only today's CSV is read on each refresh."""
    folder = data_dir / "scrape_log"
    imported = sorted(_import_dir(folder).glob("*.parquet")) if _import_dir(folder).exists() else []
    if not folder.exists() and not imported:
        return pd.DataFrame(columns=LOG_COLUMNS)
    own = sorted(folder.glob("*.parquet")) if folder.exists() else []
    finished = own + imported
    key = tuple((f.name, f.stat().st_mtime_ns) for f in finished)
    hit = _LOG_CACHE.get(folder)
    if hit is None or hit[0] != key:
        frames = [pd.read_parquet(f) for f in finished]
        hist = _typed_log(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame(columns=LOG_COLUMNS)
        hit = _LOG_CACHE[folder] = (key, hist.sort_values("attempt_ts", ignore_index=True))
    done = {f.stem for f in own}
    today = [pd.read_csv(f, dtype={"error": str}) for f in sorted(folder.glob("*.csv")) if f.stem not in done] \
        if folder.exists() else []
    if not today:
        out = hit[1] if len(hit[1]) else pd.DataFrame(columns=LOG_COLUMNS)
    else:
        new = _typed_log(pd.concat(today, ignore_index=True))
        parts = [f for f in (hit[1], new) if len(f)]
        out = pd.concat(parts, ignore_index=True).sort_values("attempt_ts", kind="stable", ignore_index=True)
    if imported and len(out):
        # Imported history can overlap this collector's: keep one attempt per minute, a success if any.
        minute = out["attempt_ts"].dt.floor("min")
        keep = (out.assign(_m=minute).sort_values(["_m", "ok"], ascending=[True, False], kind="stable")
                .drop_duplicates("_m").index)
        out = out.loc[sorted(keep)].reset_index(drop=True)
    return out


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


def export_for_import(data_dir: Path, out_dir: Path, tag: str) -> list[str]:
    """Package this computer's collected data as import files (imports/.../<day>.<tag>.parquet) for another
    collector's data folder. They sit beside that collector's own files and are merged on load."""
    if not tag.isalnum():
        raise ValueError("tag must be letters and digits only")
    written = []
    for sub, typer, str_cols in (("snapshots", _typed_snapshots, _SNAP_STR), ("scrape_log", _typed_log, {"error": str})):
        folder = data_dir / sub
        if not folder.exists():
            continue
        for day in sorted({_day_of(p) for p in folder.iterdir() if p.suffix in (".csv", ".parquet")}):
            pq, csv = folder / f"{day}.parquet", folder / f"{day}.csv"
            df = pd.read_parquet(pq) if pq.exists() else pd.read_csv(csv, dtype=str_cols) if csv.exists() else None
            if df is None or df.empty:
                continue
            dest = out_dir / "imports" / sub / f"{day}.{tag}.parquet"
            dest.parent.mkdir(parents=True, exist_ok=True)
            typer(df).to_parquet(dest, index=False)
            written.append(f"imports/{sub}/{dest.name} ({len(df):,} rows)")
    return written
