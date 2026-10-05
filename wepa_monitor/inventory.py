"""Consumable and paper inventory: what's on hand, where, and the trail of every unit.

Items
  The ten consumables the monitor tracks (toner and drums in K/C/M/Y, belt, fuser), counted in units, and
  paper, counted in reams to two decimals (a refill seldom uses a whole ream).

Locations (the administrator defines all but the kiosks; closets move, so they're editable)
  central      Central storage. One always exists ("Central storage", where to be recorded) so stock has a
               home before anyone has found the room; the administrator fills in where it is.
  closet       a building's telecom closet
  paper        a residence hall's paper closet
  kiosk        under a print kiosk: one per station, automatic

Where things may be kept: parts (toner, drums, belts, fusers) only ever in central storage; paper anywhere
(central storage, a telecom or paper closet, under a kiosk).

The ledger (records/inventory.jsonl, hash-chained: see ledger.py). Every change is an entry; stock is the
replay of the entries, so every unit's trail is there to read:
  receipt.draft / receipt.approve / receipt.reject
                  new stock from an invoice, receipt or typed counts. Nothing is added until a second person
                  approves (reading invoices, and typing numbers, both make mistakes).
  move            from one location to another (Central storage -> a hall's closet -> under a kiosk)
  install         a part went into a printer: written automatically when the monitor sees a replacement
  refill          paper went into a printer's tray: written automatically when an empty tray is refilled,
                  as an estimate (one tray's worth, in reams); the weekly count corrects it
  count           a physical count. Stock at that location is set to what was counted; a shortfall opens an
                  Inventory investigation (damaged, stolen or never recorded), a surplus is noted
  adjust          a recorded write-off (damaged, stolen, returned to vendor) or correction, with a reason
  location.*      a location added, changed or retired
  kiosk.config    a kiosk's tray size (reams one refill uses)
  start           when tracking began: automatic deductions only count events after it

Where an automatic deduction comes from: a part, always central storage. Paper: the kiosk's own stock if it
has any, else a closet in the same building, else central storage. If none had it, the deduction still
happens (it came from somewhere), the balance goes negative, and the location is flagged for a count.
"""
from __future__ import annotations

import re

import pandas as pd

from . import config, refs
from .ledger import Ledger

PAPER = "paper"
ITEMS = {**config.COMPONENT_LABELS, PAPER: "Paper"}
PAPER_SHEETS_PER_REAM = 500
DEFAULT_TRAY_REAMS = 1.10            # a 550-sheet tray, the usual size on the kiosks' printers
PAPER_TOLERANCE = 0.5                # reams: refills are estimates, so small paper differences aren't incidents
LOCATION_KINDS = {"central": "Central storage", "closet": "Telecom closet", "paper": "Paper closet",
                  "kiosk": "Under the kiosk"}
ADJUST_REASONS = ("damaged", "stolen", "returned to vendor", "found", "correction")
PAPER_CHECK_DAYS = 7                 # paper is counted at least weekly; Friday is refill day
PAPER_CHECK_WEEKDAY = 4              # Friday


DEFAULT_CENTRAL = "central:main"
PART_KINDS = ("central",)            # parts are only ever kept in central storage


class InventoryError(ValueError):
    pass


def allowed(item: str, kind: str) -> bool:
    """Parts are kept only in central storage; paper anywhere."""
    return item == PAPER or kind in PART_KINDS


def _allowed_at(st: dict, item: str, loc: str) -> None:
    L = st["locations"].get(loc)
    if L and not allowed(item, L["kind"]):
        raise InventoryError(f"{ITEMS[item]} is kept only in central storage, not at {L['name']}.")


def ledger(data_dir) -> Ledger:
    return Ledger(data_dir, "inventory")


def qty(value, item: str) -> float:
    """Units are whole numbers; paper is reams to two decimals."""
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise InventoryError("Quantities must be numbers.") from exc
    if v != v or abs(v) > 100000:
        raise InventoryError("That quantity isn't plausible.")
    if item == PAPER:
        return round(v, 2)
    if abs(v - round(v)) > 1e-9:
        raise InventoryError(f"{ITEMS[item]} is counted in whole units.")
    return float(round(v))


