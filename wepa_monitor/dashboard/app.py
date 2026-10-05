"""Dash application: the shell (sidebar, header tools, notifications, appearance) and routing.

Pages live in dashboard/views/. The shell's controls (filter, period, appearance) are
persistent components, so every page reads the same choices and they survive
navigation and reloads.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from .. import (activity, config, export, football, geo, metrics as M, roundsmap, routing, sandman, security,
               theme_art)
from .charts import SECTION_ORDER
from . import explain as X
from .components import icon, page_tabs, prose, segmented
from .views import account_view, assistant, feedback_view, inventory_view, investigations_view, software
from .views import (activity_log, analytics, executive, insights_view, outcomes, overview, rounds, station,
                    stations, system)
from .views.common import PERIODS, area_key

# The main menu, in four groups by what people come to do. System health, Software and the Activity log are
# for administrators and live in the account menu.
NAV_GROUPS = [
    ("Operate", [("/", "Overview", "home"), ("/rounds", "Rounds", "route"), ("/stations", "Stations", "grid")]),
    ("Analyze", [("/insights", "Insights", "sparkle"), ("/analytics", "Analytics", "chart")]),
    ("Report", [("/executive", "Executive", "briefcase"), ("/outcomes", "IT Outcomes", "award")]),
    ("Manage", [("/investigations", "Investigations", "search"), ("/inventory", "Inventory", "boxes")]),
]
NAV = [item for _, items in NAV_GROUPS for item in items]
ADMIN_NAV = [("/system", "System health", "pulse"), ("/software", "Software and security", "shield"),
             ("/activity", "Activity log", "history")]
TABBAR = ["/", "/rounds", "/stations", "/insights"]     # phones: these four, then More
SHORT = {"/": "Overview", "/rounds": "Rounds", "/stations": "Stations", "/insights": "Insights"}
ACTIVE_ALIASES = {"/station": "/stations", "/investigation": "/investigations", "/changelog": "/software",
                  "/release": "/software"}
PAGE_META = {
    "/": ("Overview", "Live status of every print station, and what needs attention now."),
    "/insights": ("Insights", "What happened and why, in plain language. Ask a question in your own words."),
    "/rounds": ("Rounds", "The fastest route to every printer that needs a visit, for whoever is on shift."),
    "/stations": ("Stations", "Every print station with its current status. Select one for its full history."),
    "/analytics": ("Analytics", "Reliability, faults, supplies, usage, the station report card and forecasts."),
    "/executive": ("Executive summary", "Service levels for the month and year to date, with what changed."),
    "/outcomes": ("IT Outcomes", "A print-ready feature for the IT annual report, built from the monitoring data."),
    "/system": ("System health", "Is the monitor healthy? Data quality, the live log and who is signed in."),
    "/activity": ("Activity log", "Every status change, part replacement and system event, newest first."),
    "/investigations": ("Investigations", "Root-cause work on recurring problems, with a permanent audit trail."),
    "/inventory": ("Inventory", "Supplies on hand, where every unit went, deliveries, counts and kiosk keys."),
    "/account": ("My account", "Your photo, contact phone and password."),
    "/directory": ("Directory", "Everyone with an account. The administrator manages accounts."),
    "/investigation": ("Investigation", "One investigation: its record, decisions and audit trail."),
    "/software": ("Software and security", "Running version, known vulnerabilities, updates, backup collectors "
                                           "and a self-check of the whole app."),
    "/feedback": ("Suggest a feature", "Ideas and problem reports go straight to the development backlog."),
    "/changelog": ("Change log", "What changed in each version, in plain language, with sources."),
    "/release": ("Change log", "What changed in this version, in plain language, with sources."),
}
USES_PERIOD = {"/analytics", "/station"}
USES_SCOPE = {"/", "/insights", "/stations", "/analytics", "/executive", "/activity", "/outcomes"}
THEMES = [("auto", "Match my device", "Light or dark, following your system setting"),
          ("light", "Light", "Clean and bright"),
          ("dark", "Dark", "Easy on the eyes at night"),
          ("crimson", "BSU", "Bridgewater crimson and stone")]
# Themes that take a slot's place: (slot, theme, name, description). Cosmic and Cup are earned (see ui.js);
# Go Bears appears by itself on a football game day.
SPECIAL_THEMES = {"cosmic": ("dark", "Cosmic", "Glow in the dark, with neon printers in orbit"),
                  "cup": ("light", "Cup", "A teal swoosh and a purple squiggle, very 1994"),
                  "gobears": ("crimson", "Go Bears", "It's game day: crimson and gold, all in")}


def theme_options(eggs: dict | None, gameday: bool) -> list[dict]:
    """The appearance choices. Values never change (they are what the browser remembers); an unlocked or
    game-day theme takes over its slot's label, swatch, and look."""
    eggs = eggs or {}
    names = {k: (k, name, desc) for k, name, desc in THEMES}
    for theme, (slot, name, desc) in SPECIAL_THEMES.items():
        if eggs.get(theme) or (theme == "gobears" and gameday):
            names[slot] = (theme, name, desc)
    light, dark = names["light"][1], names["dark"][1]
    names["auto"] = ("auto", "Match my device", f"{light} or {dark}, following your system setting")
    return [{"label": html.Span([html.Span(className=f"swatch swatch--{look}"),
                                 html.Span([html.B(name), html.Span(desc, className="themes__desc")],
                                           className="themes__text")]), "value": slot}
            for slot, (look, name, desc) in names.items()]


def april_fools(now: datetime | None = None) -> bool:
    """True on April 1 on campus (America/New_York). WEPA_APRIL_FOOLS=1 or =0 overrides, for a demo or a test."""
    force = os.environ.get("WEPA_APRIL_FOOLS", "").strip()
    if force in ("0", "1"):
        return force == "1"
    today = (now or datetime.now(ZoneInfo(config.LOCAL_TZ))).date()
    return (today.month, today.day) == (4, 1)


def _voice(eggs: dict | None) -> str | None:
    """The AI's voice: a metal frontman while the Sandman theme is on, otherwise the usual analyst."""
    return sandman.VOICE if (eggs or {}).get("sandman") else None


def attach_refs(ds: M.Dataset) -> None:
    """Every outage's permanent reference number (OUT/JAM/PAP/SUP/ERR), as a 'ref' column."""
    from .. import refs
    try:
        ds.sev_inc = ds.sev_inc.assign(ref=refs.sync(ds))
        from .. import investigations
        investigations.detect(ds)
    except Exception as exc:  # noqa: BLE001 - numbering must never stop the dashboard loading
        print(f"reference numbers not assigned: {type(exc).__name__}: {exc}", flush=True)
        ds.sev_inc = ds.sev_inc.assign(ref="")
    try:
        from .. import inventory
        if ds.data_dir.name == "live" or os.environ.get("WEPA_INVENTORY_SYNC") == "1":
            inventory.sync(ds)
    except Exception as exc:  # noqa: BLE001 - inventory must never stop the dashboard loading
        print(f"inventory not updated: {type(exc).__name__}: {exc}", flush=True)


