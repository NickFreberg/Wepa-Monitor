"""Rounds planner: the fastest route to every printer that needs attention.

The campus is a graph built from OpenStreetMap: one network for walking (footways, paths,
steps and roads) and one for driving the transit van (roads and service drives, one-way
streets respected). Shortest paths between stops come from Dijkstra's algorithm on that graph;
the order of stops is then chosen to minimize total time (exact for up to 10 stops, nearest
neighbor plus 2-opt beyond that), optionally visiting down printers before everything else.

In van mode each stop is reached by driving to that building's parking spot and walking in;
parking spots come from reference/parking.csv (hand-picked) or, failing that, the nearest
OpenStreetMap parking lot.

The network is stored in reference/campus_network.json so the planner works offline;
`python -m wepa_monitor network` rebuilds it from OpenStreetMap.
"""
from __future__ import annotations

import heapq
import itertools
import json
import math
import defusedxml.ElementTree as ET   # hardened parser: no entity expansion or external entities
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import pandas as pd
import requests

from . import config

NETWORK_PATH = config.REFERENCE_DIR / "campus_network.json"
PARKING_PATH = config.REFERENCE_DIR / "parking.csv"
OSM_BBOX = (-70.980, 41.981, -70.956, 41.993)        # west, south, east, north: main campus

WALK_TYPES = {"footway", "path", "pedestrian", "steps", "service", "residential", "living_street", "unclassified",
              "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link", "track",
              "corridor", "crossing", "cycleway"}
# Van speeds (m/s): campus drives are slow; streets around campus are 25 mph.
DRIVE_SPEED = {"service": 4.5, "living_street": 4.5, "residential": 9.0, "unclassified": 9.0, "tertiary": 11.0,
               "tertiary_link": 9.0, "secondary": 11.0, "secondary_link": 9.0, "primary": 11.0, "primary_link": 9.0}
WALK_SPEED = 1.3                     # m/s, about 3 mph
STEPS_PENALTY = 1.5                  # steps are slower than flat paths
PARK_AND_LOCK_S = 90                 # parking the van and getting out, per stop

STARTS = {   # named starting points: label -> building
    "ResNet office (East Campus Commons, Rm 107)": "East Campus Commons",
    "IT Service Center (Maxwell Library, ground floor)": "Maxwell Library",
    "RSR desk: Shea/Durgin": "Shea/Durgin",
    "RSR desk: Crimson Hall": "Crimson Hall",
    "RSR desk: Scott Hall": "Scott Hall",
}
TEAM_STARTS = {"ResNet": ["ResNet office (East Campus Commons, Rm 107)", "RSR desk: Shea/Durgin",
                          "RSR desk: Crimson Hall", "RSR desk: Scott Hall"],
               "IT Service Center": ["IT Service Center (Maxwell Library, ground floor)"]}
# Minutes on site, by work-queue kind.
SERVICE_MIN = {"red": 10, "yellow": 6, "tray": 4, "consumable_now": 8, "consumable_soon": 8, "stale": 6}


# --- building the network ------------------------------------------------------------------------------------