def _item(item: str) -> str:
    if item not in ITEMS:
        raise InventoryError("Unknown item.")
    return item


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "location"


# --- state: replay the ledger -----------------------------------------------------------------------------

def state(data_dir, stations: pd.DataFrame | None = None) -> dict:
    """Locations, stock, receipts and settings, as of the end of the ledger."""
    locs: dict[str, dict] = {}
    stock: dict[tuple[str, str], float] = {}
    receipts: dict[str, dict] = {}
    trays: dict[str, float] = {}
    counted: dict[tuple[str, str], float] = {}
    seen_auto: set[str] = set()
    started = None
    locs[DEFAULT_CENTRAL] = {"id": DEFAULT_CENTRAL, "kind": "central", "name": "Central storage (room not recorded yet)",
                             "building": "", "notes": "", "active": True}
    if stations is not None:
        for r in stations.itertuples():
            locs[f"kiosk:{r.station_id}"] = {"id": f"kiosk:{r.station_id}", "kind": "kiosk", "name": f"Under {r.label}",
                                            "building": r.building, "station_id": r.station_id, "active": True}

    def add(loc, item, q):
        stock[(loc, item)] = round(stock.get((loc, item), 0.0) + q, 2)

    for e in ledger(data_dir).read():
        a, d = e["action"], e["data"]
        if a == "start":
            started = started or e["ts"]
        elif a == "location.add":
            locs[d["id"]] = {**d, "active": True}
        elif a == "location.update" and d["id"] in locs:
            locs[d["id"]].update({k: v for k, v in d.items() if k != "id"})
        elif a == "location.retire" and d["id"] in locs:
            locs[d["id"]]["active"] = False
        elif a == "kiosk.config":
            trays[d["station_id"]] = float(d["tray_reams"])
        elif a == "receipt.draft":
            receipts[d["receipt"]] = {**d, "status": "awaiting approval", "drafted_by": e["user"],
                                      "drafted_by_name": e["user_name"], "drafted_at": e["ts"]}
        elif a == "receipt.approve" and d["receipt"] in receipts:
            rc = receipts[d["receipt"]]
            rc.update(status="approved", approved_by=e["user"], approved_by_name=e["user_name"], approved_at=e["ts"])
            for ln in rc["lines"]:
                add(ln["to"], ln["item"], ln["qty"])
        elif a == "receipt.reject" and d["receipt"] in receipts:
            receipts[d["receipt"]].update(status="rejected", rejected_by=e["user"], reason=e["note"])
        elif a == "move":
            add(d["from"], d["item"], -d["qty"])
            add(d["to"], d["item"], d["qty"])
        elif a in ("install", "refill"):
            add(d["from"], d["item"], -d["qty"])
            if d.get("key"):
                seen_auto.add(d["key"])
        elif a == "count":
            for item, c in d["counts"].items():
                stock[(d["location"], item)] = float(c)
                counted[(d["location"], item)] = e["ts"]
        elif a == "adjust":
            add(d["location"], d["item"], d["qty"])
    return {"locations": locs, "stock": stock, "receipts": receipts, "trays": trays, "started": started,
            "seen_auto": seen_auto, "counted": counted}


def balances(st: dict, active_only: bool = True) -> pd.DataFrame:
    rows = []
    for (loc, item), q in st["stock"].items():
        L = st["locations"].get(loc, {"name": loc, "kind": "?", "building": "", "active": False})
        if active_only and not L.get("active", True) and abs(q) < 1e-9:
            continue
        rows.append({"location": loc, "location_name": L["name"], "kind": L["kind"], "building": L.get("building", ""),
                     "item": item, "item_label": ITEMS.get(item, item), "qty": q})
    cols = ["location", "location_name", "kind", "building", "item", "item_label", "qty"]
    return pd.DataFrame(rows, columns=cols)


def totals(st: dict) -> pd.DataFrame:
    """On hand per item, split by level: central, closets, kiosks."""
    b = balances(st)
    out = []
    for item, label in ITEMS.items():
        g = b[b["item"] == item]
        lv = {k: float(g[g["kind"] == k]["qty"].sum()) for k in LOCATION_KINDS}
        out.append({"item": item, "item_label": label, "central": lv["central"], "closets": lv["closet"] + lv["paper"],
                    "kiosks": lv["kiosk"], "total": round(sum(lv.values()), 2),
                    "negative": int((g["qty"] < -1e-9).sum())})
    return pd.DataFrame(out)


