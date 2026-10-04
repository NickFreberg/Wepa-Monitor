"""Investigations (INV#########): documented root-cause work on recurring or unusual printer problems.

What this is, and isn't
-----------------------
An evidence file for problems worth raising with Wepa: a printer that keeps faulting, keeps dropping
off the network, or fails far more than its neighbours. It is *not* a ticketing system and doesn't
replace BSU's ITSM tool: each investigation can carry that system's ticket number and a Wepa case
number as references, and nothing is synced either way. Paper and consumable problems are out of
scope (they're routine refills); the detector looks only at OUT (unreachable) and ERR (hardware or
system) outages.

Lifecycle and governance
------------------------
    New -> Analyze -> Respond -> Review -> Closed Complete
       \\-> Closed Cancelled   (from New or Analyze)
            Analyze/Respond -> Closed Incomplete
            Review -> Closed Complete | Closed Incomplete | back to Respond or Analyze
            Respond -> back to Analyze (the root cause didn't hold up)

* Entering Analyze needs an assignee and sets the escalation date.
* Entering Respond needs a root cause, marked suspected or confirmed.
* Entering Review needs the action taken (e.g. "Opened Wepa case …", "Fuser replaced").
* Closing, cancelling or sending back (to Respond or Analyze) needs a written reason.
* Separation of duties: whoever moved it into Review can't close it from Review; a second person reviews.
* Fields can be edited only in New, Analyze and Respond. Review and closed records are locked; notes
  can still be added to closed records (e.g. Wepa's reply), never to archived ones.
* Only named staff accounts can make changes (see accounts.py): the shared admin and viewers read only.

Evidence integrity
------------------
Every change is an event appended to records/investigations.jsonl: who, when, what changed (old and new
values), and why. Nothing is ever edited or deleted. Each event carries the SHA-256 hash of the
previous one, so any later edit to the file breaks the chain and verify() reports where. An
investigation's current state is the replay of its events. Closed investigations move to the archive
two years after closing: still searchable and exportable, read-only.

Impact (Low / Moderate / High), recomputed from the data until someone overrides it
----------------------------------------------------------------------------------
Three factors, each scored 1-3: usage (this printer's printing vs the typical printer), redundancy
(another working printer in the building? how far is the nearest walk-in one?) and lost printing
(busy-weighted down hours in the last 30 days vs the campus median). The average sets the level: under
1.5 Low, 2.5 and up High, Moderate between. With
under 90 days of data the level defaults to Moderate. A manual override records who, when and why.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, refs

NEW, ANALYZE, RESPOND, REVIEW = "New", "Analyze", "Respond", "Review"
CLOSED_COMPLETE, CLOSED_CANCELLED, CLOSED_INCOMPLETE = "Closed Complete", "Closed Cancelled", "Closed Incomplete"
STATES = [NEW, ANALYZE, RESPOND, REVIEW, CLOSED_COMPLETE, CLOSED_CANCELLED, CLOSED_INCOMPLETE]
CLOSED = {CLOSED_COMPLETE, CLOSED_CANCELLED, CLOSED_INCOMPLETE}
EDITABLE_IN = {NEW, ANALYZE, RESPOND}
TRANSITIONS = {
    NEW: [ANALYZE, CLOSED_CANCELLED],
    ANALYZE: [RESPOND, CLOSED_CANCELLED, CLOSED_INCOMPLETE],
    RESPOND: [REVIEW, ANALYZE, CLOSED_INCOMPLETE],
    REVIEW: [CLOSED_COMPLETE, CLOSED_INCOMPLETE, RESPOND, ANALYZE],
}
ORDER = {s: i for i, s in enumerate(STATES)}


def is_backward(cur: str, to: str) -> bool:
    return to not in CLOSED and ORDER[to] < ORDER[cur]
EDITABLE = ["title", "description", "assignee", "root_cause", "root_cause_status", "action_taken",
            "itsm_ref", "wepa_case", "impact_override", "impact_reason"]
FIELD_LABEL = {"title": "Name", "description": "Description", "assignee": "Assigned to",
               "root_cause": "Root cause", "root_cause_status": "Root cause is", "action_taken": "Action taken",
               "itsm_ref": "ITSM ticket #", "wepa_case": "Wepa case #", "impact_override": "Impact (set by hand)",
               "impact_reason": "Why the impact was set by hand", "linked": "Linked outages", "state": "State"}
ROOT_CAUSE_STATUS = ("Suspected", "Confirmed")
IMPACTS = ("Low", "Moderate", "High")
ARCHIVE_AFTER_DAYS = 730
MIN_HISTORY_DAYS = 90
SCOPE = ("OUT", "ERR")                 # categories the detector watches
RECUR_WINDOW_D, RECUR_MIN = 14, 3      # this many outages of one category within this many days
DETECT_EVERY_S = 15 * 60
SYSTEM = {"username": "system", "name": "Automatic detection", "can_edit": True}
GENESIS = "0" * 64

_lock = threading.Lock()
_last_detect: dict[str, float] = {}


class InvestigationError(ValueError):
    pass


# --- the event log ------------------------------------------------------------------------------------

def _path(data_dir) -> Path:
    return refs.records_dir(data_dir) / "investigations.jsonl"


def _canonical(e: dict) -> str:
    return json.dumps({k: v for k, v in e.items() if k != "hash"}, sort_keys=True, separators=(",", ":"),
                      default=str)


def _read(data_dir) -> list[dict]:
    p = _path(data_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def verify(data_dir) -> tuple[bool, str]:
    """Check the hash chain. Returns (intact, message)."""
    prev = GENESIS
    events = _read(data_dir)
    for i, e in enumerate(events, 1):
        if e.get("prev") != prev:
            return False, f"Chain broken at event {i}: it doesn't follow the event before it."
        if hashlib.sha256(_canonical(e).encode()).hexdigest() != e.get("hash"):
            return False, f"Event {i} was changed after it was written."
        prev = e["hash"]
    return True, f"All {len(events):,} recorded changes are intact (chain ends {prev[:12]}…)."


def _append(data_dir, inv: str, user: dict, action: str, changes: dict | None = None, note: str = "",
            state: str | None = None) -> dict:
    with _lock, refs._FileLock(refs.records_dir(data_dir) / "investigations.lock"):
        events = _read(data_dir)
        prev = events[-1]["hash"] if events else GENESIS
        e = {"seq": len(events) + 1, "ts": time.time(), "inv": inv, "user": user["username"],
             "user_name": user.get("name", user["username"]), "action": action, "changes": changes or {},
             "note": note.strip(), "prev": prev}
        if state:
            e["state"] = state
        e["hash"] = hashlib.sha256(_canonical(e).encode()).hexdigest()
        p = _path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(e, default=str) + "\n")
        return e


def _replay(events: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in events:
        r = out.setdefault(e["inv"], {"ref": e["inv"], "events": [], "linked": [], "notes": []})
        r["events"].append(e)
        for k, (_, new) in e.get("changes", {}).items():
            r[k] = new
        if e["action"] == "note":
            r["notes"].append(e)
        if e.get("state"):
            r["state"] = e["state"]
            if e["state"] == ANALYZE and not r.get("escalated_at"):
                r["escalated_at"] = e["ts"]
            if e["state"] in CLOSED:
                r["closed_at"], r["closed_by"] = e["ts"], e["user"]
            elif "closed_at" in r:
                r.pop("closed_at", None)
            if e["state"] == REVIEW:
                r["submitted_for_review_by"] = e["user"]
        r["updated_at"] = e["ts"]
    return out


def all_records(data_dir) -> dict[str, dict]:
    recs = _replay(_read(data_dir))
    now = time.time()
    for r in recs.values():
        r["archived"] = bool(r.get("state") in CLOSED and r.get("closed_at")
                             and now - r["closed_at"] > ARCHIVE_AFTER_DAYS * 86400)
    return recs


def get(data_dir, ref: str) -> dict | None:
    return all_records(data_dir).get(ref)


def table(data_dir) -> pd.DataFrame:
    recs = all_records(data_dir)
    cols = ["ref", "title", "state", "location", "station_id", "category", "first_occurrence", "escalated_at",
            "assignee", "impact_override", "updated_at", "archived", "origin", "closed_at"]
    if not recs:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([{c: r.get(c) for c in cols} for r in recs.values()])
    return df.sort_values("ref", ascending=False).reset_index(drop=True)


# --- actions (each one is an event) -----------------------------------------------------------------

def _require_editor(user: dict) -> None:
    if not user.get("can_edit"):
        raise InvestigationError("Only named staff accounts can change investigations. Sign in with your own "
                                 "account (an administrator creates it with `python -m wepa_monitor users add`).")


def create(data_dir, user: dict, station_id: str, title: str, description: str, category: str = "ERR",
           origin: str = "manual", linked: list[str] | None = None, first_occurrence: str | None = None,
           location: str = "", detector: str = "") -> str:
    _require_editor(user)
    if not title.strip():
        raise InvestigationError("A name is required.")
    ref = refs.next_number(data_dir, "INV", {"station_id": station_id, "kind": "investigation"})
    fields = {"title": title.strip()[:160], "description": description.strip(), "station_id": station_id,
              "location": location, "category": category, "origin": origin, "detector": detector,
              "linked": sorted(set(linked or [])), "first_occurrence": first_occurrence,
              "created_at": time.time(), "created_by": user["username"], "state": NEW}
    _append(data_dir, ref, user, "create", {k: [None, v] for k, v in fields.items()}, state=NEW)
    return ref


def update(data_dir, ref: str, user: dict, changes: dict, note: str = "") -> dict:
    _require_editor(user)
    r = get(data_dir, ref)
    if r is None:
        raise InvestigationError(f"No investigation {ref}.")
    if r.get("archived"):
        raise InvestigationError("Archived investigations are read-only.")
    if r["state"] not in EDITABLE_IN:
        raise InvestigationError(f"{ref} is in {r['state']}: fields are locked. Add a note instead"
                                 + ("" if r["state"] in CLOSED else ", or send it back to Respond") + ".")
    diff = {}
    for k, v in changes.items():
        if k not in EDITABLE:
            raise InvestigationError(f"'{k}' can't be edited.")
        v = (v.strip() if isinstance(v, str) else v) or None
        if k == "root_cause_status" and v not in (None, *ROOT_CAUSE_STATUS):
            raise InvestigationError("Root cause must be Suspected or Confirmed.")
        if k == "impact_override" and v not in (None, *IMPACTS):
            raise InvestigationError("Impact must be Low, Moderate or High.")
        if r.get(k) != v:
            diff[k] = [r.get(k), v]
    new_override = diff.get("impact_override", [None, r.get("impact_override")])[1]
    reason = diff.get("impact_reason", [None, r.get("impact_reason")])[1]
    if "impact_override" in diff and new_override and not reason:
        raise InvestigationError("Say why the impact is being set by hand.")
    if not diff:
        return r
    _append(data_dir, ref, user, "update", diff, note)
    return get(data_dir, ref)


def transition(data_dir, ref: str, user: dict, to: str, note: str = "", impact_now: str | None = None) -> dict:
    _require_editor(user)
    r = get(data_dir, ref)
    if r is None:
        raise InvestigationError(f"No investigation {ref}.")
    if r.get("archived"):
        raise InvestigationError("Archived investigations are read-only.")
    cur = r["state"]
    if to not in TRANSITIONS.get(cur, []):
        raise InvestigationError(f"Can't go from {cur} to {to}.")
    note = (note or "").strip()
    if to == ANALYZE and not r.get("assignee"):
        raise InvestigationError("Assign it to someone before moving it to Analyze.")
    if to == RESPOND and cur == ANALYZE and not (r.get("root_cause") and r.get("root_cause_status")):
        raise InvestigationError("Record the root cause (and whether it's suspected or confirmed) before Respond.")
    if to == REVIEW and not r.get("action_taken"):
        raise InvestigationError("Record the action taken before sending it for review.")
    if (to in CLOSED or is_backward(cur, to)) and not note:
        raise InvestigationError("Write the reason for this decision.")
    if cur == REVIEW and to in CLOSED and r.get("submitted_for_review_by") == user["username"]:
        raise InvestigationError("Someone other than the person who submitted it for review has to close it.")
    extra = {}
    if to in CLOSED:
        extra["impact_at_close"] = [None, r.get("impact_override") or impact_now]
    _append(data_dir, ref, user, "transition", {"state": [cur, to], **extra}, note, state=to)
    return get(data_dir, ref)


def add_note(data_dir, ref: str, user: dict, note: str) -> dict:
    _require_editor(user)
    r = get(data_dir, ref)
    if r is None:
        raise InvestigationError(f"No investigation {ref}.")
    if r.get("archived"):
        raise InvestigationError("Archived investigations are read-only.")
    if not (note or "").strip():
        raise InvestigationError("The note is empty.")
    _append(data_dir, ref, user, "note", {}, note)
    return get(data_dir, ref)


# --- impact -------------------------------------------------------------------------------------------

def impact(ds, station_id: str) -> dict:
    """{'level', 'default' (bool), 'factors': [(name, value text, score 1-3)], 'note'}"""
    from . import impact as I, metrics as M, narrative as N, nearby, ops
    days = N.history_days(ds)
    factors = []
    start, end = M.window(ds, 30)
    use = M.usage_by_station(ds, start, end)
    rel = use.set_index("station_id")["relative"].get(station_id, np.nan) if len(use) else np.nan
    s_use = 2 if not np.isfinite(rel) else (3 if rel >= 1.5 else 1 if rel <= 0.5 else 2)
    factors.append(("Usage", "no usage data yet" if not np.isfinite(rel) else f"{rel:.1f}× the typical printer", s_use))
    cur = ops.current_status(ds)
    b = nearby.backups(cur, station_id, 3)
    same = b[b["kind"] == "same building"] if len(b) else b
    walk = b[b["kind"] != "same building"]["seconds"].min() / 60 if len(b) and (b["kind"] != "same building").any() else np.nan
    if len(same):
        s_red, txt = 1, f"{len(same)} other printer{'s' if len(same) != 1 else ''} in the building"
    elif np.isfinite(walk) and walk < 5:
        s_red, txt = 2, f"only printer in the building; nearest walk-in printer {walk:.0f} min away"
    else:
        s_red, txt = 3, ("only printer in the building; nearest walk-in printer "
                         + (f"{walk:.0f} min away" if np.isfinite(walk) else "unknown"))
    factors.append(("Redundancy", txt, s_red))
    t = I.by_station(ds, start, end)
    if len(t):
        w = t.set_index("station_id")["weighted_h"]
        mine = float(w.get(station_id, 0.0))
        med = float(np.median(np.r_[w.values, np.zeros(max(len(ds.stations) - len(w), 0))]))
        ratio = mine / med if med > 0 else (np.inf if mine > 0 else 1.0)
        s_lost = 3 if ratio >= 2 else 1 if ratio <= 0.5 else 2
        factors.append(("Lost printing (30 days)", f"{mine:.1f} busy-weighted hours"
                        + (f", {ratio:.1f}× the campus median" if np.isfinite(ratio) else ""), s_lost))
    else:
        factors.append(("Lost printing (30 days)", "no downtime recorded", 1))
    avg = float(np.mean([f[2] for f in factors]))
    computed = "High" if avg >= 2.5 else "Low" if avg < 1.5 else "Moderate"
    if days < MIN_HISTORY_DAYS:
        return {"level": "Moderate", "computed": computed, "default": True, "factors": factors,
                "note": f"Moderate by default: only {days:.0f} days of data (needs {MIN_HISTORY_DAYS})."}
    return {"level": computed, "computed": computed, "default": False, "factors": factors,
            "note": f"Average factor score {avg:.2f} of 3."}


# --- automatic detection ------------------------------------------------------------------------------

def _location(ds, sid: str) -> str:
    st = ds.stations.set_index("station_id")
    if sid not in st.index:
        return sid
    r = st.loc[sid]
    return f"{r['building']} · {r['description']} (#{sid}) · {r['area']}"


def candidates(ds) -> list[dict]:
    """Printers with recurring OUT or ERR outages in the last RECUR_WINDOW_D days, with the evidence."""
    from scipy import stats

    from . import narrative as N, rules
    inc = ds.sev_inc
    if inc is None or inc.empty or "ref" not in inc:
        return []
    red = inc[(inc["severity"] == "red") & (inc["ref"].astype(str).str[:3].isin(SCOPE))]
    since = ds.as_of - pd.Timedelta(days=RECUR_WINDOW_D)
    recent = red[red["start"] >= since].assign(cat=lambda d: d["ref"].str[:3])
    if recent.empty:
        return []
    n_st = max(len(ds.stations), 1)
    out = []
    for (sid, cat), g in recent.groupby(["station_id", "cat"]):
        if len(g) < RECUR_MIN:
            continue
        others = recent[(recent["cat"] == cat) & (recent["station_id"] != sid)]
        campus = len(others) / max(n_st - 1, 1)
        p = float(stats.poisson.sf(len(g) - 1, max(campus, 0.05)))
        causes = []
        for s0 in g["start"]:
            c = N.outage_causes(ds, sid, s0)
            causes.append(rules.issue_label(c[0][0]) if c else "No cause reported")
        counts = pd.Series(causes).value_counts()
        kind = {"OUT": "network or connection drop-offs", "ERR": "hardware or system faults"}[cat]
        first = g["start"].min()
        name = ds.stations.set_index("station_id")["label"].get(sid, sid)
        desc = (f"{len(g)} {cat} outages in the last {RECUR_WINDOW_D} days (first {first.tz_convert(config.LOCAL_TZ):%a %b %-d, %Y %-I:%M %p}): "
                + ", ".join(f"{k} ×{v}" for k, v in counts.items())
                + f". Other printers averaged {campus:.1f} {cat} outages each over the same days; the chance of "
                f"{len(g)} or more at that rate is {p:.1%}. References: {', '.join(sorted(g['ref']))}.")
        out.append({"station_id": sid, "category": cat, "title": f"Recurring {kind}: {name}", "description": desc,
                    "linked": sorted(g["ref"]), "first_occurrence": first.isoformat(), "p": p,
                    "location": _location(ds, sid)})
    return out


def detect(ds, force: bool = False) -> list[str]:
    """Open New investigations for new patterns; link newer outages to open ones. Returns refs touched."""
    key = str(refs.records_dir(ds.data_dir))
    if not force and time.time() - _last_detect.get(key, 0) < DETECT_EVERY_S:
        return []
    _last_detect[key] = time.time()
    touched = []
    recs = all_records(ds.data_dir)
    for c in candidates(ds):
        same = [r for r in recs.values() if r.get("station_id") == c["station_id"] and r.get("category") == c["category"]]
        open_ = [r for r in same if r.get("state") not in CLOSED]
        if open_:
            r = open_[0]
            add = sorted(set(c["linked"]) - set(r.get("linked") or []))
            if add:
                _append(ds.data_dir, r["ref"], SYSTEM, "update",
                        {"linked": [r.get("linked"), sorted(set(r.get("linked") or []) | set(add))]},
                        f"Linked {len(add)} newer outage{'s' if len(add) != 1 else ''}: {', '.join(add)}.")
                touched.append(r["ref"])
            continue
        last_closed = max((r.get("closed_at") or 0 for r in same), default=0)
        if last_closed:
            newer = [x for x in c["linked"] if (refs.lookup(ds.data_dir, x) or {}).get("start", "")
                     > pd.Timestamp(last_closed, unit="s", tz="UTC").isoformat()]
            if len(newer) < RECUR_MIN:
                continue
        touched.append(create(ds.data_dir, SYSTEM, c["station_id"], c["title"], c["description"], c["category"],
                              origin="auto", linked=c["linked"], first_occurrence=c["first_occurrence"],
                              location=c["location"], detector=f"{RECUR_MIN}+ {c['category']} outages in "
                              f"{RECUR_WINDOW_D} days"))
    archive_due(ds.data_dir)
    return touched


def archive_due(data_dir) -> None:
    """Record, once, that a closed investigation passed two years and moved to the archive."""
    for r in all_records(data_dir).values():
        if r.get("archived") and not any(e["action"] == "archive" for e in r["events"]):
            _append(data_dir, r["ref"], SYSTEM, "archive", {}, "Closed more than two years ago: moved to the archive.")


# --- lifecycle metrics (all derived from the event log, so they're facts, not estimates) ----------------

def fmt_dur(seconds) -> str:
    if seconds is None or (isinstance(seconds, float) and not np.isfinite(seconds)):
        return "—"
    from . import durations
    return durations.human(seconds)


def _owner(ds, station_id):
    from . import support
    st = ds.stations.set_index("station_id")
    return support.owner(st.at[station_id, "section"]) if station_id in st.index else config.DEFAULT_OWNER


def state_visits(r: dict, ds=None, now: float | None = None) -> pd.DataFrame:
    """One row per stay in a state: which state, which time in it (instance), when and by whom it was
    entered, how (opened / forward / sent back / closed), the reason, when it was left and for what, and
    how long it lasted, in calendar time and in the responsible desk's staffed hours."""
    from . import support
    now = now or time.time()
    owner = _owner(ds, r.get("station_id")) if ds is not None and r.get("station_id") else None
    rows, counts, prev = [], {}, None
    for e in r["events"]:
        if not e.get("state"):
            continue
        to = e["state"]
        frm = (e.get("changes", {}).get("state") or [None, None])[0]
        if e["action"] == "create":
            how = "Opened"
        elif to in CLOSED:
            how = "Closed"
        elif frm and is_backward(frm, to):
            how = "Sent back"
        else:
            how = "Forward"
        if prev is not None:
            prev["left_at"], prev["left_by"], prev["next"] = e["ts"], e.get("user_name", e["user"]), to
        counts[to] = counts.get(to, 0) + 1
        prev = {"state": to, "instance": counts[to], "entered_at": e["ts"], "entered_by": e.get("user_name", e["user"]),
                "how": how, "reason": e.get("note", ""), "left_at": None, "left_by": "", "next": ""}
        rows.append(prev)
    df = pd.DataFrame(rows, columns=["state", "instance", "entered_at", "entered_by", "how", "reason", "left_at",
                                     "left_by", "next"])
    if df.empty:
        return df.assign(seconds=[], staffed_seconds=[], current=[])
    end = df["left_at"].where(df["left_at"].notna(), now).astype(float)
    df["current"] = df["left_at"].isna()
    df["seconds"] = np.where(df["state"].isin(CLOSED), np.nan, end - df["entered_at"])
    if owner:
        df["staffed_seconds"] = [np.nan if st in CLOSED else support.staffed_seconds(
            owner, pd.Timestamp(a, unit="s", tz="UTC"), pd.Timestamp(b, unit="s", tz="UTC"))
            for st, a, b in zip(df["state"], df["entered_at"], end)]
    else:
        df["staffed_seconds"] = np.nan
    return df


