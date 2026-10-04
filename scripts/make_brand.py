"""Draw the brand: a wireframe Boyden Hall, the compact app mark, and the background pattern.

    python scripts/make_brand.py

Writes into wepa_monitor/dashboard/assets/:
  boyden.svg        full facade, line art (black strokes; used as a CSS mask and recolored by theme)
  boyden-mark.svg   compact mark: tower, cupola, pediment and columns (the app logo; also a mask)
  pattern.svg       seamless 720 px tile: data mesh, chart glyphs, wireframe printers, paper, bears
  favicon.ico       the mark in BSU crimson, with gold and slate offset lines
and docs/product/assets/ (colored copies for the documentation cover).

Inspired by the wireframe-building idea in Virginia Tech's Institute for Advanced Computing identity
(thin architectural line art, repeated in offset colors so it "vibrates"), drawn from scratch for BSU's
Boyden Hall: the brick wings, the pedimented portico, the clock tower and its cupola.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "wepa_monitor" / "dashboard" / "assets"
DOC_ASSETS = ROOT / "docs" / "product" / "assets"
CRIMSON, GOLD, SLATE = "#8b1e24", "#c99a2e", "#4f7396"


# --- Boyden Hall, front elevation ----------------------------------------------------------------------

def _poly_d(pts, close=True):
    d = "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    return d + ("Z" if close else "")


class _Model:
    """A small 3D wireframe model of Boyden Hall, drawn in a three-quarter axonometric view.

    x runs along the facade (centre line x = 0), y runs back from the front of the main block, z is up.
    `strong` holds the massing (every edge of every solid, hidden ones included, as a wireframe does);
    `light` holds detail: windows, the floor grid, dome ribs and railings."""

    def __init__(self, yaw: float, pitch: float):
        self.c, self.s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        self.cp, self.sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
        self.strong: list[list[tuple]] = []
        self.light: list[list[tuple]] = []
        self.nodes: list[tuple] = []

    def p(self, x, y, z):
        depth = -x * self.s + y * self.c
        return (x * self.c + y * self.s, -z * self.cp - depth * self.sp)

    def line(self, *pts, light=False):
        (self.light if light else self.strong).append([self.p(*q) for q in pts])

    def ring(self, pts, light=False):
        self.line(*pts, pts[0], light=light)

    def box(self, x0, x1, y0, y1, z0, z1, light=False, node=False):
        for z in (z0, z1):
            self.ring([(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)], light)
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            self.line((x, y, z0), (x, y, z1), light=light)
        if node:
            self.nodes += [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]

    def prism(self, tri, y0, y1, light=False):
        """A triangular prism (pediment, gable) from an xz triangle, extruded from y0 to y1."""
        for y in (y0, y1):
            self.ring([(x, y, z) for x, z in tri], light)
        for x, z in tri:
            self.line((x, y0, z), (x, y1, z), light=light)

    def hip(self, x0, x1, y0, y1, z0, z1, inset):
        ym = (y0 + y1) / 2
        a, b = (x0 + inset, ym, z1), (x1 - inset, ym, z1)
        self.line(a, b)
        for corner, end in (((x0, y0), a), ((x0, y1), a), ((x1, y0), b), ((x1, y1), b)):
            self.line((*corner, z0), end)
        self.nodes += [a, b]

    def octo(self, cx, cy, r, z, n=8):
        return [(cx + r * math.cos(2 * math.pi * (i + 0.5) / n), cy + r * math.sin(2 * math.pi * (i + 0.5) / n), z)
                for i in range(n)]

    def dome(self, cx, cy, r, z0, h, rings=3, n=8):
        layers = [self.octo(cx, cy, r * math.cos(math.pi / 2 * i / rings), z0 + h * math.sin(math.pi / 2 * i / rings), n)
                  for i in range(rings)]
        top = (cx, cy, z0 + h)
        for i, ring in enumerate(layers):
            self.ring(ring, light=i > 0)
        for j in range(n):
            self.line(*[layer[j] for layer in layers], top, light=True)
        for a, b in zip(layers, layers[1:]):           # one diagonal per panel: a geodesic look
            for j in range(n):
                self.line(a[j], b[(j + 1) % n], light=True)

    def window(self, x, y, z0, z1, w):
        self.ring([(x, y, z0), (x + w, y, z0), (x + w, y, z1), (x, y, z1)], light=True)
        self.line((x + w / 2, y, z0), (x + w / 2, y, z1), light=True)
        self.line((x, y, (z0 + z1) / 2), (x + w, y, (z0 + z1) / 2), light=True)

    def arch(self, x, y, z0, z1, w):
        self.line((x, y, z0), (x, y, z1 - w * 0.6), (x + w / 2, y, z1), (x + w, y, z1 - w * 0.6), (x + w, y, z0),
                  light=True)

    def grid(self, x0, x1, y0, y1, step):
        x = x0
        while x <= x1 + 0.1:
            self.line((x, y0, 0), (x, y1, 0), light=True)
            x += step
        y = y0
        while y <= y1 + 0.1:
            self.line((x0, y, 0), (x1, y, 0), light=True)
            y += step


def _boyden_model(full: bool) -> _Model:
    m = _Model(yaw=24 if full else 30, pitch=22 if full else 24)
    m.grid(-300, 300, -120, 120, 40) if full else m.grid(-90, 90, -80, 100, 45)
    # Portico: steps, six columns, entablature and the pediment prism.
    m.box(-68, 68, -46, 0, 0, 3)
    if full:
        m.box(-62, 62, -40, 0, 3, 6, light=True)
    for x in (-55, -33, -11, 11, 33, 55):
        m.line((x, -34, 6), (x, -34, 78))
        if full:
            m.box(x - 3, x + 3, -37, -31, 74, 78, light=True)
    m.box(-62, 62, -40, 0, 78, 86, node=full)
    m.prism([(-64, 86), (0, 114), (64, 86)], -40, 0)
    m.ring([(-52, -40, 89), (0, -40, 109), (52, -40, 89)], light=True)
    m.nodes += [(0, -40, 114)]
    # Main block behind the portico.
    m.box(-70, 70, 0, 60, 0, 104, node=full)
    if full:
        m.arch(-7, -34, 6, 46, 14)
    # Tower: brick base with clock, balustrade, belfry with arched openings, cornice, faceted dome, lantern.
    m.box(-22, 22, 8, 52, 104, 148, node=True)
    clock = [(11 * math.cos(2 * math.pi * (i + .5) / 8), 8, 126 + 11 * math.sin(2 * math.pi * (i + .5) / 8))
             for i in range(8)]
    m.ring(clock)
    m.line((0, 8, 126), (0, 8, 134), light=True)
    m.line((0, 8, 126), (6, 8, 122), light=True)
    m.box(-30, 30, 0, 60, 148, 154)
    if full:
        for x in range(-30, 31, 10):
            m.line((x, 0, 154), (x, 0, 161), light=True)
        m.ring([(-30, 0, 161), (30, 0, 161), (30, 60, 161), (-30, 60, 161)], light=True)
    m.box(-15, 15, 15, 45, 154, 184)
    for x in ((-12, -1.5), (1.5, 12)):
        m.arch(x[0], 15, 157, 180, x[1] - x[0])
    m.box(-18, 18, 12, 48, 184, 189)
    m.dome(0, 30, 17, 189, 21, rings=3 if full else 2)
    for z in (210, 217):
        m.ring(m.octo(0, 30, 4.5, z))
    m.line((0, 30, 217), (0, 30, 236))
    m.ring([(0, 30, 224), (3, 30, 227), (0, 30, 230), (-3, 30, 227)])
    m.nodes += [(0, 30, 236)]
    if full:
        # Wings, hipped roofs, chimneys and three rows of windows on each front.
        for x0, x1 in ((-250, -70), (70, 250)):
            m.box(x0, x1, 4, 56, 0, 92, node=True)
            m.hip(x0, x1, 4, 56, 92, 114, 18)
            for cx in ((x0 + 50, x1 - 50)):
                m.box(cx - 5, cx + 5, 24, 34, 100, 124, light=True)
            for z0, z1 in ((12, 30), (40, 58), (66, 84)):
                for x in range(x0 + 10, x1 - 12, 20):
                    m.window(x, 4, z0, z1, 11)
        for z0, z1 in ((18, 40), (52, 72)):
            for x in (-50, 39):
                m.window(x, -0.1, z0, z1, 11)
    return m


def boyden_paths(full: bool = True) -> tuple[list[str], list[str], str]:
    """(massing, detail, viewBox) for the wireframe model, scaled so the drawing is about 1200 units wide."""
    m = _boyden_model(full)
    pts = [q for line in m.strong + m.light for q in line]
    x0, x1 = min(q[0] for q in pts), max(q[0] for q in pts)
    y0, y1 = min(q[1] for q in pts), max(q[1] for q in pts)
    k = (1200 if full else 320) / (x1 - x0)
    f = lambda q: ((q[0] - x0) * k, (q[1] - y0) * k)  # noqa: E731
    pad = 14 if full else 10
    h = 4.5 if full else 6
    marks = []
    for q in m.nodes:
        x, y = f(m.p(*q))
        marks.append(_poly_d([(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)]))
    strong = [_poly_d([f(q) for q in line], False) for line in m.strong] + marks
    light = [_poly_d([f(q) for q in line], False) for line in m.light]
    vb = f"{-pad} {-pad} {(x1 - x0) * k + 2 * pad:.0f} {(y1 - y0) * k + 2 * pad:.0f}"
    return strong, light, vb


def boyden_svg(full: bool, colors: tuple[str, ...] | None = None, stroke: float | None = None,
               bg: str | None = None) -> str:
    strong, light, vb = boyden_paths(full)
    sw = stroke or (2.2 if full else 6)
    def layer(color, dx=0.0, dy=0.0, op=1.0):
        t = f' transform="translate({dx} {dy})"' if dx or dy else ""
        return (f'<g fill="none" stroke="{color}" stroke-width="{sw}" stroke-linejoin="round" '
                f'stroke-linecap="round" opacity="{op}"{t}><path d="{" ".join(strong)}"/>'
                f'<path d="{" ".join(light)}" stroke-width="{sw * 0.5}" opacity=".8"/></g>')
    if colors is None:
        body = layer("#000")
    else:
        off = 3.2 if full else 5
        body = layer(colors[2], -off, off * 0.6, .75) + layer(colors[1], off, -off * 0.4, .85) + layer(colors[0])
    x, y, w, h = vb.split()
    rect = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{bg}"/>' if bg else ""
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">{rect}{body}</svg>'


# --- background pattern -----------------------------------------------------------------------------------

T = 720
R = random.Random(20)


def _poly(pts, close=True):
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    return d + (" Z" if close else "")


def _xf(cx, cy, s, rot):
    c, si = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    return lambda x, y: (cx + s * (x * c - y * si), cy + s * (x * si + y * c))


def glyph(kind: str, cx: float, cy: float, s: float, rot: float) -> list[str]:
    f = _xf(cx, cy, s, rot)
    P = lambda pts, close=True: _poly([f(*q) for q in pts], close)  # noqa: E731
    if kind == "printer":                                   # body, paper in, paper out, panel light
        return [P([(-20, -6), (20, -6), (20, 14), (-20, 14)]), P([(-12, -6), (-12, -20), (12, -20), (12, -6)], False),
                P([(-13, 14), (-13, 26), (13, 26), (13, 14)], False), P([(-8, 19), (8, 19)], False),
                P([(12, -1), (15, -1)], False), P([(-20, 6), (20, 6)], False)]
    if kind == "bars":
        out = [P([(-22, 18), (22, 18)], False)]
        for i, h in enumerate((14, 26, 10, 32)):
            x = -18 + i * 10
            out.append(P([(x, 18), (x, 18 - h), (x + 7, 18 - h), (x + 7, 18)], False))
        return out
    if kind == "spark":
        pts = [(-24, 8), (-14, -2), (-6, 4), (4, -12), (12, -4), (24, -16)]
        return [P(pts, False), P([(-24, 16), (24, 16)], False)] + [P([(x - 1.6, y), (x, y - 1.6), (x + 1.6, y), (x, y + 1.6)]) for x, y in pts[1::2]]
    if kind == "donut":
        ring = [(math.cos(a) * 16, math.sin(a) * 16) for a in [i * math.pi / 9 for i in range(18)]]
        inner = [(math.cos(a) * 9, math.sin(a) * 9) for a in [i * math.pi / 6 for i in range(12)]]
        return [P(ring), P(inner), P([(0, -16), (0, -9)], False), P([(11.3, 11.3), (6.4, 6.4)], False)]
    if kind == "paper":
        return [P([(-12, -16), (6, -16), (12, -10), (12, 16), (-12, 16)]), P([(6, -16), (6, -10), (12, -10)], False),
                P([(-7, -6), (7, -6)], False), P([(-7, 0), (7, 0)], False), P([(-7, 6), (3, 6)], False)]
    if kind == "scatter":
        return [P([(x - 1.8, y), (x, y - 1.8), (x + 1.8, y), (x, y + 1.8)]) for x, y in
                [(-18, 10), (-10, 4), (-4, 8), (2, -2), (8, 0), (14, -10), (18, -6), (-12, -8)]] + \
               [P([(-22, 14), (22, -14)], False)]
    if kind == "bear":                                     # low-poly bear head: outline, ears, facets, snout
        out = [(-16, -8), (-10, -16), (0, -18), (10, -16), (16, -8), (18, 4), (10, 14), (0, 18), (-10, 14), (-18, 4)]
        return [P(out), P([(-16, -8), (-20, -18), (-10, -16)]), P([(16, -8), (20, -18), (10, -16)]),
                P([(-6, 6), (0, 2), (6, 6), (0, 12)]), P([(-10, -16), (-6, 6)], False), P([(10, -16), (6, 6)], False),
                P([(0, -18), (0, 2)], False), P([(-18, 4), (-6, 6)], False), P([(18, 4), (6, 6)], False),
                P([(-7, -5), (-5, -5)], False), P([(5, -5), (7, -5)], False)]
    if kind == "boyden":
        return [P([(-18, 18), (18, 18)], False), P([(-16, 4), (0, -6), (16, 4)]), P([(-6, -6), (-6, -18), (6, -18), (6, -6)], False),
                P([(-4, -18), (0, -26), (4, -18)], False)] + [P([(x, 4), (x, 18)], False) for x in (-12, -4, 4, 12)]
    raise ValueError(kind)


def pattern_svg() -> str:
    n = 6
    cell = T / n
    pts = {(i, j): ((i + 0.5) * cell + R.uniform(-28, 28), (j + 0.5) * cell + R.uniform(-28, 28))
           for i in range(n) for j in range(n)}
    lines = []

    def pt(i, j):
        x, y = pts[(i % n, j % n)]
        return x + (i // n) * T + (T if i < 0 else 0) * 0, y + (j // n) * T

    for i in range(n):
        for j in range(n):
            for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
                if R.random() < 0.42:
                    (x0, y0), (x1, y1) = pt(i, j), pt(i + di, j + dj)
                    lines.append((x0, y0, x1, y1))
    paths = []
    for x0, y0, x1, y1 in lines:                       # draw each edge in every tile copy it touches (seamless)
        for ox in (-T, 0):
            for oy in (-T, 0, T):
                paths.append(f"M{x0 + ox + (T if x1 > T and ox < 0 else 0):.1f} {y0 + oy:.1f}L{x1 + ox + (T if x1 > T and ox < 0 else 0):.1f} {y1 + oy:.1f}")
    paths = sorted(set(paths))
    nodes = [f"M{x - 2:.1f} {y:.1f}a2 2 0 1 0 4 0a2 2 0 1 0 -4 0" for (x, y) in pts.values() if R.random() < 0.55]
    kinds = ["printer", "bars", "spark", "bear", "paper", "donut", "printer", "scatter", "bear", "bars", "boyden", "spark"]
    spots = [(1, 0), (3, 1), (5, 0), (0, 2), (2, 3), (4, 2), (1, 4), (3, 5), (5, 4), (0, 5), (4, 4), (2, 1)]
    glyphs = []
    for kind, (i, j) in zip(kinds, spots):
        cx, cy = (i + 0.5) * cell + R.uniform(-10, 10), (j + 0.5) * cell + R.uniform(-10, 10)
        size = 1.15 if kind in ("bear", "printer", "boyden") else 1.0
        glyphs += glyph(kind, cx, cy, size, R.choice((0, 0, -8, 8, 14, -14)))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{T}" height="{T}" viewBox="0 0 {T} {T}">'
            f'<g fill="none" stroke="#000" stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="{" ".join(paths)}" stroke-width="1" opacity=".6"/>'
            f'<path d="{" ".join(nodes)}" stroke-width="1.3"/>'
            f'<path d="{" ".join(glyphs)}" stroke-width="1.6"/></g></svg>')


# --- patterns for the special themes ---------------------------------------------------------------------

def _seamless(body: str, size: int, stroke: float, extra: str = "") -> str:
    """Wrap tile content so anything crossing an edge reappears on the opposite side."""
    copies = "".join(f'<use href="#t" x="{dx}" y="{dy}"/>' for dx in (-size, 0, size) for dy in (-size, 0, size))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
            f'<defs><g id="t" fill="none" stroke="#000" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round"{extra}>{body}</g></defs>{copies}</svg>')


def _shape(kind: str, cx: float, cy: float, s: float, rot: float) -> list[str]:
    f = _xf(cx, cy, s, rot)
    P = lambda pts, close=True: _poly([f(*q) for q in pts], close)  # noqa: E731
    def circle(x, y, r, n=20):
        return P([(x + r * math.cos(2 * math.pi * i / n), y + r * math.sin(2 * math.pi * i / n)) for i in range(n)])
    if kind == "star":                                     # four-point sparkle
        return [P([(0, -14), (3, -3), (14, 0), (3, 3), (0, 14), (-3, 3), (-14, 0), (-3, -3)])]
    if kind == "dot":
        return [circle(0, 0, 2, 8)]
    if kind == "pin":                                      # bowling pin, two stripes
        side = [(0, -26), (5, -24), (6, -18), (4, -12), (4, -9), (8, 0), (10, 10), (8, 20), (5, 26)]
        outline = side + [(-x, y) for x, y in reversed(side)]
        return [P(outline), P([(-4, -12), (4, -12)], False), P([(-4, -9), (4, -9)], False)]
    if kind == "ball":
        return [circle(0, 0, 16), circle(-4, -6, 2.4, 8), circle(4, -6, 2.4, 8), circle(0, 1, 2.4, 8)]
    if kind == "planet":
        ring = [(math.cos(a) * 26, math.sin(a) * 7) for a in [i * math.pi / 12 for i in range(24)]]
        return [circle(0, 0, 11), P(ring)]
    if kind == "zigzag":
        return [P([(-24, 0), (-14, -8), (-4, 8), (6, -8), (16, 8), (24, 0)], False)]
    if kind == "paw":                                      # a generic paw print: pad and four toes
        pad = [(-10, 6), (-6, -2), (0, -4), (6, -2), (10, 6), (6, 12), (0, 11), (-6, 12)]
        return [P(pad), circle(-12, -10, 4, 10), circle(-4, -16, 4, 10), circle(5, -16, 4, 10), circle(13, -10, 4, 10)]
    if kind == "football":
        body = [(math.cos(a) * 20, math.sin(a) * 11 * (1 - 0.15 * math.cos(a) ** 2)) for a in [i * math.pi / 12 for i in range(24)]]
        return [P(body), P([(-8, 0), (8, 0)], False)] + [P([(x, -3), (x, 3)], False) for x in (-5, -1.7, 1.7, 5)]
    if kind == "goalpost":
        return [P([(0, 24), (0, 4)], False), P([(-16, 4), (16, 4)], False), P([(-16, 4), (-16, -24)], False),
                P([(16, 4), (16, -24)], False)]
    if kind == "pennant":
        return [P([(-18, -16), (-18, 20)], False), P([(-18, -16), (20, -8), (-18, 0)]), P([(-12, -9), (6, -8)], False)]
    if kind == "megaphone":
        return [P([(-16, -4), (8, -14), (8, 14), (-16, 4)]), P([(-22, -4), (-16, -4), (-16, 4), (-22, 4)]),
                P([(-12, 4), (-10, 14), (-5, 14), (-6, 3)], False)]
    return glyph(kind, cx, cy, s, rot)


def _scatter(kinds: list[str], size: int, seed: int, grid: int) -> list[str]:
    rnd = random.Random(seed)
    cell = size / grid
    out = []
    for i in range(grid):
        for j in range(grid):
            kind = kinds[(i * 3 + j * 5 + rnd.randrange(len(kinds))) % len(kinds)]
            x, y = (i + 0.5) * cell + rnd.uniform(-cell * .25, cell * .25), (j + 0.5) * cell + rnd.uniform(-cell * .25, cell * .25)
            out += _shape(kind, x, y, rnd.uniform(0.85, 1.15), rnd.choice((0, -15, 15, 30, -30)))
    return out


def cosmic_svg() -> str:
    """Cosmic bowling: sparkles, pins, balls, planets and zigzags."""
    size = 640
    shapes = _scatter(["star", "pin", "dot", "ball", "star", "planet", "zigzag", "dot", "star"], size, 7, 7)
    return _seamless(f'<path d="{" ".join(shapes)}"/>', size, 1.8)


def spirit_svg() -> str:
    """Game day: paw prints, footballs, goalposts, pennants, megaphones, bears and a few Boydens."""
    size = 640
    shapes = _scatter(["paw", "football", "paw", "goalpost", "pennant", "bear", "paw", "megaphone", "boyden"], size, 11, 6)
    return _seamless(f'<path d="{" ".join(shapes)}"/>', size, 1.8)


def _wave(x0, y0, length, amp, period, steps=60, phase=0.0):
    return [(x0 + length * i / steps, y0 + amp * math.sin(phase + 2 * math.pi * (length * i / steps) / period))
            for i in range(steps + 1)]


def cup_svgs() -> tuple[str, str]:
    """The 90s paper cup, as two masks: a broad teal brush stroke (a) and a thin purple squiggle (b)."""
    size = 600
    rnd = random.Random(1994)
    brush = []
    for y, phase in ((150, 0.0), (450, 2.1)):
        main = _wave(-40, y, size + 80, 22, 380, 70, phase)
        brush.append(f'<path d="{_poly(main, False)}" stroke-width="30"/>')
        for _ in range(5):                                 # dry-brush bristle strokes along the edges
            off = rnd.choice((-1, 1)) * rnd.uniform(17, 25)
            start, end = rnd.randrange(0, 30), rnd.randrange(40, 71)
            pts = [(x, yy + off) for x, yy in main[start:end]]
            brush.append(f'<path d="{_poly(pts, False)}" stroke-width="{rnd.uniform(2, 4.5):.1f}"/>')
    squiggle = []
    for y, phase in ((60, 1.0), (300, 3.0), (540, 0.4)):
        pts = []
        for i in range(161):
            t = i / 160
            x = -30 + (size + 60) * t + 14 * math.cos(t * 2 * math.pi * 9 + phase)
            yy = y + 26 * math.sin(t * 2 * math.pi * 2 + phase) + 12 * math.sin(t * 2 * math.pi * 9 + phase)
            pts.append((x, yy))
        squiggle.append(_poly(pts, False))
    a = _seamless("".join(brush), size, 30)
    b = _seamless(f'<path d="{" ".join(squiggle)}"/>', size, 5)
    return a, b


def favicon() -> None:
    """Render the colored mark to PNG with Chromium, then pack an .ico (16-64 px)."""
    import io

    from PIL import Image
    from playwright.sync_api import sync_playwright
    svg = boyden_svg(False, (CRIMSON, GOLD, SLATE), stroke=12, bg="#ffffff")
    exe = "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = br.new_page(viewport={"width": 256, "height": 256})
        sized = svg.replace("<svg ", '<svg width="200" height="230" ')
        pg.set_content("<body style='margin:0;background:#fff'><div style='width:256px;height:256px;display:grid;"
                       f"place-items:center'>{sized}</div></body>")
        png = pg.screenshot(clip={"x": 0, "y": 0, "width": 256, "height": 256})
        br.close()
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    im.save(ASSETS / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    im.save(ASSETS / "boyden-mark.png")


def main() -> None:
    DOC_ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "boyden.svg").write_text(boyden_svg(True))
    (ASSETS / "boyden-mark.svg").write_text(boyden_svg(False))
    (ASSETS / "pattern.svg").write_text(pattern_svg())
    (ASSETS / "pattern-cosmic.svg").write_text(cosmic_svg())
    (ASSETS / "pattern-spirit.svg").write_text(spirit_svg())
    cup_a, cup_b = cup_svgs()
    (ASSETS / "pattern-cup-a.svg").write_text(cup_a)
    (ASSETS / "pattern-cup-b.svg").write_text(cup_b)
    (DOC_ASSETS / "boyden-cover.svg").write_text(boyden_svg(True, ("#ffffff", "#f3c25b", "#8fc1e8"), stroke=2.4))
    (DOC_ASSETS / "boyden-mark-color.svg").write_text(boyden_svg(False, (CRIMSON, GOLD, SLATE)))
    favicon()
    for f in ("boyden.svg", "boyden-mark.svg", "pattern.svg", "favicon.ico"):
        print(f"wrote {ASSETS / f} ({(ASSETS / f).stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