def trail(data_dir, item: str | None = None, location: str | None = None, limit: int = 300) -> pd.DataFrame:
    """Every movement, newest first, optionally for one item or one location."""
    rows = []
    drafts: dict[str, dict] = {}
    for e in ledger(data_dir).read():
        a, d = e["action"], e["data"]
        if a == "receipt.draft":
            drafts[d["receipt"]] = d
            continue
        if a in ("move", "install", "refill", "adjust"):
            frm, to = d.get("from", ""), d.get("to", "")
            if a == "adjust":
                frm, to = (d["location"], "") if d["qty"] < 0 else ("", d["location"])
            entries = [(d["item"], abs(d["qty"]), frm, to, d.get("station_id", ""))]
        elif a == "receipt.approve" and d["receipt"] in drafts:
            r = drafts[d["receipt"]]
            src = "delivery" + (f" from {r['vendor']}" if r.get("vendor") else "") + f" ({d['receipt']})"
            entries = [(ln["item"], ln["qty"], src, ln["to"], "") for ln in r["lines"]]
        elif a == "count":
            entries = [(i, c, "", d["location"], "") for i, c in d["counts"].items()]
        else:
            continue
        for it, q, frm, to, sid in entries:
            if item and it != item:
                continue
            if location and location not in (frm, to):
                continue
            rows.append({"ts": e["ts"], "action": a, "item": it, "qty": q, "from": frm, "to": to,
                         "station_id": sid, "by": e["user_name"], "note": e.get("note", ""),
                         "reason": d.get("reason", ""), "variance": (d.get("variance") or {}).get(it)})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["ts", "action", "item", "qty", "from", "to", "station_id", "by", "note", "reason",
                                     "variance"])
    return df.sort_values("ts", ascending=False).head(limit).reset_index(drop=True)


# --- actions --------------------------------------------------------------------------------------------

def _actor(user: dict) -> dict:
    if not user or not user.get("username"):
        raise InventoryError("Sign in to change inventory.")
    return user


def start(data_dir, user: dict) -> None:
    if state(data_dir)["started"] is None:
        ledger(data_dir).append(_actor(user), "start", {}, "Inventory tracking started.")


def add_location(data_dir, user: dict, kind: str, name: str, building: str = "", notes: str = "") -> str:
    if kind not in ("central", "closet", "paper"):
        raise InventoryError("Kiosk locations are created automatically; pick central, closet or paper.")
    name = re.sub(r"[\x00-\x1f<>]", "", name or "").strip()[:80]
    if not name:
        raise InventoryError("A location needs a name.")
    st = state(data_dir)
    loc_id = f"{kind}:{_slug(name)}"
    n = 2
    while loc_id in st["locations"]:
        loc_id, n = f"{kind}:{_slug(name)}-{n}", n + 1
    start(data_dir, user)
    ledger(data_dir).append(_actor(user), "location.add", {"id": loc_id, "kind": kind, "name": name,
                                                          "building": building.strip()[:80], "notes": notes.strip()[:300]})
    return loc_id


def update_location(data_dir, user: dict, loc_id: str, name: str | None = None, building: str | None = None,
                    notes: str | None = None) -> None:
    st = state(data_dir)
    if loc_id not in st["locations"] or loc_id.startswith("kiosk:"):
        raise InventoryError("That location can't be changed.")
    data = {"id": loc_id}
    if name is not None:
        data["name"] = re.sub(r"[\x00-\x1f<>]", "", name).strip()[:80] or st["locations"][loc_id]["name"]
    if building is not None:
        data["building"] = building.strip()[:80]
    if notes is not None:
        data["notes"] = notes.strip()[:300]
    ledger(data_dir).append(_actor(user), "location.update", data)