def metrics(ds, r: dict, now: float | None = None) -> dict:
    """A holistic view of one investigation: lifecycle timing, rework, ownership changes, effort, and
    the outages behind it (before, during and after the investigation)."""
    now = now or time.time()
    v = state_visits(r, ds, now)
    created = r.get("created_at") or (r["events"][0]["ts"] if r["events"] else now)
    closed = r.get("closed_at") if r.get("state") in CLOSED else None
    end = closed or now
    per_state = {}
    for st in (NEW, ANALYZE, RESPOND, REVIEW):
        sv = v[v["state"] == st]
        per_state[st] = {"visits": int(len(sv)), "seconds": float(sv["seconds"].sum()) if len(sv) else 0.0,
                         "staffed_seconds": float(sv["staffed_seconds"].sum()) if len(sv) else 0.0,
                         "sent_back_into": int((sv["how"] == "Sent back").sum()),
                         "first_entered": float(sv["entered_at"].min()) if len(sv) else None}
    assignee_changes = [e["changes"]["assignee"] for e in r["events"] if "assignee" in e.get("changes", {})]
    reassignments = sum(1 for old, new in assignee_changes if old and new and old != new)
    first_assigned = next((e["ts"] for e in r["events"] if (e.get("changes", {}).get("assignee") or [None, None])[1]), None)
    people = sorted({e.get("user_name", e["user"]) for e in r["events"] if e["user"] != "system"})
    out = {
        "open_seconds": end - created, "is_closed": bool(closed), "created_at": created, "closed_at": closed,
        "time_to_assign": (first_assigned - created) if first_assigned else None,
        "time_to_escalate": (r["escalated_at"] - created) if r.get("escalated_at") else None,
        "time_to_close": (closed - created) if closed else None,
        "per_state": per_state, "visits": v,
        "sent_back": int((v["how"] == "Sent back").sum()) if len(v) else 0,
        "reassignments": reassignments, "assignees": len({n for _, n in assignee_changes if n}),
        "changes": sum(1 for e in r["events"] if e["action"] in ("update", "transition")),
        "notes": len(r["notes"]), "people": people,
    }
    # The outages behind it.
    linked = r.get("linked") or []
    inc = ds.sev_inc[ds.sev_inc["ref"].isin(linked)] if ds is not None and "ref" in ds.sev_inc and linked else None
    if inc is not None and len(inc):
        dur = inc["duration_s"].where(inc["end"].notna(), (ds.as_of - inc["start"]).dt.total_seconds()).clip(lower=0)
        c_ts = pd.Timestamp(created, unit="s", tz="UTC")
        cl_ts = pd.Timestamp(closed, unit="s", tz="UTC") if closed else None
        first = inc["start"].min()
        out.update(linked_outages=int(len(inc)), outage_seconds=float(dur.sum()), first_outage=first,
                   last_outage=inc["start"].max(),
                   before_opened=int((inc["start"] < c_ts).sum()),
                   during=int(((inc["start"] >= c_ts) & ((inc["start"] < cl_ts) if cl_ts is not None else True)).sum()),
                   after_closed=int((inc["start"] >= cl_ts).sum()) if cl_ts is not None else None,
                   detection_lag=float((c_ts - first).total_seconds()),
                   end_to_end=float((pd.Timestamp(end, unit="s", tz="UTC") - first).total_seconds()))
    else:
        out.update(linked_outages=0, outage_seconds=0.0, first_outage=None, last_outage=None, before_opened=0,
                   during=0, after_closed=None, detection_lag=None, end_to_end=None)
    # Did the fix hold? Same printer, same category, any outages since closing (linked or not).
    if closed and ds is not None and "ref" in ds.sev_inc and r.get("station_id"):
        after = ds.sev_inc[(ds.sev_inc["station_id"] == r["station_id"]) & (ds.sev_inc["severity"] == "red")
                           & (ds.sev_inc["ref"].astype(str).str[:3] == (r.get("category") or ""))
                           & (ds.sev_inc["start"] >= pd.Timestamp(closed, unit="s", tz="UTC"))]
        out["recurred_after_close"] = int(len(after))
    else:
        out["recurred_after_close"] = None
    return out


