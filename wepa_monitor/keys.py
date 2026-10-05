"""Who holds a Wepa kiosk key: staff, student workers and managers, the regular key and the master key.

The log is records/keys.jsonl, hash-chained like the inventory ledger (see ledger.py), so nobody can quietly
remove a lost key from the history. Entries:
  holder.add / holder.update   name, BSU email, position, resident student or not, residence hall
  key.issue                    a key handed to a holder: type (regular or master), key number, date
  key.return                   the key came back
  key.lost                     the key is lost. This opens a Kiosk key (KEY) investigation: a lost key is an
                               incident and the holder has a counseling conversation with management
  key.found                    a lost key turned up again (the loss stays on the record)

This is personal information about staff and student workers. Only signed-in staff and the administrator
see it; it is never given to the AI model and never included in exports.
"""
from __future__ import annotations

import re

import pandas as pd

from .ledger import Ledger

KEY_TYPES = {"regular": "Regular key", "master": "Master key"}
POSITIONS = ("Student worker (RSR)", "Student worker", "Staff", "Manager", "Other")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@([A-Za-z0-9-]+\.)*bridgew\.edu$")


class KeyLogError(ValueError):
    pass


def ledger(data_dir) -> Ledger:
    return Ledger(data_dir, "keys")


def _clean(text, n: int = 80) -> str:
    return re.sub(r"[\x00-\x1f<>]", "", str(text or "")).strip()[:n]


def _date(value, field: str) -> str:
    if not value:
        raise KeyLogError(f"Enter the {field}.")
    try:
        d = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise KeyLogError(f"The {field} isn't a date.") from exc
    if d.tz is not None:
        d = d.tz_convert(None)
    if d > pd.Timestamp.now() + pd.Timedelta(days=1):
        raise KeyLogError(f"The {field} is in the future.")
    return d.date().isoformat()


def _actor(user: dict) -> dict:
    if not user or not user.get("username"):
        raise KeyLogError("Sign in to change the key log.")
    return user


def state(data_dir) -> dict:
    """{"holders": {id: {...}}, "keys": {id: {...}}} as of the end of the log."""
    holders: dict[str, dict] = {}
    keys: dict[str, dict] = {}
    for e in ledger(data_dir).read():
        a, d = e["action"], e["data"]
        if a == "holder.add":
            holders[d["id"]] = {**d, "added_by": e["user_name"], "added_at": e["ts"]}
        elif a == "holder.update" and d["id"] in holders:
            holders[d["id"]].update({k: v for k, v in d.items() if k != "id"})
        elif a == "key.issue":
            keys[d["id"]] = {**d, "status": "held", "returned": None, "lost": None, "found": None,
                             "incident": "", "issued_by": e["user_name"]}
        elif a == "key.return" and d["id"] in keys:
            keys[d["id"]].update(status="returned", returned=d["date"])
        elif a == "key.lost" and d["id"] in keys:
            keys[d["id"]].update(status="lost", lost=d["date"], incident=d.get("incident", ""), lost_note=e["note"])
        elif a == "key.found" and d["id"] in keys:
            keys[d["id"]].update(status="found after loss", found=d["date"])
    return {"holders": holders, "keys": keys}


def table(st: dict) -> pd.DataFrame:
    """One row per key handed out, newest first, with the holder's details."""
    rows = []
    for k in st["keys"].values():
        h = st["holders"].get(k["holder"], {})
        lost = sum(1 for x in st["keys"].values() if x["holder"] == k["holder"] and x["lost"])
        rows.append({"id": k["id"], "name": h.get("name", ""), "email": h.get("email", ""),
                     "position": h.get("position", ""), "resident": h.get("resident", False),
                     "hall": h.get("hall", ""), "key_type": KEY_TYPES.get(k["key_type"], k["key_type"]),
                     "key_no": k.get("key_no", ""), "issued": k["issued"], "returned": k["returned"],
                     "status": k["status"], "lost_date": k["lost"], "keys_lost": lost, "incident": k["incident"]})
    cols = ["id", "name", "email", "position", "resident", "hall", "key_type", "key_no", "issued", "returned",
            "status", "lost_date", "keys_lost", "incident"]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values(["issued", "id"], ascending=False).reset_index(drop=True) if len(df) else df


def add_holder(data_dir, user: dict, name: str, email: str, position: str, resident: bool = False,
               hall: str = "") -> str:
    name, email = _clean(name), _clean(email, 120).lower()
    if not name:
        raise KeyLogError("Enter the person's name.")
    if not EMAIL_RE.match(email):
        raise KeyLogError("Enter a BSU email address (…@bridgew.edu or …@student.bridgew.edu).")
    if position not in POSITIONS:
        raise KeyLogError("Pick a position.")
    st = state(data_dir)
    if any(h["email"] == email for h in st["holders"].values()):
        raise KeyLogError("That person is already in the log; issue them a key instead.")
    hid = f"H{len(st['holders']) + 1:04d}"
    ledger(data_dir).append(_actor(user), "holder.add", {"id": hid, "name": name, "email": email,
                                                        "position": position, "resident": bool(resident),
                                                        "hall": _clean(hall) if resident else ""})
    return hid