class DataCache:
    """The dataset every page reads.

    Live mode keeps it fresh in the background: shortly after each minute's snapshot, a
    worker thread checks for new files, builds the next dataset (re-using finished days, which
    are processed only once), pre-computes the shared forecasts, and swaps it in. Page requests
    never wait for a reload; they always get the latest finished dataset instantly. Only the
    very first request after startup waits for the initial load.
    """

    REFRESH_OFFSET_S = 15     # seconds after the minute: the collector has written by then

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.ds: M.Dataset | None = None
        self.sig = None
        self.lock = threading.Lock()          # serializes loads (the rollup store isn't thread-safe)
        self.rollups = None                   # kept across refreshes: finished days are processed only once
        self.worker: threading.Thread | None = None
        self.loaded_at: float | None = None
        self.load_s: float | None = None

    def _signature(self):
        files = []
        for sub in ("snapshots", "scrape_log", "imports/snapshots", "imports/scrape_log"):
            folder = self.data_dir / sub
            if folder.exists():
                files += [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in folder.iterdir()]
        return tuple(sorted(files))

    def refresh(self) -> bool:
        """Load a new dataset if the data files changed. Returns True if it swapped one in."""
        with self.lock:
            sig = self._signature()
            if self.ds is not None and sig == self.sig:
                return False
            from .. import models, rollup
            t0 = time.time()
            if self.rollups is None:
                self.rollups = rollup.RollupStore(self.data_dir, M.building_map())
            ds = M.load(self.data_dir, rollups=self.rollups)
            if not ds.empty:
                models.eol_forecast(ds)       # warm the one forecast nearly every page uses
                attach_refs(ds)
            self.ds, self.sig = ds, sig       # a single assignment: readers see old or new, never half
            self.loaded_at, self.load_s = time.time(), time.time() - t0
            return True

    def _loop(self):
        while True:
            time.sleep(config.EXPECTED_INTERVAL_S - (time.time() - self.REFRESH_OFFSET_S) % config.EXPECTED_INTERVAL_S)
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - keep serving the last good dataset
                print(f"dashboard refresh failed: {type(exc).__name__}: {exc}", flush=True)

    def get(self) -> M.Dataset:
        if self.ds is None:
            self.refresh()
            with self.lock:
                if not self.ds.is_demo and self.worker is None:
                    self.worker = threading.Thread(target=self._loop, name="dashboard-refresh", daemon=True)
                    self.worker.start()
        return self.ds


def _route(path: str | None) -> str:
    path = (path or "/").rstrip("/") or "/"
    if path.startswith("/station/"):
        return "/station"
    if path.startswith("/investigations/"):
        return "/investigation"
    if path.startswith("/changelog/"):
        return "/release"
    return {"/management": "/analytics", "/operations": "/", "/quality": "/system"}.get(path, path)


def page_heading(route: str) -> tuple[str, str]:
    if route == "/station":
        return "Station", "Status, history and supplies for one print station."
    return PAGE_META.get(route, ("Page not found", ""))


def _nav_link(href: str, label: str, ic: str, here: str, cls: str = "nav__link"):
    on = here == href
    # dcc.Link takes no aria attributes; the active page is marked for screen readers in the label instead.
    return dcc.Link([icon(ic), html.Span(label), html.Span(" (current page)", className="sr-only") if on else None],
                    href=href, title=label, className=cls + (" is-active" if on else ""))


def nav_menu(here: str) -> list:
    """The sidebar: four labelled groups."""
    return [html.Div(className="nav__group", role="group", **{"aria-label": group}, children=[
        html.Div(group, className="nav__heading", **{"aria-hidden": "true"}),
        *[_nav_link(href, label, ic, here) for href, label, ic in items]]) for group, items in NAV_GROUPS]


def tabbar(here: str) -> list:
    """Phones: the four most used pages along the bottom, and More for everything else, grouped."""
    by = {href: (label, ic) for href, label, ic in NAV}
    tabs = [_nav_link(href, SHORT[href], by[href][1], here, "tabbar__link") for href in TABBAR]
    in_more = here not in TABBAR
    more = html.Details(id="more", className="popover tabbar__more", children=[
        html.Summary([icon("more"), html.Span("More")], className="tabbar__link" + (" is-active" if in_more else ""),
                     title="All pages"),
        html.Div(className="popover__panel more-sheet", children=[
            *[html.Div(className="more-sheet__group", children=[
                html.Div(group, className="more-sheet__heading"),
                *[_nav_link(href, label, ic, here, "more-sheet__link") for href, label, ic in items]])
              for group, items in NAV_GROUPS],
            html.Div(className="more-sheet__group", children=[
                html.Div("Administration", className="more-sheet__heading"),
                *[_nav_link(href, label, ic, here, "more-sheet__link") for href, label, ic in ADMIN_NAV]]),
        ]),
    ])
    return [*tabs, more]


def _filter_panel(ds: M.Dataset):
    """One Filters button, in the same place on every page that has filters, showing what's applied. Its panel
    holds the stations and the reporting period, whichever the page uses."""
    sections = [s for s in SECTION_ORDER if s in set(ds.stations["section"])] + \
        sorted(set(ds.stations["section"]) - set(SECTION_ORDER))
    areas = sorted(set(ds.stations["area"]), key=area_key)
    st = ds.stations.sort_values(["building", "station_id"])
    station_opts = [{"label": f"{r.description} #{r.station_id} · {r.building}", "value": r.station_id,
                     "search": f"{r.description} {r.station_id} {r.building} {r.area}"} for r in st.itertuples()]
    return html.Details(id="scope", className="popover", children=[
        html.Summary([icon("sliders"), html.Span("Filters", className="filterbar__word"),
                      html.Span("All stations", id="scope-label", className="filterbar__value"),
                      html.Span(id="period-label", className="filterbar__value")],
                     className="topbar__button filterbar__button",
                     title="Choose the stations and period this page reports on"),
        html.Div(className="popover__panel popover__panel--wide", children=[
            html.Div(id="period-wrap", className="filter-section", children=[
                html.Div("Period", className="popover__title"),
                segmented("period", [{"label": PERIODS[k].capitalize() if k == "all" else f"Last {PERIODS[k]}",
                                      "value": k} for k in PERIODS], "30", persistence="session"),
            ]),
            html.Div(id="scope-wrap", className="filter-section", children=[
                html.Div("Stations", className="popover__title"),
                html.Label("Find a station", className="popover__label", htmlFor="scope-stations"),
                dcc.Dropdown(id="scope-stations", options=station_opts, value=[], multi=True, searchable=True,
                             placeholder="Type a name, building or number…", className="dropdown",
                             persistence=True, persistence_type="session"),
                html.Div(className="popover__cols", children=[
                    html.Div([html.Div("Section", className="popover__label"),
                              dcc.Checklist(id="scope-sections", options=[{"label": x, "value": x} for x in sections],
                                            value=[], className="checks", labelClassName="checks__opt",
                                            persistence=True, persistence_type="session")]),
                    html.Div([html.Div("Area", className="popover__label"),
                              dcc.Checklist(id="scope-areas", options=[{"label": x, "value": x} for x in areas],
                                            value=[], className="checks", labelClassName="checks__opt",
                                            persistence=True, persistence_type="session")]),
            ]),
            ]),
            html.Button("Clear filters", id="scope-reset", className="btn btn--sm btn--block"),
        ]),
    ])


