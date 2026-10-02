"""Campus intelligence from bridgew.edu.

Public university pages give the printer data its context:

* Academic calendar (registrar): classes, holidays, breaks, reading days, finals and
  commencement for the next few academic years. Refreshed yearly (it is published years ahead).
* Residence-hall schedule (Residence Life): move-in, and when halls close and reopen for
  winter, spring and summer break. Only the current year is published, so other years are
  inferred from the academic calendar with the same offsets (and marked as inferred).
* Residence-hall sizes (Residence Life): how many students live in each hall.
* Maxwell Library opening hours (LibCal): published several weeks ahead, refreshed weekly
  and accumulated, so past days keep the hours they actually had.

Everything is parsed into small CSV files under reference/ so the dashboard works offline,
and `python -m wepa_monitor campus` (or the collector, on its own schedule) refreshes them.
The parsers are strict about structure: if a page changes shape they raise instead of
silently writing nonsense, and the previous CSV stays in place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

from . import config

SOURCES = {
    "calendar": "https://www.bridgew.edu/office/registrar/academic-calendar",
    "halls_schedule": "https://www.bridgew.edu/student-life/residence-life-housing/moving-in-moving-out-process",
    "halls": "https://www.bridgew.edu/student-life/residence-life-housing/residence-halls",
    "library": "https://bridgew.libcal.com/hours",
}
# Refreshed files are written to config.CAMPUS_DIR (on Azure, persistent storage beside the
# collected data) and read from there first, falling back to the copies committed in reference/.
CAL_PATH = config.CAMPUS_DIR / "academic_calendar.csv"
HALLS_PATH = config.CAMPUS_DIR / "residence_halls.csv"
LIBRARY_PATH = config.CAMPUS_DIR / "library_hours.csv"


def _src(path: Path) -> Path:
    """The file to read: the refreshed copy if there is one, else the committed reference copy."""
    return path if path.exists() else config.REFERENCE_DIR / path.name

MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                      "Nov", "Dec"], start=1)}
# Residence Life's hall names -> buildings in reference/buildings.csv
HALL_NAMES = {"Shea and Durgin Halls": "Shea/Durgin", "Great Hill Student Apartments": "Great Hill Apartments"}


class ParseError(ValueError):
    """The page no longer has the structure the parser expects."""


# --- fetching ------------------------------------------------------------------------------------------

def _get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT_S)
    r.raise_for_status()
    return r.text


# --- academic calendar -----------------------------------------------------------------------------------

KINDS = [  # (kind, pattern) checked in order against the event text
    ("snow_day", r"snow day"),
    ("finals_start", r"final examinations begin"),
    ("finals_end", r"final examinations end"),
    ("evening_final", r"evening class final exam"),
    ("reading_day", r"reading day"),
    ("thanksgiving_start", r"thanksgiving recess begins"),
    ("thanksgiving", r"thanksgiving recess"),
    ("break_start", r"spring break begins"),
    ("break_end", r"spring break ends"),
    ("holiday", r"no classes"),
    ("classes_resume", r"classes resume"),
    ("summer_begin", r"summer session [iv]+ classes begin"),
    ("summer_end", r"summer session [iv]+ classes end"),
    ("classes_begin", r"classes begin|first day of classes"),
    ("classes_end", r"last day of instruction"),
    ("commencement", r"commencement"),
    ("schedule_swap", r"class schedule|schedule of classes"),
    ("quarter", r"quarter (begins|ends)"),
]


def _kind(text: str) -> str:
    t = text.lower()
    return next((k for k, p in KINDS if re.search(p, t)), "other")


def parse_academic_calendar(html: str) -> pd.DataFrame:
    """One row per dated entry: date, term, event, kind."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for h2 in soup.find_all("h2"):
        m = re.match(r"(Fall|Spring|Summer)\s+(?:Semester|Sessions?)\s+(\d{4})", h2.get_text(" ", strip=True))
        if not m:
            continue
        term, year = f"{m.group(1)} {m.group(2)}", int(m.group(2))
        ul = h2.find_next_sibling("ul")
        if ul is None:
            raise ParseError(f"{term}: no list of dates under the heading")
        for li in ul.find_all("li"):
            if li.find("ul"):
                continue                                  # a month wrapper, not an entry
            text = " ".join(li.get_text(" ", strip=True).split())
            d = re.match(r"([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2})\s*\(\s*\w+\s*\)\s*(.+)", text)
            if not d or d.group(1) not in MONTHS:
                continue
            event = d.group(3).strip().rstrip(".")
            rows.append({"date": date(year, MONTHS[d.group(1)], int(d.group(2))), "term": term,
                         "event": event, "kind": _kind(event), "source": "registrar"})
    if len(rows) < 20:
        raise ParseError(f"only {len(rows)} calendar entries found; the page layout may have changed")
    return pd.DataFrame(rows).drop_duplicates(["date", "event"]).sort_values("date", ignore_index=True)