def _meters(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def build_network(osm_xml: str) -> dict:
    """Walking and driving edge lists, plus OSM parking lots, from an OSM API /map response."""
    root = ET.fromstring(osm_xml)
    coords = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in root.iter("node")}
    walk, drive, lots, used = [], [], [], set()
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
        refs = [n.get("ref") for n in w.iter("nd") if n.get("ref") in coords]
        if tags.get("amenity") == "parking" and refs:
            lat = sum(coords[r][0] for r in refs) / len(refs)
            lon = sum(coords[r][1] for r in refs) / len(refs)
            lots.append({"name": tags.get("name", ""), "kind": tags.get("parking", ""),
                         "access": tags.get("access", ""), "lat": round(lat, 6), "lon": round(lon, 6)})
        hw = tags.get("highway")
        if not hw or len(refs) < 2 or tags.get("area") == "yes":
            continue
        foot_ok = hw in WALK_TYPES and tags.get("foot") != "no"
        car_ok = hw in DRIVE_SPEED and tags.get("motor_vehicle") != "no" and tags.get("service") != "drive-through"
        oneway = tags.get("oneway")
        for a, b in zip(refs, refs[1:]):
            d = _meters(coords[a], coords[b])
            if foot_ok:
                walk.append((a, b, round(d * (STEPS_PENALTY if hw == "steps" else 1), 1)))
                used |= {a, b}
            if car_ok:
                t = round(d / DRIVE_SPEED[hw], 1)
                if oneway == "-1":
                    drive.append((b, a, round(d, 1), t))
                else:
                    drive.append((a, b, round(d, 1), t))
                    if oneway not in ("yes", "true", "1"):
                        drive.append((b, a, round(d, 1), t))
                used |= {a, b}
    # Renumber nodes compactly to keep the JSON small.
    ids = {nid: i for i, nid in enumerate(sorted(used))}
    return {"source": "OpenStreetMap contributors (ODbL), api.openstreetmap.org", "bbox": OSM_BBOX,
            "nodes": [[round(coords[n][0], 7), round(coords[n][1], 7)] for n in sorted(used)],
            "walk": [[ids[a], ids[b], d] for a, b, d in walk],
            "drive": [[ids[a], ids[b], d, t] for a, b, d, t in drive],
            "parking": lots}


def fetch_network() -> dict:
    w, s, e, n = OSM_BBOX
    r = requests.get(f"https://api.openstreetmap.org/api/0.6/map?bbox={w},{s},{e},{n}",
                     headers={"User-Agent": config.USER_AGENT}, timeout=120)
    r.raise_for_status()
    net = build_network(r.text)
    NETWORK_PATH.write_text(json.dumps(net, separators=(",", ":")))
    _network.cache_clear()
    return net


# --- graph + Dijkstra ----------------------------------------------------------------------------------------------

@dataclass
class Graph:
    xy: np.ndarray                          # node -> (lat, lon)
    adj: list[list[tuple[int, float]]]      # node -> [(neighbor, cost seconds)]
    meters: dict[tuple[int, int], float]    # edge -> length
    usable: np.ndarray                      # nodes in the main connected part

    def nearest(self, lat: float, lon: float) -> int:
        cand = np.where(self.usable)[0]
        d = (self.xy[cand, 0] - lat) ** 2 + ((self.xy[cand, 1] - lon) * math.cos(math.radians(lat))) ** 2
        return int(cand[int(np.argmin(d))])


def dijkstra(g: Graph, source: int) -> tuple[np.ndarray, np.ndarray]:
    """Classic Dijkstra with a binary heap: (cost to every node, predecessor of every node)."""
    n = len(g.adj)
    dist = np.full(n, np.inf)
    prev = np.full(n, -1, dtype=np.int64)
    dist[source] = 0.0
    heap = [(0.0, source)]
    done = np.zeros(n, dtype=bool)
    while heap:
        d, u = heapq.heappop(heap)
        if done[u]:
            continue
        done[u] = True
        for v, w in g.adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    return dist, prev


def path_to(prev: np.ndarray, target: int) -> list[int]:
    out = []
    while target != -1:
        out.append(int(target))
        target = prev[target]
    return out[::-1]


def _graph(net: dict, mode: str) -> Graph:
    xy = np.array(net["nodes"], dtype=float)
    n = len(xy)
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    meters: dict[tuple[int, int], float] = {}
    if mode == "walk":
        for a, b, d in net["walk"]:
            adj[a].append((b, d / WALK_SPEED))
            adj[b].append((a, d / WALK_SPEED))
            meters[(a, b)] = meters[(b, a)] = d
    else:
        for a, b, d, t in net["drive"]:
            adj[a].append((b, t))
            meters[(a, b)] = d
    # Keep the largest connected part (undirected), so nothing snaps to an isolated stub.
    und: list[set] = [set() for _ in range(n)]
    for a in range(n):
        for b, _ in adj[a]:
            und[a].add(b)
            und[b].add(a)
    seen = np.full(n, -1)
    best, best_size, comp = -1, 0, 0
    for s in range(n):
        if seen[s] != -1 or not und[s]:
            continue
        stack, size = [s], 0
        seen[s] = comp
        while stack:
            u = stack.pop()
            size += 1
            for v in und[u]:
                if seen[v] == -1:
                    seen[v] = comp
                    stack.append(v)
        if size > best_size:
            best, best_size = comp, size
        comp += 1
    return Graph(xy, adj, meters, seen == best)