def _appearance_panel():
    return html.Details(id="appearance", className="menu-section", children=[
        html.Summary([icon("palette"), html.Span("Appearance")], className="user-panel__link"),
        html.Div(className="appearance", children=[
            html.Div("Choose an appearance", className="popover__title"),
            html.P("Make it yours. Your choices are saved in this browser.", className="popover__hint"),
            dcc.RadioItems(id="theme-switch", value="crimson", persistence=True, persistence_type="local",
                           className="themes", labelClassName="themes__opt", options=theme_options({}, False)),
            html.Button("Exit Sandman", id="sandman-exit", className="btn btn--sm btn--block", hidden=True),
            html.Button("Bring back the original themes", id="eggs-reset", className="btn btn--sm btn--block",
                        hidden=True),
            html.Div("Text size", className="popover__label"),
            segmented("pref-text", [{"label": "Standard", "value": "standard"}, {"label": "Larger", "value": "large"}],
                      "standard", persistence="local"),
            html.Div("Spacing", className="popover__label"),
            segmented("pref-density", [{"label": "Comfortable", "value": "comfortable"},
                                       {"label": "Compact", "value": "compact"}], "comfortable", persistence="local"),
            html.Div("Motion", className="popover__label"),
            segmented("pref-motion", [{"label": "Animations on", "value": "full"},
                                      {"label": "Reduce motion", "value": "reduce"}], "full", persistence="local"),
            html.Div("Status colors", className="popover__label"),
            segmented("pref-contrast", [{"label": "Standard", "value": "standard"},
                                        {"label": "Extra contrast", "value": "high"}], "standard", persistence="local"),
        ]),
    ])


def _export_panel():
    return html.Details(id="export", className="popover", children=[
        html.Summary([icon("download"), html.Span("Download", className="filterbar__word")],
                     className="topbar__button", title="Download the data behind this page"),
        html.Div(className="popover__panel popover__panel--wide", children=[
            html.Div("Download data", className="popover__title"),
            html.P("Uses the stations and period you've chosen.", className="popover__hint"),
            html.Div("What", className="popover__label"),
            dcc.RadioItems(id="export-what", value="report_card", className="checks checks--radio",
                           labelClassName="checks__opt", persistence=True, persistence_type="local",
                           options=[{"label": v, "value": k} for k, v in export.DATASETS.items()]),
            html.Div("Format", className="popover__label"),
            segmented("export-format", [{"label": v, "value": k} for k, v in export.FORMATS.items()], "csv",
                      persistence="local"),
            html.Button([icon("download"), "Download"], id="export-go", className="btn btn--primary btn--block"),
            html.Div(id="export-status", className="popover__hint", role="status"),
        ]),
    ])


def _shell(ds: M.Dataset):
    return html.Div(className="shell", children=[
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="theme", storage_type="local"),
        dcc.Store(id="eggs", storage_type="local"),
        dcc.Store(id="gameday"),
        dcc.Store(id="aprilfools"),
        dcc.Store(id="scope-store", storage_type="session"),
        dcc.Store(id="seen", storage_type="local"),
        dcc.Store(id="notif-latest"),
        dcc.Store(id="basemap-store", storage_type="local", data="street"),
        dcc.Store(id="burn-store", storage_type="session", data="per_week"),
        dcc.Store(id="mttr-store", storage_type="session", data="W"),
        dcc.Store(id="session-beat"),
        dcc.Download(id="kml-dl"),
        dcc.Download(id="export-dl"),
        dcc.Interval(id="tick", interval=config.EXPECTED_INTERVAL_S * 1000),
        html.A("Skip to content", href="#content", className="skip"),
        html.Aside(className="sidebar", children=[
            dcc.Link(className="brand", href="/", title="Overview", children=[
                html.Span([html.I(), html.I(), html.I()], className="brand__mark", role="img",
                          **{"aria-label": "Boyden Hall, drawn as a wireframe"}),
                html.Div([html.Div("BSU Student Printing Ops", className="brand__name"),
                          html.Div("Bridgewater State University", className="brand__sub")]),
            ]),
            html.Nav(id="nav", className="nav", **{"aria-label": "Main"}),
            dcc.Link(id="side-status", className="side-status", href="/system", title="Monitor health"),
        ]),
        html.Div(className="main", children=[
            html.Header(className="topbar", children=[
                dcc.Link(html.Span([html.I(), html.I(), html.I()], className="brand__mark", role="img",
                                   **{"aria-label": "BSU Student Printing Ops: Overview"}),
                         href="/", className="topbar__brand"),
                html.Div([html.H1(id="page-title", className="topbar__title"),
                          html.P(id="page-sub", className="topbar__sub")], className="topbar__heading"),
                html.Div(className="topbar__tools", children=[
                    html.Button([icon("bell", "Alerts"), html.Span(id="bell-count", className="badge")],
                                id="bell", className="topbar__icon-btn", title="Alerts and recent activity"),
                    assistant.button(),
                    account_view.user_menu(admin_links=ADMIN_NAV, appearance=_appearance_panel()),
                ]),
                html.Div(id="filterbar", className="filterbar", children=[
                    _filter_panel(ds),
                    html.Div(id="export-wrap", children=_export_panel()),
                ]),
            ]),
            html.Div(id="gameday-banner", className="gameday-banner", hidden=True),
            html.Div(id="sandman-banner", className="sandman-banner", hidden=True, children=[
                html.Span(className="sandman-banner__text", **{"aria-hidden": "true"}),
                html.Span(className="sr-only", role="status"),
            ]),
            html.Button([icon("x"), "Stop the music"], id="sandman-stop", className="sandman-stop", hidden=True,
                        type="button"),
            html.Audio(id="sandman-audio", preload="none", hidden=True),
            html.Div(id="banner"),
            html.Main(id="content", className="content", tabIndex="-1"),
        ]),
        html.Nav(id="tabbar", className="tabbar", **{"aria-label": "Main (phone)"}),
        assistant.pane(),
        html.Div(id="egg-toast", className="egg-toast", role="status", **{"aria-live": "polite"}),
        dcc.Store(id="hints-off", storage_type="local"),
        html.Div(id="hint-sink", hidden=True),
        html.Div(id="xdrawer", className="drawer drawer--explain", children=[
            html.Div(id="xdrawer-backdrop", className="drawer__backdrop"),
            html.Aside(className="drawer__panel", role="dialog", **{"aria-label": "What this means"}, children=[
                html.Div(className="drawer__head", children=[
                    html.H2("What this means"),
                    html.Button(icon("x", "Close"), id="xdrawer-close", className="topbar__icon-btn"),
                ]),
                html.Div(id="xdrawer-body", className="drawer__body explain"),
            ]),
        ]),
        html.Div(id="drawer", className="drawer", children=[
            html.Div(id="drawer-backdrop", className="drawer__backdrop"),
            html.Aside(className="drawer__panel", role="dialog", **{"aria-label": "Alerts"}, children=[
                html.Div(className="drawer__head", children=[
                    html.H2("Alerts"),
                    html.Div([html.Button("Mark all read", id="mark-read", className="btn btn--sm"),
                              html.Button(icon("x", "Close"), id="drawer-close", className="topbar__icon-btn")],
                             className="drawer__actions"),
                ]),
                html.P("Stations going down or coming back, and gaps in monitoring, from the last 72 hours.",
                       className="drawer__sub"),
                html.Div(id="drawer-body", className="drawer__body"),
                dcc.Link("Open the full activity log", href="/activity", className="link drawer__foot",
                         id="drawer-foot"),
            ]),
        ]),
    ])


MAP_HOSTS = ("https://*.arcgisonline.com https://basemaps.cartocdn.com https://*.basemaps.cartocdn.com "
             "https://*.openstreetmap.org")