def parse_halls_schedule(html: str) -> pd.DataFrame:
    """Move-in and residence-hall closing/opening dates (published for the current year only)."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        m = re.search(r"([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})", cells[0])
        if not m or m.group(1) not in MONTHS:
            continue
        desc = cells[1].lower()
        kind = ("move_in" if "move in" in desc else "halls_close" if "close" in desc else
                "halls_open" if "open" in desc else None)
        if kind:
            rows.append({"date": date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))), "term": "",
                         "event": cells[1].rstrip("."), "kind": kind, "source": "residence-life"})
    return pd.DataFrame(rows, columns=["date", "term", "event", "kind", "source"])


def parse_hall_sizes(html: str) -> pd.DataFrame:
    """Residents per hall: each hall is an <h2> followed by 'houses 408 upper-class students'."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for h2 in soup.find_all("h2"):
        name = h2.get_text(" ", strip=True)
        if not re.search(r"(Hall|Halls|Apartments)$", name) or h2.get("class"):
            continue
        texts = []
        for el in h2.find_all_next(string=True):
            if el.find_parent("h2") is not None and el.find_parent("h2") is not h2:
                break
            texts.append(el.strip())
        block = "\n".join(t for t in texts if t)
        m = re.search(r"hous(?:es|ing) (?:approximately )?(\d[\d,]*)", block)
        standing = re.search(r"Class Standings:\s*\n([^\n]+)", block)
        rows.append({"building": HALL_NAMES.get(name, name),
                     "residents": int(m.group(1).replace(",", "")) if m else None,
                     "who": ", ".join(dict.fromkeys(w.strip().replace("-", " ").capitalize()
                                                    for w in standing.group(1).split(","))) if standing else "",
                     "source": "residence-life"})
    df = pd.DataFrame(rows, columns=["building", "residents", "who", "source"])
    if len(df) < 5:
        raise ParseError(f"only {len(df)} residence halls found")
    df["estimated"] = False
    # Halls without a stated count share what's left of the page's campus total equally
    # (Miles and DiNardo are described as mirror images of each other).
    total = re.search(r"Approximately ([\d,]+) undergraduate students live on campus", soup.get_text(" ", strip=True))
    missing = df["residents"].isna()
    if total and missing.any():
        rest = int(total.group(1).replace(",", "")) - df["residents"].sum()
        if rest > 0:
            df.loc[missing, "residents"] = round(rest / missing.sum())
            df.loc[missing, "estimated"] = True
    df["residents"] = df["residents"].astype("Int64")
    return df


