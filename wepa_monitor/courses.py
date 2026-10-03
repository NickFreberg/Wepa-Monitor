"""Class schedules from BSU's public course search, to ask: does printing follow class times?

The course search (bridgew.edu/bsu-course-search) lists every section with its room ("HRG205"),
meeting days and times ("MWF 9:05am-9:55am") and dates. We keep only what the question needs:
building, days, start/end time, dates and status. No instructor names, no enrollment, nothing
about students. The listing is fetched page by page, two seconds apart, at most once a month
(and whenever a new term appears), with the same identifying User-Agent as the collector.

What it can and can't say: the course search gives sections, not head counts, so "class activity"
means sections meeting in a building at that hour. Printing activity comes from toner use, which is
coarse. The comparison is a correlation across hours of the week, not proof that a class caused a
print job.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

from . import config

URL = "https://www.bridgew.edu/bsu-course-search"
PARAMS = "field_v3credittype_value=All&field_v3campus_target_id=All&field_v3subject_target_id=All" \
         "&field_v3scheduletype_value=All&field_v3classdays_value=All&field_v3timeofday_value=All"
PATH = config.CAMPUS_DIR / "courses.csv"
REFRESH_DAYS = 30
PAGE_DELAY_S = 2.0
MAX_PAGES = 120

# Room-code prefixes -> buildings (as named in reference/buildings.csv where there's a printer).
BUILDINGS = {
    "LIB": "Maxwell Library", "DMF": "DMF Science & Math Center", "HRG": "Harrington Hall",
    "MKC": "Moakley Center", "HNT": "Hunt Hall", "ATC": "Tinsley Center", "RSU": "Rondileau Student Union",
    "BDN": "Boyden Hall", "ECC": "East Campus Commons", "TIL": "Tillinghast Hall", "HRT": "Hart Hall",
    "KLY": "Kelly Gymnasium", "ART": "Art Building", "BUR": "Burrill Avenue Building",
}
DAY = {"M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6}
COLUMNS = ["term", "code", "building", "room", "days", "start_min", "end_min", "start_date", "end_date", "status"]


def _get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT_S)
    r.raise_for_status()
    return r.text


def current_term(html: str) -> tuple[str, str] | None:
    """(term id, label) of the newest whole term in the search form, e.g. ('1508611', '2026 FALL - ALL')."""
    soup = BeautifulSoup(html, "lxml")
    sel = soup.select_one("select[name=field_v3termcode_target_id]")
    if sel is None:
        return None
    opts = [(o.get("value"), o.get_text(strip=True)) for o in sel.select("option")]
    whole = [o for o in opts if o[0] not in (None, "All") and o[1].upper().endswith("- ALL")]
    chosen = [o for o in sel.select("option") if o.has_attr("selected") and o.get("value") != "All"]
    if chosen and chosen[0].get_text(strip=True).upper().endswith("- ALL"):
        return chosen[0]["value"], chosen[0].get_text(strip=True)
    return whole[0] if whole else None


def _minutes(t: str) -> int | None:
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap])m", t.strip().lower())
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    return h * 60 + int(m.group(2))


def parse_page(html: str, term: str = "") -> pd.DataFrame:
    """Sections on one results page that meet in a room at set times."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table.cols-11 tbody tr"):
        cells = {re.sub(r"-table-column(--\d+)?$", "", td["headers"][0]).replace("view-field-v3", ""):
                 td.get_text(" ", strip=True) for td in tr.select("td") if td.get("headers")}
        loc, when = cells.get("campuslocation", ""), cells.get("coursetime", "")
        m = re.match(r"([A-Z]{2,4})(\w*)$", loc.replace(" ", ""))
        t = re.match(r"([MTWRFSU]+)\s+(\d{1,2}:\d{2}\s*[ap]m)\s*-\s*(\d{1,2}:\d{2}\s*[ap]m)", when, re.I)
        if not m or not t or m.group(1) == "WEB" or cells.get("campus", "Main Campus") != "Main Campus":
            continue
        code = m.group(1)[:3] if m.group(1)[:3] in BUILDINGS else m.group(1)
        dates = re.findall(r"(\d{2}/\d{2}/\d{4})", cells.get("startend", ""))
        rows.append({"term": term, "code": code, "building": BUILDINGS.get(code, code),
                     "room": loc.replace(" ", ""), "days": t.group(1).upper(),
                     "start_min": _minutes(t.group(2)), "end_min": _minutes(t.group(3)),
                     "start_date": dates[0] if dates else "", "end_date": dates[-1] if dates else "",
                     "status": cells.get("status", "")})
    return pd.DataFrame(rows, columns=COLUMNS)


def last_page(html: str) -> int:
    pages = [int(p) for p in re.findall(r"[?&;]page=(\d+)", html)]
    return max(pages) if pages else 0


def fetch(log=print) -> pd.DataFrame:
    first = _get(URL)
    term = current_term(first)
    if term is None:
        raise ValueError("couldn't find the term list on the course search page")
    tid, tlabel = term
    base = f"{URL}?field_v3termcode_target_id={tid}&{PARAMS}"
    html = _get(base)
    n = min(last_page(html), MAX_PAGES)
    frames = [parse_page(html, tlabel)]
    for page in range(1, n + 1):
        time.sleep(PAGE_DELAY_S)
        frames.append(parse_page(_get(f"{base}&page={page}"), tlabel))
    out = pd.concat(frames, ignore_index=True)
    log(f"courses: {len(out):,} in-person section meetings for {tlabel} from {n + 1} pages")
    return out


def _age_days() -> float:
    return (time.time() - PATH.stat().st_mtime) / 86400 if PATH.exists() else 1e9