def _harden(app: Dash) -> None:
    """Browser-side defenses: a Content Security Policy that only runs this app's own scripts (Dash's
    inline bootstrap is allowed by hash, not by 'unsafe-inline'), no framing by other sites, no caching
    of private pages, and a cap on request size."""
    server = app.server
    server.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024          # uploads (profile pictures) are capped at 5 MB
    csp = {}

    def policy() -> str:
        if not csp:
            csp["v"] = "; ".join([
                "default-src 'self'",
                "script-src 'self' " + " ".join(app.csp_hashes()),
                "style-src 'self' 'unsafe-inline'",                    # Plotly sizes charts with inline styles
                f"img-src 'self' data: blob: {MAP_HOSTS}",
                f"connect-src 'self' {MAP_HOSTS}",
                "font-src 'self' data:",
                "worker-src 'self' blob:", "child-src 'self' blob:",
                "frame-src 'self' https://www.youtube-nocookie.com",   # the Sandman mini-player (sandman.py)
                "object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'self'",
            ])
        return csp["v"]

    @server.after_request
    def _headers(resp):
        from flask import request
        resp.headers.setdefault("Content-Security-Policy", policy())
        static = request.path.startswith(("/assets/", "/_dash-component-suites/", "/_favicon"))
        if not static:
            resp.headers["Cache-Control"] = "no-store"
        resp.headers.pop("Server", None)
        return resp


