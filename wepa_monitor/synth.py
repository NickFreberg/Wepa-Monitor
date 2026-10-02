"""Synthetic snapshot generator for demo mode.

Produces data in exactly the format the live scraper writes, so every metric
and chart runs unchanged on it. Everything it emits is fabricated; the data
directory is stamped {"synthetic": true} and the dashboard shows a DEMO banner.

The model is deliberately simple and explainable:

* Pages printed per minute ~ Poisson(rate), where rate = station baseline x
  hour-of-day profile x weekday factor x academic-calendar factor. The calendar
  factor comes from BSU's real academic calendar and residence-hall schedule
  (campus.py): finals are busiest, breaks quiet, and hall printers go almost idle
  while the halls are closed.
* Every page drains toner K, drums, belt and fuser; color pages also drain
  toner C/M/Y. Yields are typical of a mid-range color laser.
* Paper drains Tray1 first, then Tray2. One empty tray is only a printer-text
  warning (as seen on the live page); all trays empty is a red PAPER OUT.
* Staff refill trays on rounds and respond to red alerts only while the owning
  team's desk is open (ResNet or the IT Service Center; see config.SUPPORT_TEAMS),
  so problems that start after hours wait for the next shift.
* Jams, network drops, fatal errors and stockouts happen at modest rates.
* The scraper itself fails occasionally and has a couple of longer outages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import campus, config, store, support
from .reference import load_stations
from .scrape import parse_status_page

FIXTURE = config.ROOT / "tests" / "fixtures" / "status_page_2026-10-01.html"

# Pages per page-point: how many pages one percentage point of each part lasts.
PAGES_PER_PT = {
    "toner_k": 120, "toner_c": 85, "toner_m": 85, "toner_y": 85,   # per color page for CMY
    "drum_k": 450, "drum_c": 420, "drum_m": 420, "drum_y": 420,
    "belt": 600, "fuser": 800,
}
REPLACE_AT = {"toner": 4, "drum": 3, "fuser": 2}

HOURLY = {
    "residence": np.array([.5, .3, .15, .05, .03, .03, .05, .15, .4, .7, .9, 1.0,
                           1.0, .9, .85, .8, .8, .85, .9, 1.0, 1.0, .95, .85, .7]),
    "lab": np.array([0, 0, 0, 0, 0, 0, .05, .3, .8, 1.0, 1.0, 1.0,
                     .95, 1.0, 1.0, .9, .7, .5, .35, .3, .25, .15, .05, 0]),
    "satellite": np.array([0, 0, 0, 0, 0, 0, 0, .2, .7, 1.0, 1.0, .9,
                           .8, .9, .9, .7, .4, .2, .1, 0, 0, 0, 0, 0]),
}
WEEKEND = {"residence": 0.8, "lab": 0.3, "satellite": 0.1}
# Who responds to each kind of station: its desk hours come from config.SUPPORT_TEAMS.
OWNER = {"residence": "ResNet", "lab": "IT Service Center", "satellite": "IT Service Center"}


# Demand by point in the academic year, from the real BSU calendar (campus.py) when it covers
# the date: (residence halls, labs/satellite). Residence demand collapses when halls are closed.
PHASE_FACTOR = {"classes": (1.0, 1.0), "move_in": (1.25, 0.3), "holiday": (0.8, 0.25), "reading": (1.3, 1.2),
                "finals": (1.4, 1.35), "thanksgiving": (0.25, 0.1), "spring_break": (0.15, 0.15),
                "winter_break": (0.06, 0.1), "summer": (0.04, 0.12), "summer_session": (0.04, 0.3)}


def academic_factor(d: date, kind: str) -> float:
    day = _CAMPUS.on(d) if _CAMPUS is not None and not _CAMPUS.empty else None
    if day is not None:
        res, lab = PHASE_FACTOR.get(day["phase"], (1.0, 1.0))
        if kind == "residence":
            return res if day["halls_open"] else 0.02
        return lab
    return _fallback_factor(d, kind)


_CAMPUS = None


def _fallback_factor(d: date, kind: str) -> float:
    md = (d.month, d.day)
    summer = (5, 18) <= md <= (8, 31)
    winter = md >= (12, 20) or md <= (1, 20)
    thanksgiving = d.month == 11 and 24 <= d.day <= 30
    spring_break = d.month == 3 and 9 <= d.day <= 15
    if summer:
        return 0.04 if kind == "residence" else 0.15
    if winter:
        return 0.06 if kind == "residence" else 0.1
    if thanksgiving or spring_break:
        return 0.25
    if (9, 1) <= md <= (9, 10) or (1, 21) <= md <= (1, 28):   # move-in ramp
        return 1.25
    if (12, 8) <= md <= (12, 19) or (5, 1) <= md <= (5, 17):  # finals
        return 1.4
    return 1.0


@dataclass
class _Clock:
    local_hour: np.ndarray
    local_clock: np.ndarray     # fractional local hour, e.g. 9.5
    weekday: np.ndarray
    weekend: np.ndarray
    local_date: list


def _clock(minutes: pd.DatetimeIndex) -> _Clock:
    local = minutes.tz_convert(config.LOCAL_TZ)
    return _Clock(np.asarray(local.hour), np.asarray(local.hour + local.minute / 60), np.asarray(local.dayofweek),
                  np.asarray(local.dayofweek >= 5), list(local.date))


def _staffed_mask(clock: _Clock, kind: str) -> np.ndarray:
    """Minutes when the owning team's desk is open (staff only respond then)."""
    hours = config.SUPPORT_TEAMS[OWNER[kind]]["hours"]
    mask = np.zeros(len(clock.local_clock), dtype=bool)
    for d, (a, b) in hours.items():
        mask |= (clock.weekday == d) & (clock.local_clock >= a) & (clock.local_clock < b)
    closed = {pd.Timestamp(x).date() for x in support.closed_dates()}
    if closed:
        mask &= ~np.isin(np.array(clock.local_date, dtype=object), list(closed))
    return mask