def retire_location(data_dir, user: dict, loc_id: str) -> None:
    st = state(data_dir)
    if loc_id not in st["locations"] or loc_id.startswith("kiosk:"):
        raise InventoryError("That location can't be retired.")
    left = {i: q for (loc, i), q in st["stock"].items() if loc == loc_id and abs(q) > 1e-9}
    if left:
        raise InventoryError("Move or write off what's stored there first.")
    if st["locations"][loc_id]["kind"] == "central" and sum(
            L["kind"] == "central" and L.get("active", True) for L in st["locations"].values()) < 2:
        raise InventoryError("Parts need a central storage location; add or edit one instead of retiring the last.")
    ledger(data_dir).append(_actor(user), "location.retire", {"id": loc_id})


def _known(st: dict, loc: str) -> None:
    if loc not in st["locations"] or not st["locations"][loc].get("active", True):
        raise InventoryError("Pick a current location.")


def move(data_dir, user: dict, item: str, amount, frm: str, to: str, note: str = "",
         stations: pd.DataFrame | None = None) -> dict:
    item = _item(item)
    q = qty(amount, item)
    if q <= 0:
        raise InventoryError("Move a positive amount.")
    if frm == to:
        raise InventoryError("Pick two different locations.")
    st = state(data_dir, stations)
    _known(st, frm)
    _known(st, to)
    _allowed_at(st, item, frm)
    _allowed_at(st, item, to)
    have = st["stock"].get((frm, item), 0.0)
    if q > have + 1e-9:
        raise InventoryError(f"{st['locations'][frm]['name']} has {fmt_qty(have, item)} on record. Count it first if "
                             "that's wrong.")
    start(data_dir, user)
    return ledger(data_dir).append(_actor(user), "move", {"item": item, "qty": q, "from": frm, "to": to}, note)


def adjust(data_dir, user: dict, item: str, amount, location: str, reason: str, note: str,
           stations: pd.DataFrame | None = None, incident: str = "") -> dict:
    """A write-off or correction. Damaged, stolen and returned always reduce stock; found always adds;
    a correction takes the sign as entered."""
    item = _item(item)
    q = qty(amount, item)
    if reason not in ADJUST_REASONS:
        raise InventoryError("Pick a reason.")
    if not note.strip():
        raise InventoryError("Say what happened.")
    if reason in ("damaged", "stolen", "returned to vendor"):
        q = -abs(q)
    elif reason == "found":
        q = abs(q)
    if q == 0:
        raise InventoryError("Enter how many.")
    st = state(data_dir, stations)
    _known(st, location)
    _allowed_at(st, item, location)
    start(data_dir, user)
    return ledger(data_dir).append(_actor(user), "adjust", {"item": item, "qty": q, "location": location,
                                                            "reason": reason, "incident": incident}, note)


def count(data_dir, user: dict, location: str, counts: dict, note: str = "", stations: pd.DataFrame | None = None,
          on_shortfall=None) -> dict:
    """Record a physical count at one location. Returns {"variance": {...}, "incident": ref or ""}.
    on_shortfall(location_name, shortfalls) is called to open an investigation when something is missing."""
    st = state(data_dir, stations)
    _known(st, location)
    clean, variance = {}, {}
    for item, c in counts.items():
        if c is None or c == "":
            continue
        item = _item(item)
        _allowed_at(st, item, location)
        q = qty(c, item)
        if q < 0:
            raise InventoryError("Counts can't be negative.")
        clean[item] = q
        variance[item] = round(q - st["stock"].get((location, item), 0.0), 2)
    if not clean:
        raise InventoryError("Enter at least one count.")
    short = {i: -v for i, v in variance.items() if v < -(PAPER_TOLERANCE if i == PAPER else 0.0) - 1e-9}
    ref = ""
    if short and on_shortfall is not None:
        ref = on_shortfall(st["locations"][location]["name"], short) or ""
    start(data_dir, user)
    ledger(data_dir).append(_actor(user), "count", {"location": location, "counts": clean, "variance": variance,
                                                    "incident": ref}, note)
    return {"variance": variance, "incident": ref}


def set_tray(data_dir, user: dict, station_id: str, reams) -> None:
    q = qty(reams, PAPER)
    if not 0.1 <= q <= 10:
        raise InventoryError("A tray holds between 0.1 and 10 reams.")
    ledger(data_dir).append(_actor(user), "kiosk.config", {"station_id": station_id, "tray_reams": q})