@lru_cache(maxsize=1)
def _network(stamp: float) -> dict | None:
    return json.loads(NETWORK_PATH.read_text()) if NETWORK_PATH.exists() else None


VAN_PATH_SPEED = 4.5          # m/s on a lane the administrator drew (campus service-road speed)
JOIN_MAX_M = 150              # a drawn path's ends join the nearest network node within this distance


def _with_drawn(net: dict, mode: str) -> dict:
    """The network plus the paths drawn on the Rounds map (roundsmap.py) for this mode: each drawn line becomes
    new nodes and edges, and its two ends join the nearest existing node of the same mode. Walk paths and van
    routes go both ways; a one-way van route only in the direction it was drawn."""
    from . import roundsmap
    drawn = roundsmap.paths("walk") if mode == "walk" else roundsmap.paths("van") + roundsmap.paths("van_oneway")
    if not drawn:
        return net
    nodes = [list(n) for n in net["nodes"]]
    walk, drive = list(net["walk"]), list(net["drive"])
    base = {a for e in (walk if mode == "walk" else drive) for a in e[:2]}
    base_idx = np.array(sorted(base)) if base else np.array([], dtype=int)
    base_xy = np.array(net["nodes"], dtype=float)[base_idx] if len(base_idx) else np.zeros((0, 2))

    def join(lat, lon):
        if not len(base_idx):
            return None
        d = (base_xy[:, 0] - lat) ** 2 + ((base_xy[:, 1] - lon) * math.cos(math.radians(lat))) ** 2
        j = int(base_idx[int(np.argmin(d))])
        m = _meters((lat, lon), tuple(net["nodes"][j]))
        return (j, m) if m <= JOIN_MAX_M else None

    def add(a, b, m, both=True):
        if mode == "walk":
            walk.append([a, b, m])
        else:
            drive.append([a, b, m, m / VAN_PATH_SPEED])
            if both:
                drive.append([b, a, m, m / VAN_PATH_SPEED])

    for q in drawn:
        both = q["kind"] != "van_oneway"
        ids = []
        for lat, lon in q["coords"]:
            nodes.append([lat, lon])
            ids.append(len(nodes) - 1)
        for a, b in zip(ids, ids[1:]):
            add(a, b, _meters(tuple(nodes[a]), tuple(nodes[b])), both)
        first, last = join(*nodes[ids[0]]), join(*nodes[ids[-1]])
        if first:                          # onto the line at its start, off it at its end
            add(first[0], ids[0], max(first[1], 0.5), both)
        if last:
            add(ids[-1], last[0], max(last[1], 0.5), both)
    return {**net, "nodes": nodes, "walk": walk, "drive": drive}


@lru_cache(maxsize=4)
def _graph_cached(stamp: float, mode: str, drawn_stamp: float = 0.0) -> Graph:
    return _graph(_with_drawn(_network(stamp), mode), mode)


def network() -> dict | None:
    return _network(NETWORK_PATH.stat().st_mtime if NETWORK_PATH.exists() else 0)


def graph(mode: str) -> Graph:
    from . import roundsmap
    return _graph_cached(NETWORK_PATH.stat().st_mtime, mode, roundsmap.stamp())


# --- parking -------------------------------------------------------------------------------------------------------

