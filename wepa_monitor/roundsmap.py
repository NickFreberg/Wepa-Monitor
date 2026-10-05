"""The Rounds map: points and paths the administrator draws, so Rounds plans real routes.

Points
  start    where a round can begin (an office, a desk)
  end      where a one-way round can finish
  door     the entrance to use for a building; Rounds routes to the door instead of the building's middle
  parking  where the transit van parks for a building
Paths
  walk     a walking path (a shortcut, a cut-through, a path OpenStreetMap is missing); joins the walking network
  van      a road or lane the van can use; joins the driving network

Stored in records/rounds_map.json in the data folder. Everyone signed in can read it (Rounds uses it); only the
administrator can change it, from the editor at /rounds/map (a phone-friendly Leaflet page). Saves are checked
(campus bounds, sizes, kinds) and written atomically, with one backup of the previous version.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

POINT_KINDS = ("start", "end", "door", "parking")
PATH_KINDS = ("walk", "van")
BOUNDS = (41.975, 42.000, -70.990, -70.950)       # south, north, west, east: main campus with a margin
MAX_POINTS, MAX_PATHS, MAX_VERTICES = 400, 300, 400
_dir: Path | None = None


def configure(data_dir: Path) -> None:
    global _dir
    _dir = Path(data_dir)


def path() -> Path | None:
    return _dir / "records" / "rounds_map.json" if _dir else None


def empty() -> dict:
    return {"version": 1, "points": [], "paths": [], "updated": None, "updated_by": None}


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
    seen = set()
    for i, p in enumerate(pts):
        if not isinstance(p, dict) or p.get("kind") not in POINT_KINDS:
            raise MapError("a point has an unknown kind")
        pid = _id(p.get("id"), "p", i)
        if pid in seen:
            raise MapError("two points share an id")
        seen.add(pid)
        lat, lon = _coord(p.get("lat"), p.get("lon"))
        out["points"].append({"id": pid, "kind": p["kind"], "lat": lat, "lon": lon,
                              "name": _text(p.get("name")), "building": _text(p.get("building"))})
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


def door_for(building: str) -> tuple[float, float] | None:
    for p in points("door"):
        if p["building"] == building:
            return p["lat"], p["lon"]
    return None


def parking_for(building: str) -> tuple[float, float] | None:
    for p in points("parking"):
        if p["building"] == building:
            return p["lat"], p["lon"]
    return None


def place_name(p: dict) -> str:
    """How a start or end point appears in Rounds."""
    base = p["name"] or (f"{p['building']} " if p["building"] else "") + ("start" if p["kind"] == "start" else "end")
    return base.strip()


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
    boot = json.dumps({"buildings": buildings, "csrf": csrf}).replace("</", "<\\/")
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
  <button type="button" data-tool="select" class="is-on"><span class="rm-ico rm-ico--select"></span>Select</button>
  <button type="button" data-tool="start"><span class="rm-ico rm-ico--start"></span>Start</button>
  <button type="button" data-tool="door"><span class="rm-ico rm-ico--door"></span>Door</button>
  <button type="button" data-tool="parking"><span class="rm-ico rm-ico--van"></span>Van spot</button>
  <button type="button" data-tool="end"><span class="rm-ico rm-ico--end"></span>End</button>
  <button type="button" data-tool="walk"><span class="rm-ico rm-ico--walk"></span>Walk path</button>
  <button type="button" data-tool="van"><span class="rm-ico rm-ico--route"></span>Van route</button>
</nav>
<div class="rm-line" id="rm-line" hidden>
  <button type="button" id="rm-undo" class="rm-btn">Undo point</button>
  <button type="button" id="rm-cancel" class="rm-btn">Cancel</button>
  <button type="button" id="rm-finish" class="rm-btn rm-btn--primary">Finish line</button>
</div>
<script id="rm-boot" type="application/json">{boot}</script>
<script src="/assets/vendor/leaflet/leaflet.js"></script>
<script src="/assets/vendor/roundsmap/editor.js"></script>
</body></html>"""