# --- receipts: invoices, receipts and typed counts, approved by a second person -----------------------------

def draft_receipt(data_dir, user: dict, lines: list[dict], vendor: str = "", invoice_no: str = "",
                  file: str = "", source: str = "manual", read_by: str = "", stations: pd.DataFrame | None = None) -> str:
    st = state(data_dir, stations)
    clean = []
    for ln in lines:
        if not ln.get("item") or ln.get("qty") in (None, ""):
            continue
        item = _item(ln["item"])
        q = qty(ln["qty"], item)
        if q <= 0:
            continue
        _known(st, ln.get("to", ""))
        _allowed_at(st, item, ln["to"])
        clean.append({"item": item, "qty": q, "to": ln["to"], "text": str(ln.get("text", ""))[:200]})
    if not clean:
        raise InventoryError("Add at least one line with an item, a quantity and where it's going.")
    rid = refs.next_number(data_dir, "RCV", {"kind": "receipt"})
    start(data_dir, user)
    ledger(data_dir).append(_actor(user), "receipt.draft", {
        "receipt": rid, "lines": clean, "vendor": vendor.strip()[:80], "invoice_no": invoice_no.strip()[:60],
        "file": file, "source": source, "read_by": read_by})
    return rid


def approve_receipt(data_dir, user: dict, rid: str, sign_in_required: bool = True) -> None:
    st = state(data_dir)
    rc = st["receipts"].get(rid)
    if not rc or rc["status"] != "awaiting approval":
        raise InventoryError("That receipt isn't waiting for approval.")
    if sign_in_required and rc["drafted_by"] == _actor(user)["username"]:
        raise InventoryError("A second person approves: someone other than whoever entered it.")
    ledger(data_dir).append(_actor(user), "receipt.approve", {"receipt": rid})


def reject_receipt(data_dir, user: dict, rid: str, reason: str) -> None:
    st = state(data_dir)
    rc = st["receipts"].get(rid)
    if not rc or rc["status"] != "awaiting approval":
        raise InventoryError("That receipt isn't waiting for approval.")
    if not reason.strip():
        raise InventoryError("Say why it's rejected.")
    ledger(data_dir).append(_actor(user), "receipt.reject", {"receipt": rid}, reason)


# --- shortfalls become investigations ---------------------------------------------------------------------

def open_incident(data_dir, user: dict, location_name: str, shortfall: dict) -> str:
    """Open an Inventory (STK) investigation for a count that came up short. Missing stock needs an
    investigation and someone accountable: most often it was damaged or stolen, or used without being
    recorded."""
    from . import investigations as I
    what = ", ".join(f"{fmt_qty(q, i)} of {ITEMS[i]}" for i, q in shortfall.items())
    desc = (f"A count at {location_name} came up short: {what} fewer than the records show.\n\n"
            f"Counted by {user.get('name') or user.get('username')}.\n\n"
            "Find out where it went: damaged (write it off with the reason), stolen (report it to management and "
            "BSU Police), or used or moved without being recorded (record the move). Note who is accountable.")
    actor = {"username": "system", "name": f"Inventory count ({user.get('username', 'unknown')})", "can_edit": True}
    return I.create(data_dir, actor, "", f"Inventory shortfall: {location_name}", desc, category="STK",
                    origin="auto", location=location_name, detector="inventory count",
                    first_occurrence=pd.Timestamp.now(tz="UTC").isoformat())


# --- automatic deductions -----------------------------------------------------------------------------------

SYSTEM = {"username": "system", "name": "Automatic (from the printers)"}


def _source(st: dict, station_id: str, building: str, item: str) -> str:
    """Where a part or paper for this kiosk most likely came from. Parts: central storage only. Paper: under
    the kiosk, then the building's paper or telecom closet, then central storage."""
    centrals = [loc for loc, L in st["locations"].items() if L["kind"] == "central" and L.get("active", True)]
    if item == PAPER:
        kiosk = f"kiosk:{station_id}"
        if st["stock"].get((kiosk, item), 0.0) > 1e-9:
            return kiosk
        for kind in ("paper", "closet"):
            for loc, L in st["locations"].items():
                if L["kind"] == kind and L.get("active", True) and L.get("building") == building \
                        and st["stock"].get((loc, item), 0.0) > 1e-9:
                    return loc
    for loc in centrals:
        if st["stock"].get((loc, item), 0.0) > 1e-9:
            return loc
    if item == PAPER:
        return f"kiosk:{station_id}"
    return centrals[0] if centrals else DEFAULT_CENTRAL


