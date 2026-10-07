"""The Rounds map: points and paths the administrator draws, so Rounds plans real routes.

Points
  home     Home Base: where a round begins and ends (an RSR station, the IT Service Center in Maxwell, the ResNet
           office in East Campus Commons). Can belong to a team.
  door     a building entrance; Rounds routes to the door instead of the building's middle. A door can be marked
           accessible (shown with the blue wheelchair symbol); "Use accessible entrances" in Rounds prefers those.
  printer  where a printer (or printers) is in a building: a stop on a round, or its final destination
  parking  a parking space for a transit van (ResNet and the IT Service Center each have one); can belong to a team
  van      where a team's transit van is right now (one per team). In van mode, a round starts by walking to it,
           and Rounds compares that with going on foot
  fuel     the fuel station (only one)
  closet   a supply closet, where consumables are kept; Rounds can start from one
Paths
  walk        a walking path, always usable both ways; joins the walking network
  van         a van route usable both ways; joins the driving network
  van_oneway  a van route one way only, in the direction it was drawn (one-way streets)

Stored in records/rounds_map.json in the data folder. Everyone signed in can read it (Rounds uses it); only the
administrator can change it, from the editor at /rounds/map (a phone-friendly Leaflet page). Saves are checked
(campus bounds, sizes, kinds, one fuel station, one van per team) and written atomically, with one backup of the
previous version. Maps saved by earlier versions are read as: start and end -> Home Base, van spot -> Parking.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

POINT_KINDS = ("home", "door", "printer", "parking", "van", "fuel", "closet")
PATH_KINDS = ("walk", "van", "van_oneway")
LEGACY_KINDS = {"start": "home", "end": "home"}       # version 1 maps
TEAMS = ("ResNet", "IT Service Center")
SINGLE = {"fuel"}                                     # one on the whole map
ONE_PER_TEAM = {"van"}                                # one per team
BOUNDS = (41.975, 42.000, -70.990, -70.950)       # south, north, west, east: main campus with a margin
MAX_POINTS, MAX_PATHS, MAX_VERTICES = 400, 300, 400
_dir: Path | None = None


def configure(data_dir: Path) -> None:
    global _dir
    _dir = Path(data_dir)


def path() -> Path | None:
    return _dir / "records" / "rounds_map.json" if _dir else None


def empty() -> dict:
    return {"version": 2, "points": [], "paths": [], "updated": None, "updated_by": None}


def load() -> dict:
    p = path()
    if not p or not p.exists():
        return empty()
    try:
        data = json.loads(p.read_text())
        return validate(data)
    except (OSError, ValueError):
        return empty()


def stamp() -> float:
    """Changes whenever the map is saved (used to refresh the routing graphs)."""
    p = path()
    return p.stat().st_mtime if p and p.exists() else 0.0


class MapError(ValueError):
    pass


def _coord(lat, lon) -> tuple[float, float]:
    try:
        lat, lon = round(float(lat), 6), round(float(lon), 6)
    except (TypeError, ValueError) as exc:
        raise MapError("a coordinate isn't a number") from exc
    s, n, w, e = BOUNDS
    if not (s <= lat <= n and w <= lon <= e):
        raise MapError("a point is outside the main campus")
    return lat, lon


def _text(value, limit: int = 80) -> str:
    return re.sub(r"[\x00-\x1f<>]", "", str(value or "")).strip()[:limit]


def _id(value, prefix: str, i: int) -> str:
    v = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:32]
    return v or f"{prefix}{i}"


LABELS = {"home": "Home Base", "door": "Door", "printer": "Printer", "parking": "Parking space",
          "van": "Van location", "fuel": "Fuel station", "closet": "Supply closet"}


def validate(data: dict) -> dict:
    """A clean copy of `data`, or MapError. Unknown fields are dropped; text is trimmed and stripped of markup."""
    if not isinstance(data, dict):
        raise MapError("the map must be an object")
    pts, paths = data.get("points") or [], data.get("paths") or []
    if not isinstance(pts, list) or not isinstance(paths, list):
        raise MapError("points and paths must be lists")
    if len(pts) > MAX_POINTS or len(paths) > MAX_PATHS:
        raise MapError("the map has too many points or paths")
    out = empty()
    seen, singles, vans = set(), set(), set()
    for i, p in enumerate(pts):
        if not isinstance(p, dict):
            raise MapError("a point has an unknown kind")
        kind = LEGACY_KINDS.get(p.get("kind"), p.get("kind"))
        if kind not in POINT_KINDS:
            raise MapError("a point has an unknown kind")
        pid = _id(p.get("id"), "p", i)
        if pid in seen:
            raise MapError("two points share an id")
        seen.add(pid)
        lat, lon = _coord(p.get("lat"), p.get("lon"))
        team = p.get("team") if p.get("team") in TEAMS else ""
        if kind in SINGLE:
            if kind in singles:
                raise MapError(f"the map can have only one {LABELS[kind].lower()}")
            singles.add(kind)
        if kind in ONE_PER_TEAM:
            if not team:
                raise MapError("say whose van it is (ResNet or IT Service Center)")
            if team in vans:
                raise MapError(f"{team}'s van can only be in one place")
            vans.add(team)
        q = {"id": pid, "kind": kind, "lat": lat, "lon": lon, "name": _text(p.get("name")),
             "building": _text(p.get("building"))}
        if kind == "door":
            q["accessible"] = bool(p.get("accessible"))
        if kind in ("home", "parking", "van"):
            q["team"] = team
        out["points"].append(q)
    for i, q in enumerate(paths):
        if not isinstance(q, dict) or q.get("kind") not in PATH_KINDS:
            raise MapError("a path has an unknown kind")
        coords = q.get("coords") or []
        if not isinstance(coords, list) or not 2 <= len(coords) <= MAX_VERTICES:
            raise MapError("a path needs between 2 and 400 points")
        line = [list(_coord(c[0], c[1])) for c in coords if isinstance(c, (list, tuple)) and len(c) == 2]
        if len(line) != len(coords):
            raise MapError("a path has a bad point")
        out["paths"].append({"id": _id(q.get("id"), "l", i), "kind": q["kind"], "coords": line,
                             "name": _text(q.get("name"))})
    out["updated"], out["updated_by"] = data.get("updated"), _text(data.get("updated_by"), 64) or None
    return out


def save(data: dict, user: str) -> dict:
    clean = validate(data)
    clean["updated"], clean["updated_by"] = time.time(), _text(user, 64)
    p = path()
    if p is None:
        raise MapError("no data folder is configured")
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.with_suffix(".prev.json").write_text(p.read_text())
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1))
    os.replace(tmp, p)
    return clean


# --- what Rounds uses ------------------------------------------------------------------------------------------

def points(kind: str) -> list[dict]:
    return [p for p in load()["points"] if p["kind"] == kind]


def paths(kind: str) -> list[dict]:
    return [q for q in load()["paths"] if q["kind"] == kind]


def _xy(p: dict | None) -> tuple[float, float] | None:
    return (p["lat"], p["lon"]) if p else None


def door_for(building: str, accessible: bool = False) -> tuple[float, float] | None:
    """The door Rounds uses for a building: an accessible one first when asked for, else the first drawn."""
    doors = [p for p in points("door") if p["building"] == building]
    if accessible:
        doors = sorted(doors, key=lambda p: not p.get("accessible"))
    return _xy(doors[0] if doors else None)


def printer_for(building: str) -> tuple[float, float] | None:
    return _xy(next((p for p in points("printer") if p["building"] == building), None))


def parking_for(building: str, team: str = "") -> tuple[float, float] | None:
    """A parking space for this building: the team's own first, then one shared by both vans."""
    spaces = [p for p in points("parking") if p["building"] == building and p.get("team") in ("", team)]
    spaces.sort(key=lambda p: p.get("team") != team)
    return _xy(spaces[0] if spaces else None)