def _next_staffed(staffed: np.ndarray) -> np.ndarray:
    """For each minute, index of the next staffed minute (>= itself)."""
    n = len(staffed)
    nxt = np.full(n, n, dtype=np.int64)
    idx = np.where(staffed)[0]
    pos = np.searchsorted(idx, np.arange(n))
    ok = pos < len(idx)
    nxt[ok] = idx[pos[ok]]
    return nxt


class _Station:
    def __init__(self, rng, row, kind, init_levels, staffed, next_staffed):
        self.rng = rng
        self.kind = kind
        self.staffed = staffed
        self.next_staffed = next_staffed
        self.lp = "(LP)" in row["description"] or "low profile" in row["description"].lower()
        self.color_share = 0.35 if "(color)" in row["description"].lower() else rng.uniform(0.08, 0.18)
        self.tray_cap = 250 if self.lp else 550
        self.n_trays = 1 if self.lp else 2
        self.trays = [rng.integers(50, self.tray_cap) for _ in range(self.n_trays)]
        self.level = {c: float(init_levels.get(c) if init_levels.get(c) is not None
                               else rng.uniform(30, 100)) + rng.uniform(0, 0.99)
                      for c in config.COMPONENTS}
        self.cmy_drum_skew = {c: rng.uniform(0.97, 1.03) for c in ("drum_c", "drum_m", "drum_y")}
        self.coverage = {c: rng.uniform(0.7, 1.3) for c in ("toner_c", "toner_m", "toner_y")}
        self.faults: dict[str, int] = {}          # code -> minute it resolves
        self.texts: dict[str, int] = {}           # transient printer-text -> resolve minute
        self.pending_swaps: dict[str, int] = {}   # component -> swap minute
        self.yellow_until = -1
        self.round_minutes: set[int] = set()
        self.friday: np.ndarray = np.zeros(0, dtype=bool)
        self.u: np.ndarray = np.zeros((0, 4))     # pre-drawn uniforms: net, fatal, jitter, early swap

    # -- helpers --------------------------------------------------------------
    def dispatch(self, t: int, median_min: float, sigma: float = 0.7) -> int:
        start = self.next_staffed[min(t, len(self.next_staffed) - 1)]
        return int(start + max(3, self.rng.lognormal(np.log(median_min), sigma)))

    def refill(self, t: int):
        self.trays = [self.tray_cap] * self.n_trays
        self.faults.pop("paper_out_error", None)
        if self.rng.random() < 0.015:
            self.faults["tray_missing"] = self.dispatch(t + 1, 40)
            self.texts["Tray1 missing"] = self.faults["tray_missing"]
        elif self.rng.random() < 0.04:
            self.yellow_until = self.dispatch(t + 1, 240)

    def swap(self, comp: str):
        # Drums are separate parts on the live page (e.g. 97 / 32 / 92), so each swaps alone.
        self.level[comp] = 100.0 if comp.startswith("toner") else self.rng.uniform(99, 100.99)

    # -- one minute -------------------------------------------------------------
    def step(self, t: int, pages: int):
        rng = self.rng
        # Resolve faults whose time has come
        for code, until in list(self.faults.items()):
            if t >= until:
                del self.faults[code]
                if code == "tray_missing":
                    self.texts.pop("Tray1 missing", None)
        for text, until in list(self.texts.items()):
            if t >= until:
                del self.texts[text]
        for comp, when in list(self.pending_swaps.items()):
            if t >= when:
                del self.pending_swaps[comp]
                self.swap(comp)
                self.faults.pop("toner_critical", None)
        if t in self.round_minutes:
            if self.yellow_until > t:      # a visit fixes a mis-set tray dial...
                self.yellow_until = t
            self.refill(t)                 # ...and may introduce a new one

        down = any(c in self.faults for c in ("printer_down", "paper_out_error", "tray_missing",
                                               "not_reachable", "fatal_error", "toner_critical"))
        if pages and not down:
            color_pages = rng.binomial(pages, self.color_share)
            for comp in config.COMPONENTS:
                if comp in ("toner_c", "toner_m", "toner_y"):
                    used = color_pages * self.coverage[comp]
                elif comp in ("drum_c", "drum_m", "drum_y"):
                    used = pages * self.cmy_drum_skew[comp]
                else:
                    used = pages
                self.level[comp] = max(0.0, self.level[comp] - used / PAGES_PER_PT[comp])
            # Paper: Tray1 first, then Tray2
            remaining = pages
            for i in range(self.n_trays):
                take = min(self.trays[i], remaining)
                self.trays[i] -= take
                remaining -= take
            if sum(self.trays) == 0:
                self.faults["paper_out_error"] = self.dispatch(t, 45)
                # Dispatch refills; resolution handled by refill
                self.round_minutes.add(self.faults["paper_out_error"])
                self.faults["paper_out_error"] += 10_000_000  # resolved only by refill
            # Jams
            if rng.random() < 1 - (1 - 1 / 2500) ** pages:
                self.faults["printer_down"] = self.dispatch(t, 55)
                self.texts[rng.choice(["Paper Feed Jam", "Paper Feed JamPaper Jam for Duplex Unit",
                                       "Paper Jam for Duplex Unit"])] = self.faults["printer_down"]

            self._check_thresholds(t)

        # Random hardware / network faults (per-minute hazards, pre-drawn)
        u_net, u_fatal = self.u[t, 0], self.u[t, 1]
        if u_net < 1 / (30 * 1440):
            self.faults["not_reachable"] = (t + int(rng.lognormal(np.log(20), 0.5))
                                            if rng.random() < 0.7 else self.dispatch(t, 60))
        if u_fatal < 1 / (50 * 1440):
            self.faults["fatal_error"] = self.dispatch(t, 80)

    def _check_thresholds(self, t: int):
        """Schedule swaps when a part crosses its replacement point (sometimes early)."""
        for comp in config.COMPONENTS:
            lvl = self.level[comp]
            is_toner = comp.startswith("toner")
            if is_toner and lvl <= 0 and "toner_critical" not in self.faults:
                self.faults["toner_critical"] = 10_000_000      # stockout until the swap
            if comp in self.pending_swaps:
                continue
            if comp == "belt":
                threshold = (config.BELT_CHANGE_PCT_FRIDAY if self.friday[t]
                             else config.BELT_CHANGE_PCT_WEEKDAY)
            else:
                threshold = REPLACE_AT["toner" if is_toner else ("drum" if comp.startswith("drum") else comp)]
            if is_toner and lvl <= 10 and self.u[t, 3] < 0.0003:
                self.pending_swaps[comp] = self.dispatch(t, 120)          # early swap -> stranded toner
            elif lvl <= threshold:
                self.pending_swaps[comp] = self.dispatch(t, 180 if is_toner else 600, 0.8)

    def snapshot(self, t: int) -> tuple[str, str, str, list[int]]:
        codes = sorted(c for c in self.faults)
        if any(c in codes for c in ("paper_out_error", "tray_missing", "printer_down",
                                    "not_reachable", "fatal_error", "toner_critical")):
            status = "red"
        elif self.yellow_until > t:
            status, codes = "yellow", codes + ["incorrect_tray_size"]
        else:
            status = "green"
        texts = [f"Paper Out Warning for Tray{i + 1}" for i, n in enumerate(self.trays) if n == 0]
        texts += list(self.texts)
        if self.level["drum_k"] <= 5:
            texts.append("Drum Life Warning for Black")
        jitter = 1 if self.u[t, 2] < 0.003 else 0
        levels = [min(100, int(self.level[c]) + jitter) for c in config.COMPONENTS]
        return status, ",".join(codes), "".join(texts), levels