def sync(ds, data_dir=None) -> int:
    """Write install and refill entries for replacements and refills seen since tracking started. Idempotent:
    each event has a key and is written once. Returns how many entries were written."""
    data_dir = data_dir or ds.data_dir
    st = state(data_dir, ds.stations)
    if st["started"] is None:
        return 0
    since = pd.Timestamp(st["started"], unit="s", tz="UTC")
    bld = ds.stations.set_index("station_id")["building"]
    led = ledger(data_dir)
    n = 0
    for r in ds.repl[ds.repl["ts"] >= since].sort_values("ts").itertuples():
        key = f"install|{r.station_id}|{r.component}|{pd.Timestamp(r.ts).isoformat()}"
        if key in st["seen_auto"] or r.component not in ITEMS:
            continue
        frm = _source(st, r.station_id, bld.get(r.station_id, ""), r.component)
        led.append(SYSTEM, "install", {"item": r.component, "qty": 1.0, "from": frm, "station_id": r.station_id,
                                       "at": pd.Timestamp(r.ts).isoformat(), "key": key})
        st["stock"][(frm, r.component)] = st["stock"].get((frm, r.component), 0.0) - 1
        st["seen_auto"].add(key)
        n += 1
    trays = ds.tray_inc
    if len(trays):
        done = trays[(trays["status"] == "resolved") & trays["end"].notna() & (trays["end"] >= since)]
        for r in done.sort_values("end").itertuples():
            key = f"refill|{r.station_id}|{r.tray}|{pd.Timestamp(r.end).isoformat()}"
            if key in st["seen_auto"]:
                continue
            reams = st["trays"].get(r.station_id, DEFAULT_TRAY_REAMS)
            frm = _source(st, r.station_id, bld.get(r.station_id, ""), PAPER)
            led.append(SYSTEM, "refill", {"item": PAPER, "qty": reams, "from": frm, "station_id": r.station_id,
                                          "tray": r.tray, "at": pd.Timestamp(r.end).isoformat(), "key": key,
                                          "estimate": True})
            st["stock"][(frm, PAPER)] = st["stock"].get((frm, PAPER), 0.0) - reams
            st["seen_auto"].add(key)
            n += 1
    return n


# --- weekly paper checks ---------------------------------------------------------------------------------

def paper_checks(st: dict, now: pd.Timestamp) -> pd.DataFrame:
    """Every location that holds paper, when it was last counted, and whether this week's count is due or
    overdue. Paper should be counted at least weekly; Friday is refill day, so the count is due by Friday."""
    rows = []
    week_start = (now.tz_convert(config.LOCAL_TZ).normalize()
                  - pd.Timedelta(days=(now.tz_convert(config.LOCAL_TZ).weekday() - PAPER_CHECK_WEEKDAY) % 7))
    for loc, L in st["locations"].items():
        if not L.get("active", True):
            continue
        has = st["stock"].get((loc, PAPER))
        if has is None and L["kind"] == "kiosk":
            continue
        ts = st["counted"].get((loc, PAPER))
        last = pd.Timestamp(ts, unit="s", tz="UTC") if ts else None
        age = (now - last).total_seconds() / 86400 if last is not None else None
        if last is None or age > PAPER_CHECK_DAYS + 3:
            status = "overdue"
        elif last.tz_convert(config.LOCAL_TZ) >= week_start:
            status = "done this week"
        else:
            status = "due Friday"
        rows.append({"location": loc, "location_name": L["name"], "kind": L["kind"], "building": L.get("building", ""),
                     "reams": has if has is not None else 0.0, "last_counted": last, "status": status})
    df = pd.DataFrame(rows, columns=["location", "location_name", "kind", "building", "reams", "last_counted", "status"])
    order = {"overdue": 0, "due Friday": 1, "done this week": 2}
    return df.assign(_o=df["status"].map(order)).sort_values(["_o", "location_name"]).drop(columns="_o") \
        .reset_index(drop=True)


