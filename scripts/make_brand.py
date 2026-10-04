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

def _rect(x0, y0, x1, y1):
    return f"M{x0} {y0}H{x1}V{y1}H{x0}Z"


def boyden_paths(full: bool = True) -> tuple[list[str], list[str]]:
    """(architecture, facet lines). Coordinates on a 1200 x 560 canvas, centre line x = 600."""
    p: list[str] = []
    cx = 600
    ground = 520
    # Cupola: finial with weathervane, lantern, dome, belfry with arched openings, cornice.
    p += [f"M{cx} 22V48", f"M{cx - 9} 30H{cx + 9}M{cx + 9} 30L{cx + 3} 26", "M596 36a4 4 0 1 0 8 0a4 4 0 1 0 -8 0",
          _rect(593, 48, 607, 60), "M570 90Q570 54 600 52Q630 54 630 90", _rect(564, 90, 636, 98),
          _rect(572, 98, 628, 156)]
    for x in (580, 600, 620):                                      # columns between the arches
        p.append(f"M{x} 98V156")
    for x0 in (580, 600):                                          # two arched openings
        p.append(f"M{x0 + 4} 150V118Q{x0 + 10} 106 {x0 + 16} 118V150")
    # Balustrade platform with corner urns.
    p += [_rect(538, 156, 662, 168), "M538 146H662"]
    for x in range(544, 660, 8):
        p.append(f"M{x} 146V156")
    for x in (538, 662):
        p.append(f"M{x} 146V132M{x - 4} 140H{x + 4}")
    # Brick tower base with its oculus.
    p += [_rect(552, 168, 648, 252), "M588 214a12 12 0 1 0 24 0a12 12 0 1 0 -24 0", "M600 202V226M588 214H612"]
    # Pediment, tympanum and seal.
    p += ["M474 320L600 248L726 320Z", "M494 312L600 258L706 312Z", "M590 296a10 10 0 1 0 20 0a10 10 0 1 0 -20 0"]
    # Entablature, six columns, door, steps.
    p += [_rect(478, 320, 722, 334)]
    for x in (492, 532, 572, 616, 656, 696):
        p += [_rect(x, 334, x + 12, 494), f"M{x - 3} 334H{x + 15}M{x - 3} 494H{x + 15}"]
    p += ["M586 494V448Q600 430 614 448V494"]
    p += [_rect(470, 494, 730, 502), _rect(462, 502, 738, 510), _rect(454, 510, 746, ground)]
    if not full:
        # The compact mark is shown as small as 30 px: keep only what reads at that size.
        p = [f"M{cx} 22V48", _rect(590, 48, 610, 60), "M568 92Q568 52 600 50Q632 52 632 92", _rect(560, 92, 640, 100),
             _rect(570, 100, 630, 154), "M582 148V122Q590 108 598 122V148", "M602 148V122Q610 108 618 122V148",
             _rect(536, 154, 664, 168), _rect(552, 168, 648, 252), "M584 210a16 16 0 1 0 32 0a16 16 0 1 0 -32 0",
             "M466 324L600 246L734 324Z", _rect(474, 324, 726, 338)]
        for x in (496, 542, 588, 612, 658, 704):
            p.append(f"M{x} 338V494")
        p += [_rect(462, 494, 738, 508), _rect(448, 508, 752, ground)]
        return p, ["M600 22L448 520", "M600 22L752 520"]
    if full:
        # Central block behind the portico, then the two wings with hipped roofs and chimneys.
        p += [_rect(464, 286, 736, ground)]
        for x0, x1 in ((80, 464), (736, 1120)):
            p += [_rect(x0, 300, x1, ground), f"M{x0 - 8} 300H{x1 + 8}"]
            p.append(f"M{x0} 300L{x0 + 34} 262H{x1 - 34}L{x1} 300")
            for y0, y1 in ((318, 352), (380, 414), (446, 484)):
                for x in range(x0 + 18, x1 - 24, 40):
                    p += [_rect(x, y0, x + 22, y1), f"M{x + 11} {y0}V{y1}M{x} {(y0 + y1) // 2}H{x + 22}"]
        for x in (196, 330, 870, 1004):                            # chimneys
            p.append(_rect(x, 238, x + 24, 266))
    facets = (["M600 22L80 300", "M600 22L1120 300", "M80 520L600 248", "M1120 520L600 248",
               "M196 238L552 168", "M1028 238L648 168"] if full else
              ["M600 22L454 520", "M600 22L746 520", "M474 320L746 520", "M726 320L454 520"])
    return p, facets


def boyden_svg(full: bool, colors: tuple[str, ...] | None = None, stroke: float | None = None,
               bg: str | None = None) -> str:
    arch, facets = boyden_paths(full)
    vb = "40 0 1120 540" if full else "440 10 320 520"
    sw = stroke or (2.2 if full else 9)
    def layer(color, dx=0.0, dy=0.0, op=1.0):
        t = f' transform="translate({dx} {dy})"' if dx or dy else ""
        return (f'<g fill="none" stroke="{color}" stroke-width="{sw}" stroke-linejoin="round" '
                f'stroke-linecap="round" opacity="{op}"{t}><path d="{" ".join(arch)}"/>'
                f'<path d="{" ".join(facets)}" stroke-width="{sw * 0.55}" opacity=".7"/></g>')
    if colors is None:
        body = layer("#000")
    else:
        off = 3.2 if full else 7
        body = layer(colors[2], -off, off * 0.6, .75) + layer(colors[1], off, -off * 0.4, .85) + layer(colors[0])
    rect = f'<rect x="{vb.split()[0]}" y="{vb.split()[1]}" width="100%" height="100%" fill="{bg}"/>' if bg else ""
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
    (DOC_ASSETS / "boyden-cover.svg").write_text(boyden_svg(True, ("#ffffff", "#f3c25b", "#8fc1e8"), stroke=2.4))
    (DOC_ASSETS / "boyden-mark-color.svg").write_text(boyden_svg(False, (CRIMSON, GOLD, SLATE)))
    favicon()
    for f in ("boyden.svg", "boyden-mark.svg", "pattern.svg", "favicon.ico"):
        print(f"wrote {ASSETS / f} ({(ASSETS / f).stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