def refresh(force: bool = False, log=print) -> str:
    """Monthly (or forced). Never raises; a failure keeps the previous file."""
    if not force and _age_days() < REFRESH_DAYS:
        return "fresh"
    try:
        df = fetch(log)
        if df.empty:
            return "failed: no sections parsed (page layout may have changed)"
        PATH.parent.mkdir(parents=True, exist_ok=True)
        df.assign(fetched=datetime.now(timezone.utc).date()).to_csv(PATH, index=False)
        load.cache_clear()
        return f"{len(df):,} section meetings"
    except Exception as exc:  # noqa: BLE001
        log(f"courses: refresh failed ({exc}); keeping the previous list")
        return f"failed: {exc}"



@lru_cache(maxsize=1)
def load() -> pd.DataFrame:
    path = PATH if PATH.exists() else config.REFERENCE_DIR / "courses.csv"
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype={"room": str, "days": str, "status": str})
    for c in ("start_date", "end_date"):
        df[c] = pd.to_datetime(df[c], format="%m/%d/%Y", errors="coerce")
    return df.dropna(subset=["start_min", "end_min"])


def weekly_grid(df: pd.DataFrame, on: pd.Timestamp | None = None, buildings=None) -> pd.DataFrame:
    """Sections in session by (building, weekday 0-6, hour 0-23): a section meeting 9:05-9:55 counts in
    hour 9; one meeting 2:00-3:15 counts in hours 14 and 15. Limited to sections running on `on`."""
    if df.empty:
        return pd.DataFrame(columns=["building", "weekday", "hour", "sections"])
    d = df
    if on is not None:
        day = pd.Timestamp(on).tz_localize(None).normalize() if pd.Timestamp(on).tzinfo else pd.Timestamp(on).normalize()
        d = d[(d["start_date"] <= day) & (d["end_date"] >= day)]
    if buildings is not None:
        d = d[d["building"].isin(buildings)]
    rows = []
    for r in d.itertuples(index=False):
        hours = range(int(r.start_min) // 60, int(max(r.end_min - 1, r.start_min)) // 60 + 1)
        for ch in str(r.days):
            if ch in DAY:
                rows += [(r.building, DAY[ch], h) for h in hours]
    if not rows:
        return pd.DataFrame(columns=["building", "weekday", "hour", "sections"])
    g = pd.DataFrame(rows, columns=["building", "weekday", "hour"]).value_counts().rename("sections").reset_index()
    return g


def class_vs_printing(ds, start, end, ids=None) -> dict | None:
    """Weekday hour-by-hour: sections in session on campus vs printing (toner use), and for each
    printer building with classes, how closely its printing follows its own class schedule."""
    from . import metrics as M
    df = load()
    if df.empty:
        return None
    use = M.usage_in(ds, start, end, ids)
    use = use[use["component"].str.startswith("toner")]
    if use.empty:
        return None
    local = use["scrape_ts"].dt.tz_convert(config.LOCAL_TZ)
    use = use.assign(weekday=local.dt.dayofweek, hour=local.dt.hour,
                     building=use["station_id"].map(ds.stations.set_index("station_id")["building"]))
    use = use[use["weekday"] < 5]
    on = (start + (end - start) / 2).tz_convert(config.LOCAL_TZ)
    grid = weekly_grid(df, on)
    grid = grid[grid["weekday"] < 5]
    if grid.empty:
        return None
    days = max(1.0, ((end - start).total_seconds() / 86400) * 5 / 7)
    hours = pd.DataFrame({"hour": range(7, 23)})
    cls = grid.groupby("hour")["sections"].sum() / 5
    prt = use.groupby("hour")["used"].sum() / days
    hours["sections"] = hours["hour"].map(cls).fillna(0.0)
    hours["printing"] = hours["hour"].map(prt).fillna(0.0)
    # Index both to their own average (=100) so they share one axis honestly.
    for c in ("sections", "printing"):
        m = hours[c].mean()
        hours[f"{c}_idx"] = hours[c] / m * 100 if m > 0 else np.nan
    # Per building: correlation over the weekday x hour grid (7 am - 10 pm).
    per = []
    for b, g in grid.groupby("building"):
        u = use[use["building"] == b]
        if u.empty:
            continue
        idx = pd.MultiIndex.from_product([range(5), range(7, 23)], names=["weekday", "hour"])
        cs = g.set_index(["weekday", "hour"])["sections"].reindex(idx, fill_value=0)
        ps = u.groupby(["weekday", "hour"])["used"].sum().reindex(idx, fill_value=0)
        if cs.std() == 0 or ps.std() == 0:
            continue
        rho = float(pd.Series(cs.values).corr(pd.Series(ps.values), method="spearman"))
        before = []          # printing in the hour before classes start vs other hours
        starts = df[df["building"] == b]
        start_hours = {(DAY[ch], int(r.start_min) // 60) for r in starts.itertuples() for ch in str(r.days)
                       if ch in DAY and DAY[ch] < 5}
        pre = [(d, h - 1) for d, h in start_hours if h - 1 >= 7]
        if pre:
            before = ps.reindex(pre).mean() / max(ps.mean(), 1e-9)
        per.append({"building": b, "sections": int(starts.shape[0]), "rho": rho,
                    "pre_class_ratio": float(before) if pre else np.nan})
    per = pd.DataFrame(per).sort_values("rho", ascending=False) if per else pd.DataFrame(
        columns=["building", "sections", "rho", "pre_class_ratio"])
    overall = float(hours["sections"].corr(hours["printing"], method="spearman")) if hours["printing"].std() > 0 else np.nan
    return {"hours": hours, "buildings": per, "rho": overall, "term": str(df["term"].iloc[0]) if len(df) else "",
            "sections": int(len(df))}