def fmt_qty(q: float, item: str) -> str:
    if item == PAPER:
        return f"{q:,.2f} ream{'s' if abs(q) != 1 else ''}"
    q = int(round(q))
    return f"{q:,} unit{'s' if abs(q) != 1 else ''}"


# --- reading invoices ------------------------------------------------------------------------------------

COLORS = {"k": "k", "black": "k", "bk": "k", "c": "c", "cyan": "c", "m": "m", "magenta": "m", "y": "y", "yellow": "y"}
CASE_REAMS = 10                      # a case of copy paper is ten reams


def parse_invoice_text(text: str) -> list[dict]:
    """Best-effort lines from an invoice's text: [{"item", "qty", "text"}]. People check and fix these before
    anything is added (draft_receipt + approval)."""
    out = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        low = line.lower()
        if not low or len(low) > 240:
            continue
        item = None
        if re.search(r"\b(paper|ream|reams|copy paper|tree ?free|bagasse)\b", low):
            item = PAPER
        elif "fuser" in low:
            item = "fuser"
        elif "belt" in low:
            item = "belt"
        else:
            kind = "drum" if re.search(r"\b(drum|image drum|imaging)\b", low) else "toner" if "toner" in low else None
            if kind:
                col = re.search(r"\b(black|cyan|magenta|yellow|bk|k|c|m|y)\b", low)
                if col:
                    item = f"{kind}_{COLORS[col.group(1)]}"
        if not item:
            continue
        nums = [float(n.replace(",", "")) for n in re.findall(r"(?<![\w.$])(\d{1,4}(?:,\d{3})*)(?![\d.%A-Za-z]|\s*(?:x\s*\d|mm|ppm|v\b|w\b))",
                                                              line)]
        m = re.search(r"\b(?:qty|quantity|qnty)\b\s*[:#]?\s*(\d+)", low)
        q = float(m.group(1)) if m else (nums[0] if nums else None)
        if q is None or q <= 0 or q > 5000:
            continue
        if item == PAPER and re.search(r"\b(case|cases|cs|carton|box)\b", low):
            q *= CASE_REAMS
        out.append({"item": item, "qty": q, "text": line[:200]})
    return out


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:20])
    except Exception:  # noqa: BLE001 - a damaged PDF just means "type it in"
        return ""


AI_PROMPT = (
    "This is an invoice, packing slip or receipt for printer supplies delivered to a university. List every line "
    "for these items only: toner (black, cyan, magenta, yellow), image drums (same colors), transfer belts, fusers, "
    "and copy paper. Reply with JSON only, no prose: {\"vendor\": str, \"invoice_no\": str, \"lines\": [{\"item\": "
    "one of toner_k toner_c toner_m toner_y drum_k drum_c drum_m drum_y belt fuser paper, \"qty\": number, "
    "\"text\": the line as printed}]}. For paper, give qty in reams (a case is usually 10 reams; say so in text). "
    "Use only what is printed; if a quantity is unclear, leave that line out.")


def read_invoice(data: bytes, mime: str) -> dict:
    """{"lines", "vendor", "invoice_no", "read_by"}: the AI model reads PDFs and photos when one is configured,
    otherwise the PDF's text is parsed by rule; photos without AI are typed in by hand."""
    from . import ai
    result = {"lines": [], "vendor": "", "invoice_no": "", "read_by": ""}
    if ai.enabled() and ai.provider() == "anthropic":
        try:
            got = ai.read_document(data, mime, AI_PROMPT)
            lines = [ln for ln in got.get("lines", []) if ln.get("item") in ITEMS]
            return {"lines": lines, "vendor": str(got.get("vendor", ""))[:80],
                    "invoice_no": str(got.get("invoice_no", ""))[:60], "read_by": "AI model"}
        except Exception:  # noqa: BLE001 - fall back to the rules
            pass
    if mime == "application/pdf":
        text = pdf_text(data)
        if text.strip():
            result["lines"] = parse_invoice_text(text)
            m = re.search(r"invoice\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Z0-9-]{3,})", text, re.I)
            result["invoice_no"] = m.group(1) if m else ""
            result["read_by"] = "the PDF's text"
    return result