def parking_spots(buildings: pd.DataFrame, team: str = "") -> pd.DataFrame:
    """Where the van parks for each building: a van spot on the Rounds map first, then reference/parking.csv
    when filled in, else the nearest OSM parking lot that isn't marked no-access."""
    from . import roundsmap
    out = buildings[["building", "lat", "lon"]].copy()
    out["park_lat"], out["park_lon"], out["park_source"] = np.nan, np.nan, ""
    for i, r in out.iterrows():
        spot = roundsmap.parking_for(r["building"], team)
        if spot:
            out.loc[i, ["park_lat", "park_lon"]] = spot
            out.loc[i, "park_source"] = "your Rounds map"
    if PARKING_PATH.exists():
        user = pd.read_csv(PARKING_PATH)
        user = user.dropna(subset=["lat", "lon"]).drop_duplicates("building", keep="first").set_index("building")
        hit = out["building"].isin(user.index) & out["park_lat"].isna()
        out.loc[hit, "park_lat"] = out.loc[hit, "building"].map(user["lat"])
        out.loc[hit, "park_lon"] = out.loc[hit, "building"].map(user["lon"])
        out.loc[hit, "park_source"] = "your parking list"
    net = network()
    lots = pd.DataFrame(net["parking"]) if net and net.get("parking") else pd.DataFrame()
    if len(lots):
        lots = lots[~lots["access"].isin(["no"])]
        for i, r in out[out["park_lat"].isna()].iterrows():
            d = lots.apply(lambda x: _meters((r["lat"], r["lon"]), (x["lat"], x["lon"])), axis=1)
            j = d.idxmin()
            out.loc[i, ["park_lat", "park_lon"]] = lots.loc[j, ["lat", "lon"]].to_numpy(dtype=float)
            out.loc[i, "park_source"] = f"nearest lot on OpenStreetMap{': ' + lots.loc[j, 'name'] if lots.loc[j, 'name'] else ''}"
    return out


# --- planning ------------------------------------------------------------------------------------------------------

@dataclass
class Stop:
    building: str
    lat: float
    lon: float
    items: list[dict]                       # work-queue rows at this building
    priority: int                           # 0 = has a down printer
    service_min: float


@dataclass
class Leg:
    to: str
    mode: str                               # "walk" or "drive"
    meters: float
    seconds: float
    path: list[tuple[float, float]]


@dataclass
class Plan:
    start: str
    mode: str
    order: list[Stop]
    legs: list[list[Leg]]                   # per stop, the legs to reach it (drive + walk in van mode)
    back: list[Leg] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    end: str | None = None                  # a one-way round's finishing place (None: back at start or last stop)

    @property
    def travel_s(self) -> float:
        return sum(l.seconds for ls in self.legs + [self.back] for l in ls)

    @property
    def meters(self) -> float:
        return sum(l.meters for ls in self.legs + [self.back] for l in ls)

    @property
    def service_s(self) -> float:
        return sum(s.service_min for s in self.order) * 60


def _order(cost: np.ndarray, priority: list[int], round_trip: bool, urgent_first: bool) -> list[int]:
    """Best visiting order of stops 1..n starting from 0. Exact (Held-Karp) for <= 10 stops,
    otherwise nearest neighbor improved with 2-opt. With urgent_first, priority-0 stops come first."""
    n = len(cost) - 1
    if n == 0:
        return []
    groups = [list(range(1, n + 1))]
    if urgent_first:
        groups = [[i for i in range(1, n + 1) if priority[i - 1] == p] for p in sorted(set(priority))]
        groups = [g for g in groups if g]
    order, here = [], 0
    for gi, grp in enumerate(groups):
        back = round_trip and gi == len(groups) - 1
        seq = _held_karp(cost, here, grp, back) if len(grp) <= 10 else _two_opt(cost, here, _nearest(cost, here, grp), back)
        order += seq
        here = seq[-1]
    return order


def _tour_cost(cost, start, seq, back):
    c, here = 0.0, start
    for s in seq:
        c += cost[here, s]
        here = s
    return c + (cost[here, start] if back else 0.0)


