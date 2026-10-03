"""Dash application: the shell (sidebar, header tools, notifications, appearance) and routing.

Pages live in dashboard/views/. The shell's controls (filter, period, appearance) are
persistent components, so every page reads the same choices and they survive
navigation and reloads.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from .. import activity, config, export, geo, metrics as M, routing, security
from .charts import SECTION_ORDER
from . import explain as X
from .components import icon, prose, segmented
from .views import (activity_log, analytics, executive, insights_view, outcomes, overview, rounds, station,
                    stations, system)
from .views.common import PERIODS, area_key

NAV = [("/", "Overview", "home"), ("/insights", "Insights", "sparkle"), ("/rounds", "Rounds", "route"),
       ("/stations", "Stations", "grid"), ("/analytics", "Analytics", "chart"),
       ("/executive", "Executive", "briefcase"), ("/outcomes", "IT Outcomes", "award"),
       ("/system", "System", "pulse")]
PAGE_META = {
    "/": ("Overview", "What needs attention right now."),
    "/insights": ("Insights", "The story behind the numbers. Pick a time range, or ask a question."),
    "/rounds": ("Rounds", "The fastest route to every printer that needs a visit, for whoever is on shift."),
    "/stations": ("Stations", "Every print station. Click one for its full history."),
    "/analytics": ("Analytics", "Reliability, faults, supplies, usage and the station report card."),
    "/executive": ("Executive summary", "Month and year-to-date results, and what stands out."),
    "/outcomes": ("IT Outcomes", "A ready-to-print page for the IT annual report."),
    "/system": ("System", "Is the monitor healthy? Data quality, the live log, and who's using the site."),
    "/activity": ("Activity", "Everything that happened, newest first."),
}
USES_PERIOD = {"/analytics", "/station"}
USES_SCOPE = {"/", "/insights", "/stations", "/analytics", "/executive", "/activity", "/outcomes"}
THEMES = [("auto", "Match my device", "Light or dark, following your system setting"),
          ("light", "Light", "Clean and bright"),
          ("dark", "Dark", "Easy on the eyes at night"),
          ("crimson", "BSU", "Bridgewater crimson and stone")]


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
    return {"/management": "/analytics", "/operations": "/", "/quality": "/system"}.get(path, path)


def _scope_panel(ds: M.Dataset):
    sections = [s for s in SECTION_ORDER if s in set(ds.stations["section"])] + \
        sorted(set(ds.stations["section"]) - set(SECTION_ORDER))
    areas = sorted(set(ds.stations["area"]), key=area_key)
    st = ds.stations.sort_values(["building", "station_id"])
    station_opts = [{"label": f"{r.description} #{r.station_id} · {r.building}", "value": r.station_id,
                     "search": f"{r.description} {r.station_id} {r.building} {r.area}"} for r in st.itertuples()]
    return html.Details(id="scope", className="popover", children=[
        html.Summary([icon("filter"), html.Span("All stations", id="scope-label", className="topbar__label")],
                     className="topbar__button", title="Choose which stations every page reports on"),
        html.Div(className="popover__panel popover__panel--wide", children=[
            html.Div("Show stations", className="popover__title"),
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
            html.Button("Show all stations", id="scope-reset", className="btn btn--sm btn--block"),
        ]),
    ])


def _appearance_panel():
    return html.Details(id="appearance", className="popover", children=[
        html.Summary(icon("palette", "Appearance"), className="topbar__icon-btn", title="Choose an appearance"),
        html.Div(className="popover__panel popover__panel--wide appearance", children=[
            html.Div("Choose an appearance", className="popover__title"),
            html.P("Make it yours. Your choices are saved in this browser.", className="popover__hint"),
            dcc.RadioItems(id="theme-switch", value="crimson", persistence=True, persistence_type="local",
                           className="themes", labelClassName="themes__opt", options=[
                               {"label": html.Span([html.Span(className=f"swatch swatch--{k}"),
                                                    html.Span([html.B(name), html.Span(desc, className="themes__desc")],
                                                              className="themes__text")]), "value": k}
                               for k, name, desc in THEMES]),
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
        html.Summary([icon("download"), html.Span("Export", className="topbar__label")], className="topbar__button",
                     title="Download the data behind this page"),
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
                html.Img(src="/assets/bsu-bear.png", alt="Bridgewater State Bears", className="brand__logo"),
                html.Div([html.Div("ResNet Print Ops", className="brand__name"),
                          html.Div("Bridgewater State University", className="brand__sub")]),
            ]),
            html.Nav(id="nav", className="nav", **{"aria-label": "Main"}),
            dcc.Link(id="side-status", className="side-status", href="/system", title="Monitor health"),
        ]),
        html.Div(className="main", children=[
            html.Header(className="topbar", children=[
                html.Div([html.H1(id="page-title", className="topbar__title"),
                          html.P(id="page-sub", className="topbar__sub")], className="topbar__heading"),
                html.Div(className="topbar__tools", children=[
                    html.Div(_scope_panel(ds), id="scope-wrap"),
                    html.Div(segmented("period", [{"label": k if k == "all" else f"{k}d", "value": k}
                                                  for k in PERIODS], "30", persistence="session"),
                             id="period-wrap", title="Reporting period"),
                    _export_panel(),
                    dcc.Link(icon("history", "Activity log"), href="/activity", id="activity-link",
                             className="topbar__icon-btn", title="Activity log"),
                    html.Button([icon("bell", "Notifications"), html.Span(id="bell-count", className="badge")],
                                id="bell", className="topbar__icon-btn", title="Notifications"),
                    _appearance_panel(),
                ]),
            ]),
            html.Div(id="banner"),
            html.Main(id="content", className="content", tabIndex="-1"),
        ]),
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
            html.Aside(className="drawer__panel", role="dialog", **{"aria-label": "Notifications"}, children=[
                html.Div(className="drawer__head", children=[
                    html.H2("Notifications"),
                    html.Div([html.Button("Mark all read", id="mark-read", className="btn btn--sm"),
                              html.Button(icon("x", "Close"), id="drawer-close", className="topbar__icon-btn")],
                             className="drawer__actions"),
                ]),
                html.P("Stations going down or coming back, and monitoring gaps, from the last 72 hours.",
                       className="drawer__sub"),
                html.Div(id="drawer-body", className="drawer__body"),
                dcc.Link("Open the full activity log", href="/activity", className="link drawer__foot",
                         id="drawer-foot"),
            ]),
        ]),
    ])


def create_app(data_dir: Path, preload: bool = False) -> Dash:
    cache = DataCache(data_dir)
    if preload:
        cache.get()   # load and process the data now, so the first page view is instant
    app = Dash(__name__, title="ResNet Print Ops", suppress_callback_exceptions=True,
               update_title=None, assets_folder=str(Path(__file__).parent / "assets"))
    app.layout = lambda: _shell(cache.get())
    app.server.config["WEPA_CACHE"] = cache
    security.install(app.server, data_dir)

    # --- appearance: OS default until the person chooses; text size, spacing, motion, contrast ------
    app.clientside_callback(
        """
        function(choice, text, density, motion, contrast) {
            var t = choice || 'auto';
            if (t === 'auto') {
                t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
            }
            var d = document.documentElement.dataset;
            d.theme = t; d.text = text || 'standard'; d.density = density || 'comfortable';
            d.motion = motion || 'full'; d.contrast = contrast || 'standard';
            return t;
        }
        """,
        Output("theme", "data"), Input("theme-switch", "value"), Input("pref-text", "value"),
        Input("pref-density", "value"), Input("pref-motion", "value"), Input("pref-contrast", "value"),
    )

    # --- navigation & page chrome ---------------------------------------------------------------
    @app.callback(Output("nav", "children"), Output("page-title", "children"), Output("page-sub", "children"),
                  Output("period-wrap", "hidden"), Output("scope-wrap", "hidden"),
                  Output("activity-link", "className"), Input("url", "pathname"))
    def chrome(path):
        route = _route(path)
        links = [dcc.Link([icon(ic), html.Span(label)], href=href, title=label,
                          className="nav__link" + (" is-active" if route == href or
                                                   (href == "/stations" and route == "/station") else ""))
                 for href, label, ic in NAV]
        if route == "/station":
            title, sub = "Station", "Status, history and consumables for one print station."
        else:
            title, sub = PAGE_META.get(route, ("Not found", ""))
        return (links, title, sub, route not in USES_PERIOD, route not in USES_SCOPE,
                "topbar__icon-btn" + (" is-active" if route == "/activity" else ""))

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
            return [html.Div(segmented("an-tab", analytics.TABS, tab,
                                       persistence="session"), className="toolbar"), loading("an-body")]
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

    @app.callback(Output("oc-body", "children"), Input("oc-year", "value"), scope, Input("theme", "data"))
    def oc_body(year, sc, theme):
        return outcomes.render(cache.get(), theme or "light", year, sc)

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

    @app.callback(Output("ask-q", "value"), Input({"type": "ask-ex", "q": ALL}, "n_clicks"), prevent_initial_call=True)
    def ask_example(clicks):
        if not ctx.triggered_id or not any(clicks or []):
            return no_update
        return ctx.triggered_id["q"]

    # The question box is debounced: it answers on Enter, on the Ask button, or when a chip fills it in.
    @app.callback(Output("ask-body", "children"), Input("ask-go", "n_clicks"), Input("ask-q", "value"), scope)
    def ask_body(_, question, sc):
        return insights_view.render_answer(cache.get(), question, sc)

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