def van_location(team: str) -> dict | None:
    return next((p for p in points("van") if p.get("team") == team), None)


def fuel_station() -> dict | None:
    return next(iter(points("fuel")), None)


def place_name(p: dict) -> str:
    """How a Home Base or supply closet appears in Rounds."""
    base = p["name"] or ((f"{p['building']} " if p["building"] else "") + LABELS.get(p["kind"], p["kind"]).lower())
    return base.strip()[:1].upper() + base.strip()[1:]


# --- web: editor page and JSON ----------------------------------------------------------------------------------

def _can_edit() -> bool:
    from . import accounts, auth
    if not auth.enabled():
        return True
    return accounts.current().get("role") == "admin"


def install(server) -> None:
    from flask import Response, jsonify, request, session

    from . import accounts, security

    @server.route("/_rounds/map.json", methods=["GET"])
    def _rounds_map_get():
        return jsonify({**load(), "can_edit": _can_edit()})

    @server.route("/_rounds/map.json", methods=["POST"])
    def _rounds_map_post():
        if not _can_edit():
            return jsonify(error="Only the administrator can change the Rounds map."), 403
        token = request.headers.get("X-CSRF-Token", "")
        if not token or not session.get("csrf") or not _same(token, session["csrf"]):
            return jsonify(error="The editor's session expired. Reload the page and try again."), 400
        try:
            saved = save(request.get_json(force=True, silent=False), accounts.current().get("username", "local"))
        except (MapError, ValueError, TypeError) as exc:
            return jsonify(error=str(exc) or "That map couldn't be saved."), 400
        security.audit("rounds map saved", f"{len(saved['points'])} points, {len(saved['paths'])} paths")
        return jsonify(saved)

    @server.route("/rounds/map", methods=["GET"])
    def _rounds_map_page():
        if not _can_edit():
            return Response("Only the administrator can edit the Rounds map.", 403, mimetype="text/plain")
        return Response(editor_html(_csrf()), mimetype="text/html", headers={"Cache-Control": "no-store"})

    def _csrf() -> str:
        import secrets
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]