def _held_karp(cost, start, nodes, back):
    k = len(nodes)
    best: dict[tuple[int, int], tuple[float, int]] = {}
    for i in range(k):
        best[(1 << i, i)] = (cost[start, nodes[i]], -1)
    for size in range(2, k + 1):
        for subset in itertools.combinations(range(k), size):
            mask = sum(1 << i for i in subset)
            for j in subset:
                pm = mask & ~(1 << j)
                best[(mask, j)] = min(((best[(pm, i)][0] + cost[nodes[i], nodes[j]], i) for i in subset if i != j),
                                      key=lambda x: x[0])
    full = (1 << k) - 1
    end = min(range(k), key=lambda j: best[(full, j)][0] + (cost[nodes[j], start] if back else 0))
    seq, mask, j = [], full, end
    while j != -1:
        seq.append(nodes[j])
        mask, j = mask & ~(1 << j), best[(mask, j)][1]
    return seq[::-1]


def _nearest(cost, start, nodes):
    left, seq, here = set(nodes), [], start
    while left:
        nxt = min(left, key=lambda j: cost[here, j])
        seq.append(nxt)
        left.remove(nxt)
        here = nxt
    return seq


def _two_opt(cost, start, seq, back):
    improved = True
    while improved:
        improved = False
        for i in range(len(seq) - 1):
            for j in range(i + 1, len(seq)):
                cand = seq[:i] + seq[i:j + 1][::-1] + seq[j + 1:]
                if _tour_cost(cost, start, cand, back) + 1e-6 < _tour_cost(cost, start, seq, back):
                    seq, improved = cand, True
    return seq