def _plan_rounds(n: int, clock: _Clock, kind: str, rng) -> set[int]:
    """Routine refill rounds during desk hours: residence halls twice a day, labs once."""
    rounds = set()
    closed_dates = support.closed_dates()
    days = sorted(set(clock.local_date))
    day_start = {}
    for i, d in enumerate(clock.local_date):
        day_start.setdefault(d, i)
    for d in days:
        base = day_start[d] - int(clock.local_hour[day_start[d]] * 60)
        desk = config.SUPPORT_TEAMS[OWNER[kind]]["hours"].get(d.weekday())
        if not desk or d.strftime("%Y-%m-%d") in closed_dates:
            continue    # rounds only happen while the owning desk is staffed
        hours = [desk[0] + 1.0, desk[1] - 1.0] if kind == "residence" else [desk[0] + 0.5]
        for h in hours:
            if rng.random() < 0.08:   # missed round
                continue
            m = base + int((h + rng.normal(0, 1.0)) * 60)
            if 0 <= m < n:
                rounds.add(m)
    return rounds


def generate(data_dir: Path, days: int = 90, end: datetime | None = None, seed: int = 7,
             progress=print) -> dict:
    global _CAMPUS
    _CAMPUS = campus.load()
    rng = np.random.default_rng(seed)
    end = (end or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    minutes = pd.date_range(start, end, freq="1min", inclusive="left", tz="UTC")
    n = len(minutes)
    clock = _clock(minutes)

    _, sample = parse_status_page(FIXTURE.read_text())
    sample = {r["station_id"]: r for r in sample}
    stations = load_stations()

    # Scraper reliability: random single failures + two multi-hour outages.
    scrape_ok = rng.random(n) > 0.004
    for _ in range(2):
        s = rng.integers(0, max(1, n - 300))
        scrape_ok[s:s + int(rng.uniform(60, 240))] = False
    jitter_s = rng.integers(1, 5, n)
    scrape_ts = minutes + pd.to_timedelta(jitter_s, unit="s")

    season_res = np.array([academic_factor(d, "residence") for d in sorted(set(clock.local_date))])
    date_index = {d: i for i, d in enumerate(sorted(set(clock.local_date)))}
    didx = np.array([date_index[d] for d in clock.local_date])
    season_lab = np.array([academic_factor(d, "lab") for d in sorted(set(clock.local_date))])

    friday = np.array([d.weekday() == 4 for d in clock.local_date])
    staffed = {k: _staffed_mask(clock, k) for k in OWNER}
    next_staffed = {k: _next_staffed(v) for k, v in staffed.items()}

    frames = []
    ok_idx = np.where(scrape_ok)[0]
    for _, ref in stations.iterrows():
        sid = ref["station_id"]
        smp = sample.get(sid, {"description": ref["building"], "section": "Unknown"})
        kind = {"residence": "residence", "lab": "lab", "satellite": "satellite"}.get(ref["station_type"], "lab")
        base = {"residence": rng.lognormal(np.log(230), 0.35), "lab": rng.lognormal(np.log(380), 0.35),
                "satellite": 60.0}[kind]
        if "(LP)" in smp["description"] or "low profile" in smp["description"]:
            base *= 0.6
        hourly = HOURLY[kind] / HOURLY[kind].sum()
        seasonal = (season_res if kind == "residence" else season_lab)[didx]
        weekday = np.where(clock.weekend, WEEKEND[kind], 1.0)
        rate = base * seasonal * weekday * hourly[clock.local_hour] / 60.0
        pages = rng.poisson(rate)

        st = _Station(rng, smp, kind, {c: smp.get(c) for c in config.COMPONENTS},
                      staffed[kind], next_staffed[kind])
        st.round_minutes = _plan_rounds(n, clock, kind, rng)
        st.friday = friday
        st.u = rng.random((n, 4))

        status = np.empty(n, dtype=object)
        codes = np.empty(n, dtype=object)
        texts = np.empty(n, dtype=object)
        levels = np.zeros((n, len(config.COMPONENTS)), dtype=np.int16)
        for t in range(n):
            st.step(t, int(pages[t]))
            if scrape_ok[t]:
                status[t], codes[t], texts[t], levels[t] = st.snapshot(t)

        frame = pd.DataFrame(levels[ok_idx], columns=config.COMPONENTS)
        frame.insert(0, "scrape_ts", scrape_ts[ok_idx])
        frame.insert(1, "page_ts", minutes[ok_idx])
        frame.insert(2, "section", smp["section"])
        frame.insert(3, "station_id", sid)
        frame.insert(4, "description", smp["description"])
        frame.insert(5, "row_status", status[ok_idx])
        frame.insert(6, "status_codes", [",".join("Alert_" + c for c in s.split(",") if c)
                                         for s in codes[ok_idx]])
        frame.insert(7, "printer_text", texts[ok_idx])
        frames.append(frame)
        progress(f"  simulated {sid} {smp['description']}")

    snap = pd.concat(frames, ignore_index=True)
    # Store exactly as the live pipeline would: normalized codes, split printer text.
    from . import rules
    snap["status_codes"] = snap["status_codes"].map(
        lambda s: ",".join(rules.normalize_code(c) for c in s.split(",") if c))
    uniq = {t: " | ".join(rules.split_printer_text(t)) for t in snap["printer_text"].unique()}
    snap["printer_text"] = snap["printer_text"].map(uniq)
    for c in config.COMPONENTS:
        snap[c] = snap[c].astype("Float32")

    errors = rng.choice(["ConnectionError: read timed out", "HTTPError: 503 Service Unavailable",
                         "ConnectionError: Max retries exceeded"], size=n)
    log = pd.DataFrame({
        "attempt_ts": scrape_ts,
        "ok": scrape_ok,
        "http_status": np.where(scrape_ok, 200, np.where(np.char.startswith(errors.astype(str), "HTTP"), 503, 0)),
        "duration_ms": rng.integers(180, 900, n),
        "n_stations": np.where(scrape_ok, len(stations), 0),
        "error": np.where(scrape_ok, "", errors),
    })

    if data_dir.exists():
        import shutil
        shutil.rmtree(data_dir / "derived", ignore_errors=True)   # rollups of the old data set
        for sub in ("snapshots", "scrape_log"):
            for p in (data_dir / sub).glob("*") if (data_dir / sub).exists() else []:
                p.unlink()
    days_utc = snap["scrape_ts"].dt.strftime("%Y-%m-%d")
    log_days = log["attempt_ts"].dt.strftime("%Y-%m-%d")
    for day in sorted(log_days.unique()):
        store.write_day(data_dir, day,
                        snap.loc[days_utc == day, store.SNAPSHOT_COLUMNS].reset_index(drop=True),
                        log.loc[log_days == day].reset_index(drop=True))
    meta = {"synthetic": True, "generated_at": datetime.now(timezone.utc).isoformat(),
            "start": start.isoformat(), "end": end.isoformat(), "days": days, "seed": seed,
            "rows": len(snap)}
    store.write_meta(data_dir, meta)
    return meta
