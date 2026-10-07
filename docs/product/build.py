"""Build the product documentation: assemble src/*.html, draw charts from the app's real settings, embed
screenshots, and write a single self-contained page.

Every figure is drawn at build time and embedded as SVG with its own light background (diagrams are rendered
once with Mermaid in Chromium), so the page needs no scripts and every figure reads the same in light mode,
dark mode, print and viewers that drop page backgrounds.

    python docs/product/build.py            -> docs/product/BSU-Student-Printing-Ops-Documentation.html
    python docs/product/build.py --pdf      -> also docs/product/BSU-Student-Printing-Ops-Documentation.pdf

The PDF is printed with Chromium (Playwright); diagrams are drawn by Mermaid in the browser.
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from wepa_monitor import __version__, config  # noqa: E402

OUT = HERE / "BSU-Student-Printing-Ops-Documentation.html"
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
SHOTS = {"overview": "overview-bsu.png", "station": "station-detail.png", "investigation": "investigation.png",
         "assistant": "assistant.png", "inventory": "inventory.png", "outcomes": "outcomes.png"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CRIMSON, GOLD, BOTH, NONE, INK, MUTED = "#8b1e24", "#d6a84a", "#5a2b1a", "#efe7dc", "#1f1a17", "#6b625c"
PAPER = "#fffdf9"            # every figure carries this background itself


def desk_hours_svg() -> str:
    """7 x 24 grid of the week: which support desk is staffed each hour (from config.SUPPORT_TEAMS)."""
    teams = config.SUPPORT_TEAMS
    cell, gap, left, top = 26, 2, 46, 30
    w, h = left + 24 * (cell + gap) + 10, top + 7 * (cell + gap) + 64
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Weekly grid of staffed support desk hours">'
             f'<rect width="{w}" height="{h}" fill="{PAPER}"/>']
    for hr in range(0, 24, 3):
        x = left + hr * (cell + gap)
        label = f"{(hr % 12) or 12}{'a' if hr < 12 else 'p'}"
        parts.append(f'<text x="{x}" y="{top - 9}" font-size="11" fill="{MUTED}" font-family="IBM Plex Mono, monospace">{label}</text>')
    staffed = {t: 0 for t in teams}
    for d in range(7):
        y = top + d * (cell + gap)
        parts.append(f'<text x="0" y="{y + cell * 0.68}" font-size="12" fill="{INK}" font-family="Libre Franklin, Arial, sans-serif" font-weight="600">{DAYS[d]}</text>')
        for hr in range(24):
            on = [t for t, cfg in teams.items() if d in cfg["hours"] and cfg["hours"][d][0] <= hr < cfg["hours"][d][1]]
            for t in on:
                staffed[t] += 1
            fill = BOTH if len(on) > 1 else CRIMSON if on == ["ResNet"] else GOLD if on else NONE
            parts.append(f'<rect x="{left + hr * (cell + gap)}" y="{y}" width="{cell}" height="{cell}" rx="3" fill="{fill}"/>')
    ly = top + 7 * (cell + gap) + 18
    legend = [(CRIMSON, "ResNet only"), (GOLD, "IT Service Center only"), (BOTH, "Both desks"), (NONE, "No desk staffed")]
    x = left
    for color, text in legend:
        parts.append(f'<rect x="{x}" y="{ly}" width="14" height="14" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{x + 20}" y="{ly + 11.5}" font-size="12" fill="{INK}" font-family="Libre Franklin, Arial, sans-serif">{text}</text>')
        x += 30 + len(text) * 6.6
    total = 7 * 24
    summary = " · ".join(f"{t}: {n} of {total} hours ({n / total:.0%})" for t, n in staffed.items())
    parts.append(f'<text x="{left}" y="{ly + 38}" font-size="12" fill="{MUTED}" font-family="IBM Plex Mono, monospace">{summary}</text>')
    parts.append("</svg>")
    return "".join(parts)


def data_quality_svg() -> str:
    parts_ = [("Freshness", 30, "#8b1e24"), ("Completeness", 40, "#b8892b"), ("Validity", 20, "#4f6f8f"),
              ("Station coverage", 10, "#2f7d4f")]
    w, x0, bar_w, y = 800, 10, 740, 20
    out = [f'<svg viewBox="0 0 {w} 118" role="img" aria-label="Data quality score weights: freshness 30, completeness 40, validity 20, station coverage 10">'
           f'<rect width="{w}" height="118" fill="{PAPER}"/>']
    x = x0
    for name, pts, color in parts_:
        seg = bar_w * pts / 100
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{seg - 3:.1f}" height="34" rx="4" fill="{color}"/>')
        out.append(f'<text x="{x + 10:.1f}" y="{y + 22}" font-size="14" font-weight="700" fill="#ffffff" font-family="Libre Franklin, Arial, sans-serif">{pts}</text>')
        out.append(f'<text x="{x:.1f}" y="{y + 56}" font-size="13" fill="{INK}" font-family="Libre Franklin, Arial, sans-serif" font-weight="600">{name}</text>')
        x += seg
    out.append(f'<text x="{x0}" y="{y + 84}" font-size="12" fill="{MUTED}" font-family="IBM Plex Mono, monospace">score = 100 × (0.3 freshness + 0.4 completeness + 0.2 validity + 0.1 coverage)</text>')
    out.append("</svg>")
    return "".join(out)


def shot(name: str) -> str:
    from PIL import Image
    path = ROOT / "docs" / "screenshots" / SHOTS[name]
    im = Image.open(path).convert("RGB")
    if im.height > im.width:          # full-page captures: the top of the page, so the figure stays legible
        im = im.crop((0, 0, im.width, int(im.width * 0.9)))
    im.thumbnail((1100, 1100))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72, optimize=True, progressive=True)
    data = base64.b64encode(buf.getvalue()).decode()
    alt = {"overview": "Overview page", "station": "Station page", "investigation": "Investigation page",
           "assistant": "Assistant pane", "inventory": "Inventory page", "outcomes": "IT Outcomes page"}[name]
    return f'<img src="data:image/jpeg;base64,{data}" alt="{alt} (demo data)">'


def build() -> Path:
    html = "".join(p.read_text(encoding="utf-8") for p in sorted((HERE / "src").glob("*.html")))
    html = html.replace("{{SVG_DESKS}}", desk_hours_svg()).replace("{{SVG_DQ}}", data_quality_svg())
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_brand
    pattern = base64.b64encode(make_brand.pattern_svg().encode()).decode()
    html = (html.replace("{{PATTERN_URI}}", "data:image/svg+xml;base64," + pattern)
            .replace("{{BOYDEN_COVER}}", make_brand.boyden_svg(True, ("#ffffff", "#f3c25b", "#8fc1e8"), stroke=2.2))
            .replace("{{MARK_COVER}}", make_brand.boyden_svg(False, ("#ffffff", "#f3c25b", "#f19aa0"))))
    for name in SHOTS:
        html = html.replace("{{IMG_" + name + "}}", shot(name))
    assert "{{" not in html, "unfilled placeholder"
    html = draw_diagrams(html)
    OUT.write_text(html, encoding="utf-8")
    return OUT


DRAW_JS = """
async () => {
  const FONT = 'Arial, Helvetica, sans-serif';
  await document.fonts.ready;
  // One font everyone has, so labels are measured and drawn in the same face (no clipped words).
  mermaid.initialize({startOnLoad: false, securityLevel: 'strict', fontFamily: FONT,
                      themeVariables: {fontFamily: FONT}, flowchart: {useMaxWidth: false},
                      sequence: {useMaxWidth: false}, er: {useMaxWidth: false}, state: {useMaxWidth: false},
                      // Journey steps: boxes wide and tall enough for three lines, so no step runs out of its box.
                      journey: {useMaxWidth: false, width: 190, height: 70, taskMargin: 40},
                      timeline: {useMaxWidth: false}});
  const out = [];
  const blocks = [...document.querySelectorAll('pre.mermaid')];
  for (let i = 0; i < blocks.length; i++) {
    const {svg} = await mermaid.render('fig' + i, blocks[i].textContent);
    const host = document.createElement('div');
    host.innerHTML = svg;
    document.body.appendChild(host);
    const el = host.querySelector('svg');
    // Grow the frame to everything drawn (self-loops and long labels can reach past Mermaid's own frame).
    const box = el.getBBox(), pad = 16, v0 = el.viewBox.baseVal;
    const x0 = Math.min(v0.x, box.x) - pad, y0 = Math.min(v0.y, box.y) - pad;
    const x1 = Math.max(v0.x + v0.width, box.x + box.width) + pad, y1 = Math.max(v0.y + v0.height, box.y + box.height) + pad;
    el.setAttribute('viewBox', `${x0} ${y0} ${x1 - x0} ${y1 - y0}`);
    const vb = el.viewBox.baseVal;
    const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    for (const [k, v] of [['x', vb.x], ['y', vb.y], ['width', vb.width], ['height', vb.height], ['fill', '%s']]) {
      bg.setAttribute(k, v);
    }
    el.insertBefore(bg, el.firstChild);           // the figure's own background: readable whatever the page does
    // Bake every color into the drawing, so no page style (dark mode, forced colors, a viewer's own text
    // color) can turn a label or a line the same color as what's behind it.
    el.querySelectorAll('text, tspan, rect, path, polygon, polyline, circle, ellipse, line').forEach(n => {
      const c = getComputedStyle(n);
      n.style.setProperty('fill', c.fill, 'important');
      n.style.setProperty('stroke', c.stroke, 'important');
    });
    el.querySelectorAll('foreignObject *').forEach(n => {
      n.style.setProperty('color', getComputedStyle(n).color, 'important');
      n.style.setProperty('font-family', FONT, 'important');
    });
    el.querySelectorAll('text, tspan').forEach(n => n.style.setProperty('font-family', FONT, 'important'));
    el.removeAttribute('style');
    el.setAttribute('width', Math.round(vb.width)); el.setAttribute('height', Math.round(vb.height));
    out.push(host.innerHTML);
  }
  return out;
}
"""


def draw_diagrams(html: str) -> str:
    """Render every Mermaid block to SVG in Chromium and put the SVG in its place."""
    import re
    from playwright.sync_api import sync_playwright
    blocks = re.findall(r'<pre class="mermaid">.*?</pre>', html, flags=re.S)
    if not blocks:
        return html
    blocks = [re.sub(r',?"fontFamily":"[^"]*"', "", b) for b in blocks]     # the font is set once, below
    # A <br/> inside <pre> would become a real line-break element and vanish from the diagram's source text.
    blocks = [re.sub(r"<br\s*/?>", "&lt;br/&gt;", b) for b in blocks]
    page = ("<!doctype html><html><head><meta charset='utf-8'>"
            "</head><body>" + "".join(blocks) +
            f"<script src='{MERMAID}'></script></body></html>")
    tmp = HERE / ".diagrams.html"
    tmp.write_text(page, encoding="utf-8")
    exe = "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            pg = br.new_page()
            pg.goto(tmp.as_uri())
            pg.wait_for_function("window.mermaid !== undefined", timeout=60000)
            svgs = pg.evaluate(DRAW_JS % PAPER)
            br.close()
    finally:
        tmp.unlink(missing_ok=True)
    it = iter(svgs)
    return re.sub(r'<pre class="mermaid">.*?</pre>', lambda m: f'<div class="diagram">{next(it)}</div>', html,
                  flags=re.S)


def pdf(html_path: Path) -> Path:
    from playwright.sync_api import sync_playwright
    page_html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                 "<meta name='viewport' content='width=device-width, initial-scale=1'></head><body>"
                 + html_path.read_text(encoding="utf-8")
                 + "<script>document.fonts.ready.then(()=>{document.body.dataset.ready='1'})</script></body></html>")
    tmp = html_path.with_suffix(".print.html")
    tmp.write_text(page_html, encoding="utf-8")
    out = html_path.with_suffix(".pdf")
    exe = "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = br.new_page()
        pg.goto(tmp.as_uri())
        pg.wait_for_function("document.body.dataset.ready", timeout=60000)
        state = pg.evaluate("document.body.dataset.ready")
        if state != "1":
            raise RuntimeError(f"diagrams failed: {state}")
        pg.wait_for_timeout(1500)
        pg.emulate_media(media="print")
        pg.pdf(path=str(out), format="Letter", print_background=True, prefer_css_page_size=True,
               display_header_footer=True, header_template="<span></span>",
               footer_template="<div style='width:100%;font:8px Arial;color:#776d66;padding:0 0.6in;"
                               "display:flex;justify-content:space-between'><span>BSU Student Printing Ops · "
                               f"SPO-DOC-001 · v{__version__}"
                               "</span><span class='pageNumber'></span></div>")
        br.close()
    tmp.unlink()
    return out


if __name__ == "__main__":
    path = build()
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1e6:.1f} MB)")
    if "--pdf" in sys.argv:
        print(f"wrote {pdf(path).relative_to(ROOT)}")