def plan(queue: pd.DataFrame, buildings: pd.DataFrame, start_building: str, mode: str = "walk",
         round_trip: bool = True, urgent_first: bool = True, end: str | None = None, team: str = "",
         accessible: bool = False) -> Plan | None:
    """Plan a round from start_building through every building in `queue` (ops.work_queue rows).
    mode: 'walk' (on foot) or 'van' (drive to each building's parking spot, walk in and out). In van mode, when
    the team's van has a location on the Rounds map, the round begins by walking to the van.
    end: a place in `buildings` to finish at instead (a one-way round). Each stop is the building's door on the
    Rounds map (an accessible door first when `accessible`), else its printer pin, else its middle."""
    from . import roundsmap

    def spot(name):
        return roundsmap.door_for(name, accessible) or roundsmap.printer_for(name) or (
            float(b.loc[name, "lat"]), float(b.loc[name, "lon"]))
    if network() is None:
        return None
    b = buildings.set_index("building")
    walk = graph("walk")
    on_campus = queue[queue["building"].map(b["campus"]).fillna("") == "Main"]
    skipped = sorted(set(queue["building"]) - set(on_campus["building"]))
    stops = []
    for name, rows in on_campus.groupby("building", sort=False):
        items = rows.to_dict("records")
        lat, lon = spot(name)
        stops.append(Stop(name, lat, lon, items, 0 if (rows["kind"] == "red").any() else 1,
                          sum(SERVICE_MIN.get(k, 6) for k in rows["kind"])))
    start = b.loc[start_building]
    start_pt = roundsmap.door_for(start_building, accessible) or (float(start["lat"]), float(start["lon"]))
    points = [start_pt] + [(s.lat, s.lon) for s in stops]
    finish = None
    if end and end in b.index and end != start_building:
        finish = len(points)
        points.append(roundsmap.door_for(end, accessible) or (float(b.loc[end, "lat"]), float(b.loc[end, "lon"])))
        round_trip = True                 # the "way back" now leads to the end point
    walk_nodes = [walk.nearest(*p) for p in points]

    if mode == "van":
        drive = graph("drive")
        park = parking_spots(pd.DataFrame({"building": [start_building] + [s.building for s in stops]
                                           + ([end] if finish else []),
                                           "lat": [p[0] for p in points], "lon": [p[1] for p in points]}),
                             team)
        park_pts = list(zip(park["park_lat"], park["park_lon"]))
        van = roundsmap.van_location(team) if team else None
        if van:                            # the van is where it was left, not at the start's parking space
            park_pts[0] = (van["lat"], van["lon"])
        drive_nodes = [drive.nearest(*p) for p in park_pts]
        park_walk = [walk.nearest(*p) for p in park_pts]
        runs = [dijkstra(drive, n) for n in drive_nodes]
        # Walking in and out of each building from its parking spot.
        walk_in = []
        for pw, wn in zip(park_walk, walk_nodes):
            d, prev = dijkstra(walk, pw)
            walk_in.append((d[wn], path_to(prev, wn)))
        cost = np.array([[0.0 if i == j else walk_in[i][0] + runs[i][0][drive_nodes[j]] + PARK_AND_LOCK_S + walk_in[j][0]
                          for j in range(len(points))] for i in range(len(points))])
    else:
        runs = [dijkstra(walk, n) for n in walk_nodes]
        cost = np.array([[runs[i][0][walk_nodes[j]] for j in range(len(points))] for i in range(len(points))])

    n_stop = len(stops) + 1
    order_cost = cost[:n_stop, :n_stop].copy()
    if finish:
        order_cost[:, 0] = cost[:n_stop, finish]      # "returning to the start" means walking to the end point
    seq = _order(order_cost, [s.priority for s in stops], round_trip, urgent_first)
    g_used = graph("drive") if mode == "van" else walk

    def legs_between(i: int, j: int, label: str) -> list[Leg]:
        """Legs from point i (0 = start) to point j: in van mode, walk out to the van, drive to
        j's parking spot and walk in; on foot, one walk."""
        if mode == "van":
            out = [_leg(walk, walk_in[i][1][::-1], "the van", "walk", walk_in[i][0]),
                   _leg(g_used, path_to(runs[i][1], drive_nodes[j]), label, "drive",
                        runs[i][0][drive_nodes[j]] + PARK_AND_LOCK_S),
                   _leg(walk, walk_in[j][1], label, "walk", walk_in[j][0])]
            if drive_nodes[i] == drive_nodes[j]:          # same parking spot: just walk between them
                d, prev = dijkstra(walk, walk_nodes[i])
                out = [_leg(walk, path_to(prev, walk_nodes[j]), label, "walk", d[walk_nodes[j]])]
        else:
            out = [_leg(walk, path_to(runs[i][1], walk_nodes[j]), label, "walk", runs[i][0][walk_nodes[j]])]
        return [l for l in out if l.meters > 1 or l.mode == "drive" and len(l.path) > 1]

    order, legs, here = [], [], 0
    for idx in seq:
        st = stops[idx - 1]
        order.append(st)
        legs.append(legs_between(here, idx, st.building) if walk_nodes[here] != walk_nodes[idx] else [])
        here = idx
    home, home_name = (finish, end) if finish else (0, start_building)
    back = (legs_between(here, home, home_name) if round_trip and order and walk_nodes[here] != walk_nodes[home]
            else [])
    return Plan(start_building, mode, order, legs, back, skipped, end=end if finish else None)


def _leg(g: Graph, nodes: list[int], to: str, mode: str, seconds: float) -> Leg:
    m = sum(g.meters.get((a, b), g.meters.get((b, a), 0.0)) for a, b in zip(nodes, nodes[1:]))
    return Leg(to, mode, m, float(seconds), [tuple(g.xy[n]) for n in nodes])


def supplies(p: Plan) -> list[str]:
    """What to bring, from the issues on the route."""
    paper = sum(1 for s in p.order for it in s.items
                if it["kind"] == "tray" or "out of paper" in str(it["issue"]).lower()
                or "paper out" in str(it["issue"]).lower())
    parts: dict[str, int] = {}
    for s in p.order:
        for it in s.items:
            if it["kind"] in ("consumable_now", "consumable_soon"):
                label = str(it["issue"]).split(" at ")[0]
                parts[label] = parts.get(label, 0) + 1
    out = []
    if paper:
        out.append(f"{paper * 2} reams of letter paper ({paper} tray{'s' if paper != 1 else ''} to fill)")
    out += [f"{n} × {k}" for k, n in sorted(parts.items())]
    return out
