"""Color tokens and Plotly styling, shared by every chart.

Values follow a validated data-viz palette: categorical slots in a fixed
order, a reserved status palette (never reused for series), and CMYK ink
colors for toner/drum series (validated for color-vision deficiency; the
neutral K ink is the one intentional exception, so those charts always carry
direct labels).

Three everyday themes: light, dark, and "crimson" (BSU). Three more take a slot's place when earned or when
the calendar says so: "cosmic" (glow-in-the-dark, replaces dark), "cup" (the 90s paper cup, replaces light)
and "gobears" (football game days, replaces BSU). "sandman" (black on black) overrides them all while it's on. Their chart series were checked the same way. The BSU theme takes its
colors from bridgew.edu's own stylesheets: crimson #89191F for chrome, with
warm stone neutrals. Its chart series (crimson, gold, BSU blue, green) were
re-validated as a set: every adjacent pair passes the color-vision checks.
Consumable charts keep their CMYK inks in every theme, because the color
*is* the meaning there.
"""
from __future__ import annotations

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

TOKENS = {
    "light": {
        "page": "#f9f9f7", "surface": "#fcfcfb", "ink": "#0b0b0b", "secondary": "#52514e",
        "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "border": "#e6e5df",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
        "ink_k": "#3d3c38", "ink_c": "#1592c7", "ink_m": "#cf3678", "ink_y": "#c99700",
        "seq": ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
        "neutral_bar": "#c3c2b7",
    },
    "dark": {
        "page": "#0d0d0d", "surface": "#1a1a19", "ink": "#ffffff", "secondary": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "border": "#2c2c2a",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
        "ink_k": "#a8a79f", "ink_c": "#2a9bd0", "ink_m": "#d6528a", "ink_y": "#b98c12",
        "seq": ["#1f2937", "#184f95", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef", "#b7d3f6"],
        "neutral_bar": "#52514e",
    },
}

TOKENS["crimson"] = {
    "page": "#f4f1ec", "surface": "#fbfaf7", "ink": "#1c1717", "secondary": "#595959",
    "muted": "#7d7873", "grid": "#e8e5de", "axis": "#cfcbc2", "border": "#e2ded6",
    # Validated order (adjacent pairs): crimson, gold, BSU blue, green. Charts here use at most 4.
    "series": ["#9e1b24", "#a87405", "#00558c", "#0f8c62", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "ink_k": "#3d3c38", "ink_c": "#1592c7", "ink_m": "#cf3678", "ink_y": "#c99700",
    "seq": ["#f7e4e3", "#eebfbd", "#e09591", "#cc6a66", "#b23f3e", "#8f1f24", "#5f1016"],
    "neutral_bar": "#cfcbc2",
}
# Cosmic bowling: neon on a black-light night sky. Series are bright enough for 3:1 against the surface.
TOKENS["cosmic"] = {
    "page": "#08061a", "surface": "#120e2e", "ink": "#ffffff", "secondary": "#dcd8f5",
    "muted": "#a7a1d6", "grid": "#272152", "axis": "#3d3672", "border": "#2c2560",
    "series": ["#2ef2ff", "#ff5ad9", "#b8ff4a", "#ffd23f", "#a98bff", "#ff8a5c", "#5cffb6", "#ff6b8f"],
    "ink_k": "#b8b4d6", "ink_c": "#2ec4f2", "ink_m": "#ff5ad9", "ink_y": "#ffe14a",
    "seq": ["#1d1747", "#2f2479", "#4b2fa6", "#7a3cc9", "#b34fd9", "#e86ae0", "#ff9cf0"],
    "neutral_bar": "#3d3672",
}
# The 90s paper cup: white with a teal brush stroke and a purple squiggle.
TOKENS["cup"] = {
    "page": "#fbfaf6", "surface": "#ffffff", "ink": "#1d1733", "secondary": "#4a4560",
    "muted": "#77728c", "grid": "#ece9f2", "axis": "#cfcadb", "border": "#e6e2ee",
    "series": ["#0d8a99", "#5d2d8c", "#d6457f", "#b87600", "#2f6fd1", "#0a7f3f", "#8c5a2d", "#c13a3a"],
    "ink_k": "#3d3c38", "ink_c": "#1592c7", "ink_m": "#cf3678", "ink_y": "#c99700",
    "seq": ["#dff3f4", "#b5e3e7", "#7fcbd3", "#3eaebb", "#13909e", "#3f4d9a", "#5d2d8c"],
    "neutral_bar": "#cfcadb",
}
# Go Bears (game days): the BSU palette, louder. Crimson and gold lead.
TOKENS["gobears"] = {**TOKENS["crimson"], "page": "#f6efe4", "surface": "#fffcf6", "border": "#e6dccb",
                     "grid": "#ece3d3"}
# Sandman (the black album): grey on black. Series step through greys with a steel and a rust for contrast.
TOKENS["sandman"] = {
    "page": "#050505", "surface": "#0c0c0c", "ink": "#d0d0d0", "secondary": "#a8a8a8",
    "muted": "#8e8e8e", "grid": "#1b1b1b", "axis": "#2c2c2c", "border": "#1e1e1e",
    "series": ["#dcdcdc", "#7f9bb3", "#b08d6a", "#8a8a8a", "#a7b38a", "#c9a3c0", "#e0c27a", "#d98c8c"],
    "ink_k": "#a8a79f", "ink_c": "#2a9bd0", "ink_m": "#d6528a", "ink_y": "#b98c12",
    "seq": ["#161616", "#262626", "#3d3d3d", "#5c5c5c", "#828282", "#adadad", "#dcdcdc"],
    "neutral_bar": "#3a3a3a",
}
THEMES = ("light", "dark", "crimson", "cosmic", "cup", "gobears", "sandman")
DARK_THEMES = ("dark", "cosmic", "sandman")


def is_dark(theme: str) -> bool:
    return theme in DARK_THEMES

STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
STATE_STYLE = {   # labels from vocab.STATE
    "green": ("good", "✓", "Operational"),
    "yellow": ("warning", "!", "Degraded"),
    "red": ("critical", "✕", "Out of service"),
    "stale": ("serious", "?", "No signal"),
}


def ink(theme: str, component: str) -> str:
    """Color for a consumable series: CMYK inks for toner/drums, categorical for belt/fuser."""
    # Consumables are never brand-themed: the BSU, cup and game-day themes use the light theme's inks.
    t = TOKENS["light" if theme in ("crimson", "cup", "gobears") else theme]
    suffix = component.rsplit("_", 1)[-1]
    if component.startswith(("toner", "drum")):
        return t[f"ink_{suffix}"]
    return t["series"][0] if component == "belt" else t["series"][1]


def layout(theme: str, height: int = 280, **overrides) -> dict:
    t = TOKENS[theme]
    axis = dict(gridcolor=t["grid"], linecolor=t["axis"], zeroline=False, tickcolor=t["axis"],
                tickfont=dict(color=t["muted"], size=11), title=dict(font=dict(color=t["secondary"], size=12)),
                showline=True, automargin=True)
    base = dict(
        template="none", height=height,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=12, color=t["secondary"]),
        margin=dict(l=8, r=12, t=8, b=8),
        xaxis={**axis, "showgrid": False},
        yaxis={**axis, "showgrid": True, "showline": False},
        hoverlabel=dict(bgcolor=t["surface"], bordercolor=t["border"], font=dict(color=t["ink"], family=FONT)),
        legend=dict(orientation="h", x=0, y=1.0, yanchor="bottom", font=dict(color=t["secondary"]),
                    bgcolor="rgba(0,0,0,0)"),
        barcornerradius=4,
        bargap=0.35,
    )
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


GRAPH_CONFIG = {"displaylogo": False, "responsive": True,
                "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines"]}