def parse_library_hours(html: str, today: date) -> pd.DataFrame:
    """Maxwell Library opening hours by date from the LibCal weekly tables."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for table in soup.select("table.s-lc-h-w"):
        heads = [h.select_one(".s-lc-h-head-date") for h in table.select("thead th")]
        days = [h.get_text(" ", strip=True) if h else None for h in heads]
        for tr in table.select("tr.s-lc-h-loc"):
            name = tr.select_one(".s-lc-h-locname")
            if name is None or "maxwell library" not in name.get_text(" ", strip=True).lower():
                continue
            cells = tr.find_all("td")[1:8]
            for day_txt, cell in zip(days[-7:], cells):
                m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})", day_txt or "")
                if not m:
                    continue
                mo = MONTHS[m.group(1)]
                year = today.year + (1 if mo < today.month - 6 else -1 if mo > today.month + 6 else 0)
                txt = cell.get_text(" ", strip=True)
                if txt.strip("–- ") == "":
                    continue                          # not published yet
                o, c = _hours(txt)
                rows.append({"date": date(year, mo, int(m.group(2))), "open": o, "close": c})
    if not rows:
        raise ParseError("no Maxwell Library hours found")
    return pd.DataFrame(rows).drop_duplicates("date", keep="last").sort_values("date", ignore_index=True)


def _clock(t: str) -> float:
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t.strip().lower())
    if not m:
        raise ParseError(f"unreadable time {t!r}")
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    return h + int(m.group(2) or 0) / 60


def _hours(text: str) -> tuple[float | None, float | None]:
    t = text.lower()
    if "closed" in t or not t:
        return None, None
    if "24 hours" in t:
        return 0.0, 24.0
    parts = re.split(r"\s*[–-]\s*", text)
    if len(parts) != 2:
        raise ParseError(f"unreadable hours {text!r}")
    o, c = _clock(parts[0]), _clock(parts[1])
    return o, (c if c > o else c + 24)            # "2pm – 1am" closes after midnight


# --- refresh ----------------------------------------------------------------------------------------------

def _age_days(path: Path) -> float:
    return (datetime.now().timestamp() - path.stat().st_mtime) / 86400 if path.exists() else float("inf")


def refresh(force: bool = False, log=print) -> dict:
    """Fetch whatever is due. Calendar and hall data yearly, library hours weekly. Never raises:
    a failure keeps the previous file and is reported in the result."""
    done: dict[str, str] = {}

    def step(name, due, fn):
        if not (force or due):
            done[name] = "fresh"
            return
        try:
            done[name] = fn()
        except Exception as exc:                    # network or layout change: keep the old file
            done[name] = f"failed: {exc}"
        log(f"campus {name}: {done[name]}")

    def calendar():
        cal = parse_academic_calendar(_get(SOURCES["calendar"]))
        halls = parse_halls_schedule(_get(SOURCES["halls_schedule"]))
        prev = _src(CAL_PATH)
        merged = pd.concat([_load_csv(prev)[lambda d: d["source"] == "residence-life"] if prev.exists()
                            else None, cal, halls]).drop_duplicates(["date", "event"], keep="last")
        merged = merged.sort_values("date", ignore_index=True)
        merged.to_csv(CAL_PATH, index=False)
        return f"{len(cal)} calendar entries, {len(halls)} residence-hall dates"

    def hall_sizes():
        df = parse_hall_sizes(_get(SOURCES["halls"]))
        df.to_csv(HALLS_PATH, index=False)
        return f"{df['residents'].notna().sum()} halls with resident counts"

    def library():
        new = parse_library_hours(_get(SOURCES["library"]), date.today())
        prev = _src(LIBRARY_PATH)
        old = _load_csv(prev).drop(columns="fetched", errors="ignore") if prev.exists() else new.iloc[:0]
        # Keep past days as recorded; let the newest fetch win for today and later.
        keep = old[old["date"] < new["date"].min()]
        out = pd.concat([keep, new]).sort_values("date", ignore_index=True)
        out.assign(fetched=datetime.now(timezone.utc).date()).to_csv(LIBRARY_PATH, index=False)
        return f"{len(new)} days of hours ({new['date'].min()} to {new['date'].max()})"

    config.CAMPUS_DIR.mkdir(parents=True, exist_ok=True)
    step("calendar", _age_days(CAL_PATH) > config.CAMPUS_CALENDAR_REFRESH_DAYS, calendar)
    step("halls", _age_days(HALLS_PATH) > config.CAMPUS_CALENDAR_REFRESH_DAYS, hall_sizes)
    step("library", _age_days(LIBRARY_PATH) > config.CAMPUS_LIBRARY_REFRESH_DAYS, library)
    _cached.cache_clear()
    return done


def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


# --- derived: what kind of day is it? -----------------------------------------------------------------------

PHASES = {   # phase -> (label, printing demand hint used by the narrative)
    "classes": "Classes in session",
    "move_in": "Move-in",
    "holiday": "Holiday (no classes)",
    "reading": "Reading day",
    "finals": "Finals",
    "thanksgiving": "Thanksgiving recess",
    "spring_break": "Spring break",
    "winter_break": "Winter break",
    "summer": "Summer",
    "summer_session": "Summer session",
}


@dataclass
class Campus:
    events: pd.DataFrame       # date, term, event, kind, source (+ inferred residence-hall dates)
    days: pd.DataFrame         # one row per date: phase, label, halls_open, event, library_open, library_close
    halls: pd.DataFrame        # building, residents, who

    @property
    def empty(self) -> bool:
        return self.days.empty

    def on(self, d) -> pd.Series | None:
        d = pd.Timestamp(d).date() if not isinstance(d, date) else d
        return self.days.loc[d] if d in self.days.index else None

    def range(self, start: date, end: date) -> pd.DataFrame:
        return self.days.loc[(self.days.index >= start) & (self.days.index <= end)]

    def upcoming(self, after: date, days: int = 45) -> pd.DataFrame:
        e = self.events[(self.events["date"] > after) & (self.events["date"] <= after + timedelta(days=days))]
        return e[e["kind"].isin(["holiday", "thanksgiving_start", "break_start", "reading_day", "finals_start",
                                 "commencement", "classes_begin", "classes_end", "move_in", "halls_close",
                                 "halls_open"])]

    def closed_dates(self) -> set[str]:
        """Days the support desks are assumed closed: calendar holidays plus Massachusetts state holidays."""
        hol = self.events[self.events["kind"].isin(["holiday", "thanksgiving"])]["date"]
        years = {d.year for d in self.days.index} if len(self.days) else set()
        out = {d.isoformat() for d in hol}
        for y in years:
            out |= {d.isoformat() for d in state_holidays(y)}
        return out


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def state_holidays(year: int) -> list[date]:
    """Massachusetts state holidays (offices closed). An assumption where the calendar is silent."""
    return [_observed(date(year, 1, 1)), _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
            _nth_weekday(year, 4, 0, 3), _last_weekday(year, 5, 0), _observed(date(year, 6, 19)),
            _observed(date(year, 7, 4)), _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2),
            _observed(date(year, 11, 11)), _nth_weekday(year, 11, 3, 4), _observed(date(year, 12, 25))]


def _infer_halls(ev: pd.DataFrame) -> pd.DataFrame:
    """Residence-hall dates for years Residence Life hasn't published, using its usual offsets:
    close the day after finals, reopen 3 days before classes; close the Friday before spring
    break and reopen the Sunday after; move in 4 days before fall classes."""
    rows = []
    by = {k: ev[ev["kind"] == k] for k in ("finals_end", "classes_begin", "break_start", "break_end")}
    for r in by["finals_end"].drop_duplicates("term").itertuples():
        rows.append((r.date + timedelta(days=1), "Residence halls close (inferred)", "halls_close"))
    for r in by["classes_begin"].itertuples():
        if r.term.startswith("Spring"):
            rows.append((r.date - timedelta(days=3), "Residence halls open (inferred)", "halls_open"))
        elif r.term.startswith("Fall"):
            rows.append((r.date - timedelta(days=4), "Move-in begins (inferred)", "move_in"))
            rows.append((r.date - timedelta(days=4), "Residence halls open (inferred)", "halls_open"))
    for r in by["break_start"].itertuples():
        rows.append((r.date - timedelta(days=3), "Residence halls close (inferred)", "halls_close"))
    for r in by["break_end"].itertuples():
        rows.append((r.date + timedelta(days=2), "Residence halls open (inferred)", "halls_open"))
    out = pd.DataFrame(rows, columns=["date", "event", "kind"]).assign(term="", source="inferred")
    published = ev[ev["source"] == "residence-life"]
    if len(published):     # where Residence Life published a date, it wins over the inference
        near = lambda d, k: ((published["kind"] == k) & ((published["date"] - d).map(abs) <= timedelta(days=10))).any()
        out = out[[not near(d, k) for d, k in zip(out["date"], out["kind"])]]
    return out


def build(events: pd.DataFrame, halls: pd.DataFrame | None = None, library: pd.DataFrame | None = None) -> Campus:
    ev = pd.concat([events, _infer_halls(events)], ignore_index=True).sort_values("date", ignore_index=True)
    if ev.empty:
        return Campus(ev, pd.DataFrame(), halls if halls is not None else pd.DataFrame())
    first = date(ev["date"].min().year, 1, 1)
    last = date(ev["date"].max().year, 12, 31)
    idx = pd.Index([first + timedelta(days=i) for i in range((last - first).days + 1)], name="date")
    phase = pd.Series("summer", index=idx, dtype=object)
    label = pd.Series("", index=idx, dtype=object)

    def span(a, b, value):
        phase.loc[(phase.index >= a) & (phase.index <= b)] = value

    for term, g in ev.groupby("term"):
        if not term:
            continue
        d = {k: sorted(g.loc[g["kind"] == k, "date"]) for k in g["kind"].unique()}
        if term.startswith("Summer"):
            for a, b in zip(d.get("summer_begin", []), d.get("summer_end", [])):
                span(a, b, "summer_session")
            continue
        begin, end = d.get("classes_begin", [None])[0], d.get("classes_end", [None])[0]
        if begin and end:
            # The whole term, weekends included, is 'classes' until the overlays below.
            span(begin, max([end] + d.get("finals_end", [])), "classes")
        if d.get("finals_start") and d.get("finals_end"):
            span(d["finals_start"][0], d["finals_end"][0], "finals")
        for r in d.get("reading_day", []):
            span(r, r, "reading")
        for h in d.get("holiday", []):
            span(h, h, "holiday")
        if d.get("thanksgiving_start") and d.get("classes_resume"):
            span(d["thanksgiving_start"][0] + timedelta(days=1), d["classes_resume"][0] - timedelta(days=1),
                 "thanksgiving")
        if d.get("break_start") and d.get("break_end"):
            span(d["break_start"][0] - timedelta(days=2), d["break_end"][0] + timedelta(days=2), "spring_break")
    # Winter break: between a fall term's finals and the next spring's first class day.
    fe = ev[(ev["kind"] == "finals_end") & ev["term"].str.startswith("Fall")].drop_duplicates("term")
    for r in fe.itertuples():
        nxt = ev[(ev["kind"] == "classes_begin") & ev["term"].str.startswith("Spring") & (ev["date"] > r.date)]
        if len(nxt):
            span(r.date + timedelta(days=1), nxt["date"].min() - timedelta(days=1), "winter_break")
    begins = sorted(ev.loc[ev["kind"] == "classes_begin", "date"])
    for r in ev[ev["kind"] == "move_in"].itertuples():
        nxt = [b for b in begins if b > r.date]
        if nxt and phase.loc[r.date] == "summer":
            span(r.date, nxt[0] - timedelta(days=1), "move_in")
    for r in ev.itertuples():
        label.loc[r.date] = (label.loc[r.date] + "; " if label.loc[r.date] else "") + r.event

    # Residence halls open/closed, from the close/open events in date order.
    halls_open = pd.Series(True, index=idx)
    marks = ev[ev["kind"].isin(["halls_close", "halls_open"])].sort_values("date")
    state, since = True, first
    for r in marks.itertuples():
        if r.kind == "halls_close" and state:
            state, since = False, r.date
        elif r.kind == "halls_open" and not state:
            halls_open.loc[(halls_open.index >= since) & (halls_open.index < r.date)] = False
            state = True
    if not state:
        halls_open.loc[halls_open.index >= since] = False

    days = pd.DataFrame({"phase": phase, "label": phase.map(PHASES), "event": label, "halls_open": halls_open})
    if library is not None and len(library):
        lib = library.set_index("date")
        days["library_open"] = pd.to_numeric(lib["open"].reindex(days.index), errors="coerce")
        days["library_close"] = pd.to_numeric(lib["close"].reindex(days.index), errors="coerce")
        days["library_known"] = days.index.isin(lib.index)
    return Campus(ev, days, halls if halls is not None else pd.DataFrame())


@lru_cache(maxsize=1)
def _cached(stamp: tuple) -> Campus:
    cal, hall, lib = _src(CAL_PATH), _src(HALLS_PATH), _src(LIBRARY_PATH)
    events = _load_csv(cal) if cal.exists() else pd.DataFrame(columns=["date", "term", "event", "kind",
                                                                                     "source"])
    halls = pd.read_csv(hall) if hall.exists() else None
    library = _load_csv(lib) if lib.exists() else None
    return build(events, halls, library)


def load() -> Campus:
    """The campus context, rebuilt only when one of the reference files changes."""
    stamp = tuple((str(p), p.stat().st_mtime) if p.exists() else (str(p), 0)
                  for p in map(_src, (CAL_PATH, HALLS_PATH, LIBRARY_PATH)))
    return _cached(stamp)


def describe(c: Campus, start: date, end: date) -> str:
    """'Finals week (Dec 14–18)', 'Spring break', or 'classes in session' for a date range."""
    r = c.range(start, end)
    if r.empty:
        return ""
    top = r["phase"].value_counts()
    main = top.index[0]
    return PHASES.get(main, main) if top.iloc[0] >= len(r) / 2 else ", ".join(PHASES.get(p, p) for p in top.index[:2])


# --- exposure: was anyone around to be affected? ------------------------------------------------------------

LIBRARY_BUILDING = "Maxwell Library"


def in_use_seconds(c: Campus, building: str, station_type: str, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Seconds of [start, end) when the station's building was in use: residence printers only
    while the halls are open, Maxwell Library printers only during library hours (where
    published), everything else the whole time."""
    if pd.isna(start) or pd.isna(end) or end <= start:
        return 0.0
    total = (end - start).total_seconds()
    if c.empty or (station_type != "residence" and building != LIBRARY_BUILDING):
        return total
    tz = config.LOCAL_TZ
    s, e = start.tz_convert(tz), end.tz_convert(tz)
    out, day = 0.0, s.normalize()
    while day <= e:
        nxt = (day + pd.Timedelta(days=1, hours=2)).normalize()
        lo, hi = max(s, day), min(e, nxt)
        if hi > lo:
            row = c.on(day.date())
            if row is None:
                out += (hi - lo).total_seconds()
            elif station_type == "residence":
                out += (hi - lo).total_seconds() if row["halls_open"] else 0.0
            elif not row.get("library_known", False):
                out += (hi - lo).total_seconds()
            elif pd.notna(row.get("library_open")):
                base = day.tz_localize(None)
                o = pd.Timestamp(base + timedelta(hours=float(row["library_open"]))).tz_localize(tz)
                cl = pd.Timestamp(base + timedelta(hours=float(row["library_close"]))).tz_localize(tz)
                a, b = max(lo, o), min(hi, cl)
                out += max(0.0, (b - a).total_seconds())
        day = nxt
    return out