def update_holder(data_dir, user: dict, hid: str, **changes) -> None:
    st = state(data_dir)
    if hid not in st["holders"]:
        raise KeyLogError("Unknown person.")
    data = {"id": hid}
    if "name" in changes and _clean(changes["name"]):
        data["name"] = _clean(changes["name"])
    if "position" in changes:
        if changes["position"] not in POSITIONS:
            raise KeyLogError("Pick a position.")
        data["position"] = changes["position"]
    if "resident" in changes:
        data["resident"] = bool(changes["resident"])
        data["hall"] = _clean(changes.get("hall", "")) if data["resident"] else ""
    ledger(data_dir).append(_actor(user), "holder.update", data)


def issue(data_dir, user: dict, hid: str, key_type: str, key_no: str, date) -> str:
    st = state(data_dir)
    if hid not in st["holders"]:
        raise KeyLogError("Add the person first.")
    if key_type not in KEY_TYPES:
        raise KeyLogError("Pick the key type.")
    key_no = _clean(key_no, 30)
    if key_no and any(k["key_no"] == key_no and k["key_type"] == key_type and k["status"] == "held"
                      for k in st["keys"].values()):
        raise KeyLogError(f"{KEY_TYPES[key_type]} {key_no} is already out with someone. Record its return first.")
    kid = f"K{len(st['keys']) + 1:05d}"
    ledger(data_dir).append(_actor(user), "key.issue", {"id": kid, "holder": hid, "key_type": key_type,
                                                       "key_no": key_no, "issued": _date(date, "date issued")})
    return kid


def _held(st: dict, kid: str) -> dict:
    k = st["keys"].get(kid)
    if not k or k["status"] != "held":
        raise KeyLogError("That key isn't currently out.")
    return k


def return_key(data_dir, user: dict, kid: str, date, note: str = "") -> None:
    k = _held(state(data_dir), kid)
    d = _date(date, "return date")
    if d < k["issued"]:
        raise KeyLogError("The return date is before the key was issued.")
    ledger(data_dir).append(_actor(user), "key.return", {"id": kid, "date": d}, note)


def lose_key(data_dir, user: dict, kid: str, date, note: str) -> str:
    """Record a lost key and open a Kiosk key investigation. Returns its reference."""
    from . import investigations as I
    st = state(data_dir)
    k = _held(st, kid)
    d = _date(date, "date it was lost")
    if not (note or "").strip():
        raise KeyLogError("Say what happened.")
    h = st["holders"][k["holder"]]
    prior = sum(1 for x in st["keys"].values() if x["holder"] == k["holder"] and x["lost"])
    kind = KEY_TYPES[k["key_type"]].lower()
    desc = (f"{h['name']} ({h['position']}) lost a Wepa kiosk {kind}"
            + (f", number {k['key_no']}" if k.get("key_no") else "") + f", on or about {d}."
            + (f" This is their {prior + 1}{'nd' if prior == 1 else 'rd' if prior == 2 else 'th'} lost key."
               if prior else "")
            + f"\n\nWhat happened: {note.strip()}\n\n"
            "Required: a counseling conversation with management, recorded here (who, when, outcome). "
            + ("A master key opens every kiosk: decide with management and Wepa whether the locks must be "
               "changed. " if k["key_type"] == "master" else "Decide whether the kiosk locks need rekeying. ")
            + "Close this investigation once the key is recovered or replaced and the conversation is held.")
    actor = {"username": "system", "name": f"Key log ({user.get('username', 'unknown')})", "can_edit": True}
    ref = I.create(data_dir, actor, "", f"Lost Wepa {kind}: {h['name']}", desc, category="KEY", origin="auto",
                   location="Wepa kiosk keys", detector="key log",
                   first_occurrence=pd.Timestamp(d).tz_localize("UTC").isoformat())
    ledger(data_dir).append(_actor(user), "key.lost", {"id": kid, "date": d, "incident": ref}, note)
    return ref


def found_key(data_dir, user: dict, kid: str, date, note: str = "") -> None:
    st = state(data_dir)
    k = st["keys"].get(kid)
    if not k or k["status"] != "lost":
        raise KeyLogError("That key isn't recorded as lost.")
    ledger(data_dir).append(_actor(user), "key.found", {"id": kid, "date": _date(date, "date it was found")}, note)


def counts(st: dict) -> dict:
    keys = st["keys"].values()
    return {"out": sum(k["status"] == "held" for k in keys),
            "master_out": sum(k["status"] == "held" and k["key_type"] == "master" for k in keys),
            "lost": sum(k["status"] == "lost" for k in keys),
            "holders": len(st["holders"])}