def create_app(data_dir: Path, preload: bool = False) -> Dash:
    cache = DataCache(data_dir)
    if preload:
        cache.get()   # load and process the data now, so the first page view is instant
    app = Dash(__name__, title="BSU Student Printing Ops", suppress_callback_exceptions=True,
               update_title=None, assets_folder=str(Path(__file__).parent / "assets"),
               assets_path_ignore=["vendor"])   # vendored libraries load only on the pages that use them
    app.layout = lambda: _shell(cache.get())
    app.server.config["WEPA_CACHE"] = cache
    security.install(app.server, data_dir)
    _harden(app)
    from .. import peer, sysevents, updates
    sysevents.configure(data_dir)
    peer.install(app.server, data_dir)
    try:
        updates.record_running(data_dir)
    except OSError as exc:
        security.log(f"couldn't record the running version: {exc}", "warn")
    from . import public
    public.register(app.server, cache)

    @app.server.get("/investigations/<ref>/evidence.pdf")
    def _evidence(ref):
        from flask import Response, abort
        data = investigations_view.evidence_pdf(cache.get(), ref)
        if data is None:
            abort(404)
        security.audit("evidence packet", ref)
        return Response(data, mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{ref}-evidence.pdf"'})

    # --- appearance: OS default until the person chooses; text size, spacing, motion, contrast ------
    app.clientside_callback(
        """
        function(choice, text, density, motion, contrast, eggs, gameday) {
            var t = choice || 'auto';
            if (t === 'auto') {
                t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
            }
            eggs = eggs || {};
            if (eggs.sandman) {
                document.documentElement.dataset.theme = 'sandman';
                d = document.documentElement.dataset;
                d.text = text || 'standard'; d.density = density || 'comfortable';
                d.motion = motion || 'full'; d.contrast = contrast || 'standard';
                if (window.spoSandman) { window.spoSandman.maybeStart(eggs); }
                return 'sandman';
            }
            if (t === 'dark' && eggs.cosmic) { t = 'cosmic'; }
            if (t === 'light' && eggs.cup) { t = 'cup'; }
            if (t === 'crimson' && gameday) { t = 'gobears'; }
            var d = document.documentElement.dataset;
            d.theme = t; d.text = text || 'standard'; d.density = density || 'comfortable';
            d.motion = motion || 'full'; d.contrast = contrast || 'standard';
            return t;
        }
        """,
        Output("theme", "data"), Input("theme-switch", "value"), Input("pref-text", "value"),
        Input("pref-density", "value"), Input("pref-motion", "value"), Input("pref-contrast", "value"),
        Input("eggs", "data"), Input("gameday", "data"),
    )

    # --- special themes: Cosmic and Cup (earned, see ui.js), Go Bears (football game days) ------------
    @app.callback(Output("gameday", "data"), Output("gameday-banner", "children"),
                  Output("gameday-banner", "hidden"), Input("url", "pathname"))
    def gameday(_):
        game = football.today_game(data_dir)
        if not game:
            return False, None, True
        return True, [html.Span(className="gameday-banner__ball", **{"aria-hidden": "true"}),
                      html.Span(football.headline(game))], False

    # 6 7 Bristaco (ui.js) only on April Fools' Day, campus time. WEPA_APRIL_FOOLS=1 / =0 forces it on / off.
    @app.callback(Output("aprilfools", "data"), Input("url", "pathname"))
    def aprilfools(_):
        return april_fools()

    app.clientside_callback(
        """
        function(on) { document.documentElement.dataset.aprilFools = on ? '1' : ''; return window.dash_clientside.no_update; }
        """,
        Output("aprilfools", "id"), Input("aprilfools", "data"),
    )

    @app.callback(Output("theme-switch", "options"), Output("eggs-reset", "hidden"), Output("sandman-exit", "hidden"),
                  Input("eggs", "data"), Input("gameday", "data"))
    def special_themes(eggs, gameday):
        eggs = eggs or {}
        return theme_options(eggs, bool(gameday)), not any(eggs.values()), not eggs.get("sandman")

    # Sandman: three or more words of the song in Ask the data (see wepa_monitor/sandman.py).
    sandman.install(app.server, data_dir)
    roundsmap.configure(data_dir)
    roundsmap.install(app.server)
    theme_art.install(app.server, data_dir)

    @app.callback(Output("eggs", "data", allow_duplicate=True), Input("ask-go", "n_clicks"), Input("ask-q", "value"),
                  State("eggs", "data"), prevent_initial_call=True)
    def sandman_on(_, question, eggs):
        if not question or not sandman.matches(question, data_dir):
            raise PreventUpdate
        audio = sandman.audio_file(data_dir) is not None
        return {**(eggs or {}), "sandman": int(time.time() * 1000), "sandman_audio": audio,
                "sandman_video": None if audio else sandman.youtube_id()}

    app.clientside_callback(
        """
        function(n, eggs) {
            if (!n) { return window.dash_clientside.no_update; }
            if (window.spoSandman) { window.spoSandman.stop(); }
            var out = Object.assign({}, eggs || {});
            delete out.sandman; delete out.sandman_audio; delete out.sandman_video;
            return out;
        }
        """,
        Output("eggs", "data", allow_duplicate=True), Input("sandman-exit", "n_clicks"), State("eggs", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(n) {
            if (!n) { return window.dash_clientside.no_update; }
            if (window.spoSandman) { window.spoSandman.stop(); }
            return {};
        }
        """,
        Output("eggs", "data", allow_duplicate=True), Input("eggs-reset", "n_clicks"), prevent_initial_call=True,
    )

    # --- navigation & page chrome ---------------------------------------------------------------
    @app.callback(Output("nav", "children"), Output("tabbar", "children"), Output("page-title", "children"),
                  Output("page-sub", "children"), Output("period-wrap", "hidden"), Output("scope-wrap", "hidden"),
                  Output("filterbar", "hidden"), Output("export-wrap", "hidden"), Input("url", "pathname"))
    def chrome(path):
        route = _route(path)
        here = ACTIVE_ALIASES.get(route, route)
        return (nav_menu(here), tabbar(here), *page_heading(route), route not in USES_PERIOD,
                route not in USES_SCOPE, route not in USES_PERIOD | USES_SCOPE,
                route not in USES_PERIOD | USES_SCOPE)

    @app.callback(Output("period-label", "children"), Input("period", "value"), Input("url", "pathname"))
    def period_label(period, path):
        if _route(path) not in USES_PERIOD:
            return ""
        return "All time" if period == "all" else f"Last {PERIODS.get(period or '30', '30 days')}"

    @app.callback(Output("scope-store", "data"), Output("scope-label", "children"),
                  Input("scope-sections", "value"), Input("scope-areas", "value"), Input("scope-stations", "value"))
    def scope_store(sections, areas, picked):
        sc = {"sections": sections or [], "areas": areas or [], "stations": picked or []}
        if picked:
            ds = cache.get()
            names = ds.stations.set_index("station_id")["description"]
            first = names.get(picked[0], picked[0])
            label = first if len(picked) == 1 else f"{first} +{len(picked) - 1}"
        else:
            chosen = (sections or []) + (areas or [])
            label = "All stations" if not chosen else chosen[0] if len(chosen) == 1 else \
                f"{chosen[0]} +{len(chosen) - 1}"
        return sc, label

    @app.callback(Output("scope-sections", "value"), Output("scope-areas", "value"),
                  Output("scope-stations", "value"), Input("scope-reset", "n_clicks"), prevent_initial_call=True)
    def scope_reset(_):
        return [], [], []

    @app.callback(Output("side-status", "children"), Output("banner", "children"), Input("tick", "n_intervals"))
    def side_status(_):
        ds = cache.get()
        local = ds.as_of.tz_convert(config.LOCAL_TZ)
        dq = M.data_quality(ds, *M.window(ds, 1))
        mode = html.Span("DEMO DATA" if ds.is_demo else "LIVE", className=f"badge badge--{'demo' if ds.is_demo else 'live'}")
        status = [mode, html.Div(f"As of {local:%a %b %-d, %-I:%M %p}", className="side-status__line"),
                  html.Div(f"Data quality {dq.value:.0f}/100" if dq.value is not None else "No data yet",
                           className="side-status__line")]
        banner = html.Div(className="banner", role="note", children=[
            html.Strong("Demo data. "), "Everything here is synthetic, generated to behave like the real Wepa "
            "status page. No figure describes actual BSU printers."]) if ds.is_demo else None
        return status, banner

    # --- page router: renders each page's skeleton; content callbacks fill it -------------------
    @app.callback(Output("content", "children"), Input("url", "pathname"), State("url", "search"))
    def router(path, search):
        ds = cache.get()
        route = _route(path)
        params = {k: v[0] for k, v in parse_qs((search or "").lstrip("?")).items()}
        loading = lambda id_: dcc.Loading(html.Div(id=id_), type="dot", delay_show=500)  # noqa: E731
        if route == "/":
            return loading("ov-body")
        if route == "/stations":
            status = params.get("status", "all")
            return [html.Div(className="toolbar", children=[
                html.Div([icon("search"), dcc.Input(id="st-q", type="search", value=params.get("q", ""),
                                                    placeholder="Search by name, building, area or number",
                                                    debounce=0.25, className="search__input")],
                         className="search"),
                segmented("st-status", [{"label": v, "value": k} for k, v in stations.STATUS_FILTERS.items()],
                          status if status in stations.STATUS_FILTERS else "all"),
            ]), loading("st-body")]
        if route == "/station":
            sid = (path or "").rstrip("/").rsplit("/", 1)[-1]
            return [dcc.Store(id="sd-id", data=sid), loading("sd-body")]
        if route == "/analytics":
            tab = params.get("tab", "reliability")
            if tab == "quality":
                return system.layout()
            return [page_tabs("an-tab", analytics.TABS, tab), loading("an-body")]
        if route == "/executive":
            opts = executive.month_options(ds)
            return [html.Div(segmented("ex-month", opts, executive.default_month(ds)), className="toolbar"),
                    loading("ex-body")]
        if route == "/insights":
            return insights_view.layout(params.get("story", "yesterday"))
        if route == "/rounds":
            return rounds.layout(params.get("team", "ResNet"))
        if route == "/activity":
            return activity_log.layout(ds, params)
        if route == "/outcomes":
            return outcomes.layout(ds)
        if route == "/system":
            return system.layout()
        if route == "/investigations":
            return investigations_view.layout(ds, params)
        if route == "/inventory":
            return inventory_view.layout(ds, params)
        if route == "/account":
            return account_view.layout(params)
        if route == "/directory":
            return account_view.directory()
        if route == "/investigation":
            return investigations_view.detail_layout((path or "").rstrip("/").rsplit("/", 1)[-1])
        if route == "/software":
            return software.layout()
        if route == "/feedback":
            return feedback_view.layout(params)
        if route == "/changelog":
            return software.changelog_layout()
        if route == "/release":
            return software.changelog_layout((path or "").rstrip("/").rsplit("/", 1)[-1])
        return html.Div([html.P("That page doesn't exist."), dcc.Link("Go to the overview", href="/", className="link")],
                        className="empty empty--page")

    scope = Input("scope-store", "data")

    @app.callback(Output("ov-body", "children"), scope, Input("theme", "data"), Input("basemap-store", "data"),
                  Input("tick", "n_intervals"))
    def ov_body(sc, theme, basemap, _):
        return overview.render(cache.get(), theme or "light", sc, basemap or "street")

    @app.callback(Output("basemap-store", "data"), Input("basemap", "value"), prevent_initial_call=True)
    def keep_basemap(value):
        return value

    @app.callback(Output("st-body", "children"), Input("st-q", "value"), Input("st-status", "value"), scope,
                  Input("tick", "n_intervals"))
    def st_body(q, status, sc, _):
        return stations.render(cache.get(), sc, q, status)

    @app.callback(Output("sd-body", "children"), Input("sd-id", "data"), Input("period", "value"),
                  Input("theme", "data"), Input("tick", "n_intervals"))
    def sd_body(sid, period, theme, _):
        return station.render(cache.get(), theme or "light", sid, period or "30")

    @app.callback(Output({"type": "xg", "chart": "eol"}, "figure"), Output("sd-eol-note", "children"),
                  Input("sd-eol-part", "value"), State("sd-id", "data"), State("theme", "data"),
                  prevent_initial_call=True)
    def sd_eol(comp, sid, theme):
        fig, note = station.eol_figure(cache.get(), theme or "light", sid, comp)
        from .charts import blank
        return (fig if fig is not None else blank()), note

    @app.callback(Output("an-body", "children"), Input("an-tab", "value"), scope, Input("period", "value"),
                  Input("burn-store", "data"), Input("mttr-store", "data"), Input("theme", "data"))
    def an_body(tab, sc, period, burn, mttr_unit, theme):
        return analytics.render(cache.get(), theme or "light", sc, period or "30", tab, burn or "per_week",
                                mttr_unit or "W")

    @app.callback(Output("mttr-store", "data"), Input("mttr-unit", "value"), prevent_initial_call=True)
    def keep_mttr(value):
        return value

    @app.callback(Output("burn-store", "data"), Input("burn-unit", "value"), prevent_initial_call=True)
    def keep_burn(value):
        return value

    @app.callback(Output("ex-body", "children"), Input("ex-month", "value"), scope, Input("theme", "data"))
    def ex_body(month, sc, theme):
        return executive.render(cache.get(), theme or "light", month, sc)

    @app.callback(Output("ac-body", "children"), Input("ac-groups", "value"), Input("ac-q", "value"), scope,
                  Input("ac-dates", "start_date"), Input("ac-dates", "end_date"), Input("tick", "n_intervals"))
    def ac_body(groups, q, sc, d0, d1, _):
        return activity_log.render(cache.get(), sc, d0, d1, groups, q)

    @app.callback(Output("ac-dates", "start_date"), Output("ac-dates", "end_date"),
                  Input({"type": "ac-quick", "days": ALL}, "n_clicks"), prevent_initial_call=True)
    def ac_quick(clicks):
        if not ctx.triggered_id or not any(clicks or []):
            raise PreventUpdate
        return activity_log.quick_range(cache.get(), ctx.triggered_id["days"])

    @app.callback(Output("oc-body", "children"), Input("oc-year", "value"), scope, Input("theme", "data"),
                  Input("oc-regen", "n_clicks"))
    def oc_body(year, sc, theme, draft):
        return outcomes.render(cache.get(), theme or "light", year, sc, draft or 0)

    @app.callback(Output("sy-body", "children"), Input("theme", "data"), Input("tick", "n_intervals"))
    def sy_body(theme, _):
        return system.render(cache.get(), theme or "light", cache)

    @app.callback(Output("sy-log", "children"), Input("sy-log-tick", "n_intervals"))
    def sy_log(_):
        return system.terminal_lines(cache.get(), cache)

    # --- rounds ---------------------------------------------------------------------------------------
    @app.callback(Output("rd-start", "options"), Output("rd-start", "value"), Input("rd-team", "value"),
                  State("rd-start", "value"))
    def rd_start(team, current):
        opts = rounds.start_options(team or "ResNet")
        values = [o["value"] for o in opts]
        default = values[0]
        # Keep a remembered start when it's a sensible place for this team; otherwise use the team's office.
        keep = current in values[:len(routing.TEAM_STARTS.get(team or "ResNet", []))] or (
            current in values and ctx.triggered_id is None)
        return opts, current if keep else default

    @app.callback(Output("rd-body", "children"), Input("rd-team", "value"), Input("rd-start", "value"),
                  Input("rd-mode", "value"), Input("rd-include", "value"), Input("rd-order", "value"),
                  Input("rd-return", "value"), Input("theme", "data"), Input("tick", "n_intervals"))
    def rd_body(team, start, mode, include, order, finish, theme, _):
        return rounds.render(cache.get(), theme or "light", team or "ResNet", start, mode or "walk", include,
                             order or "urgent", finish or "loop")

    # --- insights -------------------------------------------------------------------------------------
    @app.callback(Output("story-body", "children"), Input("story-period", "value"), scope, Input("tick", "n_intervals"))
    def story_body(key, sc, _):
        return insights_view.render_story(cache.get(), key, sc)

    @app.callback(Output("story-ai", "children"), Input("story-period", "value"), scope, State("eggs", "data"))
    def story_ai(key, sc, eggs):
        return insights_view.render_story_ai(cache.get(), key, sc, _voice(eggs))

    @app.callback(Output("ask-q", "value"), Input({"type": "ask-ex", "q": ALL}, "n_clicks"), prevent_initial_call=True)
    def ask_example(clicks):
        if not ctx.triggered_id or not any(clicks or []):
            return no_update
        return ctx.triggered_id["q"]

    # The question box is debounced: it answers on Enter, on the Ask button, or when a chip fills it in.
    @app.callback(Output("ask-body", "children"), Input("ask-go", "n_clicks"), Input("ask-q", "value"), scope,
                  State("eggs", "data"))
    def ask_body(_, question, sc, eggs):
        if question and sandman.matches(question, data_dir):
            return insights_view.render_sandman()
        return insights_view.render_answer(cache.get(), question, sc, _voice(eggs))

    # --- explain any clicked chart point ---------------------------------------------------------------
    @app.callback(Output("xdrawer-body", "children"), Output("xdrawer", "className"),
                  Input({"type": "xg", "chart": ALL}, "clickData"),
                  State("scope-store", "data"), State("period", "value"), State("url", "pathname"),
                  prevent_initial_call=True)
    def explain_point(_, sc, period, path):
        trig = ctx.triggered_id
        value = ctx.triggered[0]["value"] if ctx.triggered else None
        if not trig or not value:
            return no_update, no_update
        ds = cache.get()
        route = _route(path)
        sid = (path or "").rstrip("/").rsplit("/", 1)[-1] if route == "/station" else None
        from .views.common import scope_ids
        ids = [sid] if sid else scope_ids(ds, sc)
        days = None if period in (None, "all") else int(period)
        if route == "/":
            days = 30
        elif route in ("/executive", "/outcomes"):
            days = None
        start, end = M.window(ds, days)
        cx = X.Context(ids, start, end, "recorded history" if days is None else f"last {days} days", sid)
        ex = X.explain(ds, trig["chart"], value["points"][0], cx, value["points"])
        if ex is None:
            return no_update, no_update
        body = [html.Div(ex.eyebrow, className="explain__eyebrow"), html.P(ex.title, className="explain__title")]
        if ex.tells:
            body += [html.H3("What it tells you"), prose([ex.tells])]
        if ex.matters:
            body += [html.H3("Why it matters"), prose([ex.matters])]
        if ex.next:
            body += [html.H3("Explore next"), html.Ul([html.Li(dcc.Link(t, href=h, className="link")) for t, h in ex.next],
                                                       className="explain__links")]
        return body, "drawer drawer--explain is-open"

    app.clientside_callback(
        "function(a, b, path) { return 'drawer drawer--explain'; }",
        Output("xdrawer", "className", allow_duplicate=True), Input("xdrawer-close", "n_clicks"),
        Input("xdrawer-backdrop", "n_clicks"), Input("url", "pathname"), prevent_initial_call=True,
    )

    # --- the 'explore' tip: dismiss once, hidden everywhere -------------------------------------------------
    @app.callback(Output("hints-off", "data"), Input({"type": "hint-close", "n": ALL}, "n_clicks"),
                  prevent_initial_call=True)
    def hide_hints(clicks):
        return True if any(clicks or []) else no_update

    app.clientside_callback(
        "function(off) { document.documentElement.classList.toggle('hints-off', !!off); return ''; }",
        Output("hint-sink", "children"), Input("hints-off", "data"),
    )

    # --- map click → station page; KML download -------------------------------------------------
    @app.callback(Output("url", "pathname"), Output("url", "search"), Input("campus-map", "clickData"),
                  prevent_initial_call=True)
    def map_click(click):
        try:
            target = click["points"][0]["customdata"][1]
        except (TypeError, KeyError, IndexError):
            return no_update, no_update
        path, _, query = str(target).partition("?")
        return path, (f"?{query}" if query else "")

    @app.callback(Output("kml-dl", "data"), Input("kml-btn", "n_clicks"), State("scope-store", "data"),
                  prevent_initial_call=True)
    def kml(n_clicks, sc):
        # The button is re-created whenever the overview re-renders (every minute, on basemap
        # switches, on navigation), and Dash fires this callback for each new copy. Only a real
        # click (n_clicks >= 1 on the button that triggered) may start a download.
        if not n_clicks or ctx.triggered_id != "kml-btn":
            raise PreventUpdate
        ds = cache.get()
        from .views.common import scope_ids
        ids = scope_ids(ds, sc)
        stamp = ds.as_of.tz_convert(config.LOCAL_TZ)
        title = f"BSU print stations{' (DEMO DATA)' if ds.is_demo else ''} - {stamp:%Y-%m-%d %H:%M}"
        return dict(content=geo.to_kml(geo.building_points(ds, ids), title),
                    filename=f"bsu-print-stations-{stamp:%Y%m%d-%H%M}.kml",
                    type="application/vnd.google-earth.kml+xml")

    # --- export ---------------------------------------------------------------------------------------
    @app.callback(Output("export-dl", "data"), Output("export-status", "children"), Input("export-go", "n_clicks"),
                  State("export-what", "value"), State("export-format", "value"), State("scope-store", "data"),
                  State("period", "value"), prevent_initial_call=True)
    def do_export(n, what, fmt, sc, period):
        if not n:
            raise PreventUpdate
        ds = cache.get()
        from .views.common import scope_ids, scope_label
        try:
            data, filename, mime = export.build(ds, what or "report_card", fmt or "csv", scope_ids(ds, sc),
                                                scope_label(ds, sc), period or "30")
        except export.ExportError as exc:
            return no_update, str(exc)
        security.audit("export", f"{what}.{fmt}")
        return dcc.send_bytes(data, filename, type=mime), f"Downloaded {filename}"

    # --- my account ----------------------------------------------------------------------------------
    @app.callback(Output("sw-body", "children"), Input("sw-tick", "n_intervals"), Input("sw-msg", "children"))
    def sw_body(_, __):
        return software.render(cache.get())

    @app.callback(Output("sw-msg", "children"), Output("sw-pw", "value"),
                  Input("sw-check", "n_clicks"), Input("sw-prepare", "n_clicks"), Input("sw-start", "n_clicks"),
                  State("sw-pw", "value"), prevent_initial_call=True)
    def sw_act(_c, _p, _s, pw):
        trig = ctx.triggered_id
        if not trig:
            raise PreventUpdate
        msg, tone = software.act(cache.get(), trig, pw)
        return html.Span(msg, className="inv-ok" if tone == "ok" else "inv-err"), ""

    @app.callback(Output("fb-list", "children"), Input("url", "pathname"), Input("fb-msg", "children"))
    def fb_list(path, _):
        if _route(path) != "/feedback":
            raise PreventUpdate
        return feedback_view.render_list(data_dir)

    @app.callback(Output("fb-msg", "children"), Output("fb-title", "value"), Output("fb-body", "value"),
                  Input("fb-send", "n_clicks"), State("fb-kind", "value"), State("fb-title", "value"),
                  State("fb-body", "value"), State("fb-page", "data"), prevent_initial_call=True)
    def fb_send(n, kind, title, body, page):
        if not n:
            raise PreventUpdate
        msg, tone, clear = feedback_view.act(data_dir, kind, title, body, page)
        span = html.Span(msg, className="inv-ok" if tone == "ok" else "inv-err")
        return (span, "", "") if clear else (span, no_update, no_update)

    @app.callback(Output("acct-body", "children"), Input("url", "pathname"))
    def acct_body(path):
        if _route(path) != "/account":
            raise PreventUpdate
        return account_view.render()

    @app.callback(Output("acct-body", "children", allow_duplicate=True), Output("acct-msg", "children"),
                  Input("acct-photo-upload", "contents"), Input("acct-photo-remove", "n_clicks"),
                  Input("acct-phone-save", "n_clicks"), State("acct-phone", "value"), prevent_initial_call=True)
    def acct_act(contents, _rm, _save, phone):
        import base64

        from .. import accounts
        me = accounts.current()
        trig = ctx.triggered_id
        if not me.get("username") or me["role"] == "admin" or not (ctx.triggered and ctx.triggered[0].get("value")):
            raise PreventUpdate
        try:
            if trig == "acct-photo-upload":
                accounts.set_photo(me["username"], base64.b64decode(contents.split(",", 1)[1]))
                security.audit("profile picture changed", me["username"])
                msg = html.Span("Picture updated. It appears everywhere after the next page load.", className="inv-ok")
            elif trig == "acct-photo-remove":
                accounts.remove_photo(me["username"])
                msg = html.Span("Picture removed.", className="inv-ok")
            elif trig == "acct-phone-save":
                u = accounts.set_phone(me["username"], phone)
                security.audit("phone changed", me["username"])
                msg = html.Span(f"Phone saved: {u['phone'] or 'none'}.", className="inv-ok")
            else:
                raise PreventUpdate
        except accounts.AccountError as exc:
            return no_update, html.Span(str(exc), className="inv-err")
        return account_view.render(), msg

    # --- investigations -----------------------------------------------------------------------------
    @app.callback(Output("inv-list", "children"), Input("inv-filter", "value"), Input("inv-q", "value"),
                  Input("inv-new-msg", "children"))
    def inv_list(show, q, _):
        return investigations_view.render_list(cache.get(), show or "open", q or "")

    @app.callback(Output("inv-new-link", "options"), Input("inv-new-station", "value"))
    def inv_link_opts(sid):
        return investigations_view.link_options(cache.get(), sid)

    @app.callback(Output("inv-new-msg", "children"), Input("inv-new-go", "n_clicks"),
                  State("inv-new-station", "value"), State("inv-new-cat", "value"), State("inv-new-title", "value"),
                  State("inv-new-desc", "value"), State("inv-new-link", "value"), prevent_initial_call=True)
    def inv_create(n, sid, cat, title, desc, linked):
        if not n:
            raise PreventUpdate
        from .. import investigations as INV
        try:
            ref = investigations_view.create_manual(cache.get(), sid, cat, title, desc, linked)
        except INV.InvestigationError as exc:
            return html.Span(str(exc), className="inv-err")
        return html.Span(["Opened ", dcc.Link(ref, href=f"/investigations/{ref}", className="link mono"), "."])

    @app.callback(Output("inv-body", "children"), Input("inv-ref", "data"))
    def inv_detail(ref):
        return investigations_view.render_detail(cache.get(), ref)

    @app.callback(Output("inv-body", "children", allow_duplicate=True), Output("inv-msg", "children"),
                  Input("inv-save", "n_clicks"), Input({"type": "inv-go", "to": ALL}, "n_clicks"),
                  Input("inv-note-go", "n_clicks"), State("inv-ref", "data"),
                  State({"type": "inv-field", "name": ALL}, "value"), State({"type": "inv-field", "name": ALL}, "id"),
                  State("inv-decision-note", "value"), State("inv-note-text", "value"), prevent_initial_call=True)
    def inv_act(_save, _go, _note, ref, values, ids, decision, note_text):
        from .. import accounts, investigations as INV
        trig = ctx.triggered_id
        if not (ctx.triggered and ctx.triggered[0].get("value")):
            raise PreventUpdate                      # buttons appearing after a re-render, not clicks
        ds = cache.get()
        user = accounts.current()
        pending = {i["name"]: v for i, v in zip(ids or [], values or [])}
        try:
            if trig == "inv-save":
                INV.update(ds.data_dir, ref, user, pending)
                msg = html.Span("Saved.", className="inv-ok")
            elif isinstance(trig, dict) and trig.get("type") == "inv-go":
                r = INV.get(ds.data_dir, ref)
                level = INV.impact(ds, r["station_id"])["level"] if r and r.get("station_id") else None
                if r and r["state"] in INV.EDITABLE_IN and pending and user.get("can_edit"):
                    INV.update(ds.data_dir, ref, user, pending)     # keep what's on screen
                INV.transition(ds.data_dir, ref, user, trig["to"], decision or "", impact_now=level)
                msg = html.Span(f"Moved to {trig['to']}.", className="inv-ok")
            elif trig == "inv-note-go":
                INV.add_note(ds.data_dir, ref, user, note_text or "")
                msg = html.Span("Note added.", className="inv-ok")
            else:
                raise PreventUpdate
        except INV.InvestigationError as exc:
            return no_update, html.Span(str(exc), className="inv-err")
        return investigations_view.render_detail(ds, ref), msg

    inventory_view.register(app, cache)

    # Page tabs go into the address, so a tab can be bookmarked, shared or reopened.
    for tab_id, key, default in (("an-tab", "tab", "reliability"), ("inv-filter", "show", "open")):
        app.clientside_callback(
            f"""function(v) {{
                var q = (v && v !== '{default}') ? '?{key}=' + encodeURIComponent(v) : '';
                return window.location.search === q ? window.dash_clientside.no_update : q;
            }}""", Output("url", "search", allow_duplicate=True), Input(tab_id, "value"), prevent_initial_call=True)

    # --- assistant pane ------------------------------------------------------------------------------
    # Opening the assistant always starts a new chat: the old conversation is deleted, not just hidden.
    app.clientside_callback(
        """
        function(open, close, session) {
            var nu = window.dash_clientside.no_update;
            var id = (dash_clientside.callback_context.triggered[0] || {}).prop_id || '';
            var pane = document.getElementById('assist');
            var isOpen = pane && pane.classList.contains('is-open');
            if (id.indexOf('assist-open') === 0 && !isOpen) {
                setTimeout(function () { var q = document.getElementById('assist-q'); if (q) { q.focus(); } }, 240);
                return ['assist is-open', [], (session || 0) + 1, ''];
            }
            return ['assist', nu, nu, nu];
        }
        """,
        Output("assist", "className"), Output("assist-chat", "data", allow_duplicate=True),
        Output("assist-session", "data", allow_duplicate=True), Output("assist-q", "value", allow_duplicate=True),
        Input("assist-open", "n_clicks"), Input("assist-close", "n_clicks"), State("assist-session", "data"),
        prevent_initial_call=True,
    )

    # Sending shows the question and a typing indicator at once; the answer replaces the indicator.
    @app.callback(Output("assist-chat", "data", allow_duplicate=True), Output("assist-q", "value"),
                  Input("assist-send", "n_clicks"), Input("assist-q", "value"),
                  Input({"type": "assist-ex", "q": ALL}, "n_clicks"), State("assist-chat", "data"),
                  State("assist-session", "data"), prevent_initial_call=True)
    def assist_send(_, typed, __, chat, session):
        trig = ctx.triggered_id
        if isinstance(trig, dict):
            if not (ctx.triggered and ctx.triggered[0]["value"]):
                raise PreventUpdate
            question = trig["q"]
        else:
            question = (typed or "").strip()
        chat = list(chat or [])
        if not question or (chat and chat[-1].get("role") == "pending"):
            raise PreventUpdate
        chat = chat[-(assistant.MAX_MESSAGES - 2):] + [{"role": "user", "text": question[:400]},
                                                        {"role": "pending", "chat": session or 0}]
        return chat, ""

    @app.callback(Output("assist-reply", "data"), Input("assist-chat", "data"),
                  State("scope-store", "data"), State("url", "pathname"), State("eggs", "data"),
                  prevent_initial_call=True)
    def assist_answer(chat, sc, path, eggs):
        if not chat or chat[-1].get("role") != "pending" or len(chat) < 2:
            raise PreventUpdate
        question = chat[-2].get("text", "")
        return {"chat": chat[-1].get("chat", 0),
                "msg": assistant.reply(cache.get(), question, sc, path, chat[:-2], _voice(eggs))}

    # The answer joins the conversation only if that conversation is still the current one.
    app.clientside_callback(
        """
        function(reply, chat, session) {
            var nu = window.dash_clientside.no_update;
            if (!reply || !chat || !chat.length) { return nu; }
            var last = chat[chat.length - 1];
            if (last.role !== 'pending' || reply.chat !== (session || 0) || last.chat !== reply.chat) { return nu; }
            return chat.slice(0, -1).concat([reply.msg]);
        }
        """,
        Output("assist-chat", "data", allow_duplicate=True), Input("assist-reply", "data"),
        State("assist-chat", "data"), State("assist-session", "data"), prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(n, session) {
            if (!n) { return [window.dash_clientside.no_update, window.dash_clientside.no_update]; }
            return [[], (session || 0) + 1];
        }
        """,
        Output("assist-chat", "data", allow_duplicate=True), Output("assist-session", "data", allow_duplicate=True),
        Input("assist-new", "n_clicks"), State("assist-session", "data"), prevent_initial_call=True,
    )

    @app.callback(Output("assist-log", "children"), Output("assist-by", "children"),
                  Output("assist-disclaimer", "children"), Input("assist-chat", "data"), Input("url", "pathname"))
    def assist_log(chat, path):
        return (assistant.render(cache.get(), chat or [], path), assistant.provider_line(),
                assistant.disclaimer())

    # --- notifications ------------------------------------------------------------------------------
    app.clientside_callback(
        """
        function(open, close, backdrop, cls) {
            var id = (dash_clientside.callback_context.triggered[0] || {}).prop_id || '';
            if (id.indexOf('bell') === 0) { return (cls || '').indexOf('is-open') >= 0 ? 'drawer' : 'drawer is-open'; }
            return 'drawer';
        }
        """,
        Output("drawer", "className"), Input("bell", "n_clicks"), Input("drawer-close", "n_clicks"),
        Input("drawer-backdrop", "n_clicks"), State("drawer", "className"), prevent_initial_call=True,
    )

    @app.callback(Output("bell-count", "children"), Output("bell-count", "hidden"), Output("drawer-body", "children"),
                  Output("notif-latest", "data"), Input("tick", "n_intervals"), Input("seen", "data"))
    def notifications(_, seen):
        ds = cache.get()
        nt = activity.notifications(ds)
        latest = nt["ts"].max().isoformat() if len(nt) else None
        seen_ts = pd.Timestamp(seen) if seen else None
        unread = nt if seen_ts is None else nt[nt["ts"] > seen_ts]
        if nt.empty:
            body = html.Div("All quiet: nothing in the last 72 hours.", className="empty")
        else:
            from .views.overview import activity_list
            body = []
            if len(unread):
                body += [html.H3(f"New ({len(unread)})", className="feed-day"), activity_list(unread.head(30), True)]
            earlier = nt.drop(unread.index)
            if len(earlier):
                body += [html.H3("Earlier", className="feed-day"), activity_list(earlier.head(30), True)]
        count = len(unread)
        return (str(count) if count < 100 else "99+"), count == 0, body, latest

    @app.callback(Output("seen", "data"), Input("mark-read", "n_clicks"), State("notif-latest", "data"),
                  prevent_initial_call=True)
    def mark_read(_, latest):
        return latest or no_update

    # --- who's using the site: a heartbeat per open tab (see security.py) -----------------------------
    app.clientside_callback(
        """
        function(n, path) {
            var id = sessionStorage.getItem('wepa-tab');
            if (!id) { id = Math.random().toString(36).slice(2) + Date.now().toString(36); sessionStorage.setItem('wepa-tab', id); }
            fetch('/_session/beat', {method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({tab: id, path: path || '/'}), credentials: 'same-origin'}).catch(function () {});
            return n || 0;
        }
        """,
        Output("session-beat", "data"), Input("tick", "n_intervals"), Input("url", "pathname"),
    )

    return app