def annotate_exposure(inc: pd.DataFrame, stations: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Add in_use_s: downtime while the building was in use (see in_use_seconds)."""
    out = inc.copy()
    if out.empty:
        out["in_use_s"] = pd.Series(dtype=float)
        return out
    c = load()
    if _EXPOSURE_FOR[0] is not c:                 # campus data changed: recompute everything
        _EXPOSURE.clear()
        _EXPOSURE_FOR[0] = c
    ref = stations.set_index("station_id")
    b = out["station_id"].map(ref["building"]).fillna("")
    t = out["station_id"].map(ref["station_type"]).fillna("") if "station_type" in ref else pd.Series("", index=out.index)
    finished = out["end"].notna() if "end" in out else pd.Series(False, index=out.index)
    ends = out["end"].fillna(as_of) if "end" in out else pd.Series(as_of, index=out.index)
    vals = []
    for bb, tt, s_, e_, done in zip(b, t, out["start"], ends, finished):
        key = (bb, tt, s_, e_)
        v = _EXPOSURE.get(key) if done else None
        if v is None:
            v = in_use_seconds(c, bb, tt, s_, e_)
            if done:                              # finished incidents never change
                _EXPOSURE[key] = v
        vals.append(v)
    out["in_use_s"] = vals
    return out


_EXPOSURE: dict[tuple, float] = {}
_EXPOSURE_FOR: list = [None]


def residents_per_printer(c: Campus, stations: pd.DataFrame) -> pd.DataFrame:
    """Residence halls with their resident count and how many printers serve them."""
    if c.halls is None or c.halls.empty:
        return pd.DataFrame(columns=["building", "residents", "printers", "per_printer", "estimated", "who"])
    n = stations[stations["station_type"] == "residence"].groupby("building").size().rename("printers")
    h = c.halls.merge(n, left_on="building", right_index=True, how="inner")
    h["per_printer"] = h["residents"].astype(float) / h["printers"]
    return h.sort_values("per_printer", ascending=False, ignore_index=True)