def _same(a: str, b: str) -> bool:
    import secrets
    return secrets.compare_digest(a, b)


def editor_html(csrf: str) -> str:
    from . import reference
    b = reference.load_buildings()
    b = b[(b["campus"] == "Main") & b["lat"].notna()]
    buildings = [{"name": r.building, "lat": float(r.lat), "lon": float(r.lon)} for r in b.itertuples()]
    boot = json.dumps({"buildings": buildings, "csrf": csrf, "teams": list(TEAMS)}).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Rounds map editor · BSU Student Printing Ops</title>
<link rel="icon" href="/assets/favicon.ico">
<link rel="stylesheet" href="/assets/vendor/leaflet/leaflet.css">
<link rel="stylesheet" href="/assets/vendor/roundsmap/editor.css">
</head><body>
<header class="rm-top"><a href="/rounds" class="rm-back">‹ Rounds</a><h1>Rounds map</h1>
<span id="rm-status" class="rm-status" role="status" aria-live="polite"></span>
<button id="rm-save" class="rm-btn rm-btn--primary" type="button" disabled>Save</button></header>
<div id="rm-map" class="rm-map" aria-label="Campus map"></div>
<div id="rm-hint" class="rm-hint" role="status"></div>
<div id="rm-sheet" class="rm-sheet" hidden></div>
<nav class="rm-tools" aria-label="Tools">
  <button type="button" data-tool="select" class="is-on"><span class="rm-ico"></span>Select</button>
  <button type="button" data-tool="erase"><span class="rm-ico"></span>Eraser</button>
  <button type="button" data-tool="home"><span class="rm-ico"></span>Home Base</button>
  <button type="button" data-tool="door"><span class="rm-ico"></span>Door</button>
  <button type="button" data-tool="printer"><span class="rm-ico"></span>Printer</button>
  <button type="button" data-tool="parking"><span class="rm-ico"></span>Parking</button>
  <button type="button" data-tool="van"><span class="rm-ico"></span>Van now</button>
  <button type="button" data-tool="fuel"><span class="rm-ico"></span>Fuel</button>
  <button type="button" data-tool="closet"><span class="rm-ico"></span>Supplies</button>
  <button type="button" data-tool="walk"><span class="rm-ico"></span>Walk path</button>
  <button type="button" data-tool="van_route"><span class="rm-ico"></span>Van route</button>
  <button type="button" data-tool="van_oneway"><span class="rm-ico"></span>One-way</button>
</nav>
<div class="rm-line" id="rm-line" hidden>
  <button type="button" id="rm-undo" class="rm-btn">Undo point</button>
  <button type="button" id="rm-cancel" class="rm-btn">Cancel</button>
  <button type="button" id="rm-finish" class="rm-btn rm-btn--primary">Finish line</button>
</div>
<div class="rm-line" id="rm-erasebar" hidden>
  <button type="button" id="rm-unerase" class="rm-btn" disabled>Undo erase</button>
  <button type="button" id="rm-clear" class="rm-btn rm-btn--danger">Clear…</button>
</div>
<script id="rm-boot" type="application/json">{boot}</script>
<script src="/assets/vendor/leaflet/leaflet.js"></script>
<script src="/assets/vendor/roundsmap/editor.js"></script>
</body></html>"""