def metrics_table(ds) -> pd.DataFrame:
    """One row per investigation, every lifecycle measure in seconds (for sorting and export)."""
    rows = []
    for r in all_records(ds.data_dir).values():
        m = metrics(ds, r)
        row = {"ref": r["ref"], "title": r.get("title"), "state": r.get("state"), "location": r.get("location"),
               "assignee": r.get("assignee"), "archived": r.get("archived", False),
               "opened": pd.Timestamp(m["created_at"], unit="s", tz="UTC"),
               "closed": pd.Timestamp(m["closed_at"], unit="s", tz="UTC") if m["closed_at"] else pd.NaT,
               "open_s": m["open_seconds"], "to_assign_s": m["time_to_assign"], "to_escalate_s": m["time_to_escalate"],
               "to_close_s": m["time_to_close"], "sent_back": m["sent_back"], "reassignments": m["reassignments"],
               "changes": m["changes"], "notes": m["notes"], "people": len(m["people"]),
               "linked_outages": m["linked_outages"], "outage_s": m["outage_seconds"], "during": m["during"],
               "after_closed": m["recurred_after_close"], "detection_lag_s": m["detection_lag"],
               "end_to_end_s": m["end_to_end"]}
        for st, d in m["per_state"].items():
            key = st.lower()
            row[f"{key}_s"], row[f"{key}_visits"], row[f"{key}_staffed_s"] = d["seconds"], d["visits"], d["staffed_seconds"]
        rows.append(row)
    return pd.DataFrame(rows).sort_values("ref", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()


def state_summary(table: pd.DataFrame) -> pd.DataFrame:
    """Across investigations: for each state, how many entered it, visits, median and longest total time."""
    out = []
    for st in (NEW, ANALYZE, RESPOND, REVIEW):
        k = st.lower()
        if table.empty or f"{k}_s" not in table:
            continue
        t = table[table[f"{k}_visits"] > 0]
        out.append({"state": st, "investigations": int(len(t)), "visits": int(t[f"{k}_visits"].sum()),
                    "repeat_visits": int((t[f"{k}_visits"] - 1).clip(lower=0).sum()),
                    "median_s": float(t[f"{k}_s"].median()) if len(t) else np.nan,
                    "max_s": float(t[f"{k}_s"].max()) if len(t) else np.nan,
                    "median_staffed_s": float(t[f"{k}_staffed_s"].median()) if len(t) else np.nan})
    return pd.DataFrame(out)
