"""Dash application: the shell (sidebar, top bar, notifications, theme) and routing.

Pages live in dashboard/views/. The shell's controls (scope, period, theme) are
persistent components, so every page reads the same choices and they survive
navigation and reloads.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd
from dash import Dash, Input, Output, State, dcc, html, no_update

from .. import activity, config, geo, metrics as M
from .charts import SECTION_ORDER
from .components import icon, segmented
from .views import activity_log, analytics, executive, overview, station, stations
from .views.common import PERIODS, area_key
from .views.overview import activity_list

NAV = [("/", "Overview", "home"), ("/stations", "Stations", "grid"), ("/analytics", "Analytics", "chart"),
       ("/executive", "Executive", "briefcase"), ("/activity", "Activity", "list")]
PAGE_META = {
    "/": ("Overview", "What needs attention right now."),
    "/stations": ("Stations", "Every print station. Click one for its full history."),
    "/analytics": ("Analytics", "Reliability, faults, consumables and data quality over time."),
    "/executive": ("Executive summary", "Month and year-to-date results, and what stands out."),
    "/activity": ("Activity", "Everything that happened, newest first."),
}
USES_PERIOD = {"/analytics", "/activity", "/station"}
USES_SCOPE = {"/", "/stations", "/analytics", "/executive", "/activity"}
THEME_OPTIONS = [{"label": "Light", "value": "light"}, {"label": "Dark", "value": "dark"},
                 {"label": "BSU", "value": "crimson"}]


class DataCache:
    """Loads the dataset once; in live mode re-loads when new snapshot files land (checked each minute)."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.ds: M.Dataset | None = None
        self.sig = None
        self.checked = 0.0
        self.lock = threading.Lock()
        self.rollups = None   # kept across refreshes: finished days are processed only once

    def _signature(self):
        files = []
        for sub in ("snapshots", "scrape_log"):
            folder = self.data_dir / sub
            if folder.exists():
                files += [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in folder.iterdir()]
        return tuple(sorted(files))

    def get(self) -> M.Dataset:
        with self.lock:
            now = time.time()
            stale = self.ds is None or (not self.ds.is_demo and now - self.checked >= config.EXPECTED_INTERVAL_S)
            if stale:
                self.checked = now
                sig = self._signature()
                if self.ds is None or sig != self.sig:
                    from .. import rollup
                    if self.rollups is None:
                        self.rollups = rollup.RollupStore(self.data_dir, M.building_map())
                    self.ds = M.load(self.data_dir, rollups=self.rollups)
                    self.sig = sig
            return self.ds



def _route(path: str | None) -> str:
    path = (path or "/").rstrip("/") or "/"
    if path.startswith("/station/"):
        return "/station"
    return {"/management": "/analytics", "/operations": "/"}.get(path, path)


def _scope_panel(ds: M.Dataset):
    sections = [s for s in SECTION_ORDER if s in set(ds.stations["section"])] + \
        sorted(set(ds.stations["section"]) - set(SECTION_ORDER))
    areas = sorted(set(ds.stations["area"]), key=area_key)
    return html.Details(id="scope", className="popover", children=[
        html.Summary([icon("filter"), html.Span("All stations", id="scope-label")], className="topbar__button",
                     title="Choose which stations every page reports on"),
        html.Div(className="popover__panel", children=[
            html.Div("Show stations in", className="popover__title"),
            html.Div("Section", className="popover__label"),
            dcc.Checklist(id="scope-sections", options=[{"label": x, "value": x} for x in sections], value=[],
                          className="checks", labelClassName="checks__opt", persistence=True,
                          persistence_type="session"),
            html.Div("Area", className="popover__label"),
            dcc.Checklist(id="scope-areas", options=[{"label": x, "value": x} for x in areas], value=[],
                          className="checks", labelClassName="checks__opt", persistence=True,
                          persistence_type="session"),
            html.Button("Show all stations", id="scope-reset", className="btn btn--sm btn--block"),
        ]),
    ])


def _shell(ds: M.Dataset):
    return html.Div(className="shell", children=[
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="theme", storage_type="local"),
        dcc.Store(id="seen", storage_type="local"),
        dcc.Store(id="notif-latest"),
        dcc.Store(id="basemap-store", storage_type="local", data="street"),
        dcc.Store(id="burn-store", storage_type="session", data="per_week"),
        dcc.Download(id="kml-dl"),
        dcc.Interval(id="tick", interval=config.EXPECTED_INTERVAL_S * 1000),
        html.A("Skip to content", href="#content", className="skip"),
        html.Aside(className="sidebar", children=[
            dcc.Link(className="brand", href="/", children=[
                html.Span(className="brand__mark", **{"aria-hidden": "true"}),
                html.Div([html.Div("ResNet Print Ops", className="brand__name"),
                          html.Div("Bridgewater State University", className="brand__sub")]),
            ]),
            html.Nav(id="nav", className="nav", **{"aria-label": "Main"}),
            html.Div(id="side-status", className="side-status"),
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
                    html.Button([icon("bell", "Notifications"), html.Span(id="bell-count", className="badge")],
                                id="bell", className="topbar__icon-btn", title="Notifications"),
                    html.Div(segmented("theme-switch", THEME_OPTIONS, None, persistence="local"),
                             className="theme-switch", title="Theme"),
                ]),
            ]),
            html.Div(id="banner"),
            html.Main(id="content", className="content", tabIndex="-1"),
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

    # --- theme: OS default on first visit, then the person's choice ---------------------------
    app.clientside_callback(
        """
        function(choice) {
            var t = choice;
            if (!t) {
                t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
            }
            document.documentElement.dataset.theme = t;
            return t;
        }
        """,
        Output("theme", "data"), Input("theme-switch", "value"),
    )

    @app.callback(Output("theme-switch", "value"), Input("url", "pathname"), State("theme-switch", "value"),
                  State("theme", "data"))
    def theme_default(_, choice, current):
        # Show the active theme as selected even before the person has picked one.
        return no_update if choice else (current or no_update)

    # --- navigation & page chrome ---------------------------------------------------------------
    @app.callback(Output("nav", "children"), Output("page-title", "children"), Output("page-sub", "children"),
                  Output("period-wrap", "hidden"), Output("scope-wrap", "hidden"), Input("url", "pathname"))
    def chrome(path):
        route = _route(path)
        links = [dcc.Link([icon(ic), html.Span(label)], href=href,
                          className="nav__link" + (" is-active" if route == href or
                                                   (href == "/stations" and route == "/station") else ""))
                 for href, label, ic in NAV]
        if route == "/station":
            title, sub = "Station", "Status, history and consumables for one print station."
        else:
            title, sub = PAGE_META.get(route, ("Not found", ""))
        return links, title, sub, route not in USES_PERIOD, route not in USES_SCOPE

    @app.callback(Output("scope-label", "children"), Input("scope-sections", "value"), Input("scope-areas", "value"))
    def scope_label(sections, areas):
        chosen = (sections or []) + (areas or [])
        if not chosen:
            return "All stations"
        return chosen[0] if len(chosen) == 1 else f"{chosen[0]} +{len(chosen) - 1}"

    @app.callback(Output("scope-sections", "value"), Output("scope-areas", "value"),
                  Input("scope-reset", "n_clicks"), prevent_initial_call=True)
    def scope_reset(_):
        return [], []

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
            return [html.Div(segmented("an-tab", analytics.TABS, params.get("tab", "reliability"),
                                       persistence="session"), className="toolbar"), loading("an-body")]
        if route == "/executive":
            opts = executive.month_options(ds)
            return [html.Div(segmented("ex-month", opts, executive.default_month(ds)), className="toolbar"),
                    loading("ex-body")]
        if route == "/activity":
            return [html.Div(className="toolbar", children=[
                html.Div([icon("search"), dcc.Input(id="ac-q", type="search", placeholder="Search activity",
                                                    value=params.get("q", ""), debounce=0.25,
                                                    className="search__input")], className="search"),
                segmented("ac-groups", [{"label": g, "value": g} for g in activity_log.GROUPS],
                          ["Status", "Parts", "Monitoring"], multi=True),
            ]), loading("ac-body")]
        return html.Div([html.P("That page doesn't exist."), dcc.Link("Go to the overview", href="/", className="link")],
                        className="empty empty--page")

    scope = [Input("scope-sections", "value"), Input("scope-areas", "value")]

    @app.callback(Output("ov-body", "children"), *scope, Input("theme", "data"), Input("basemap-store", "data"),
                  Input("tick", "n_intervals"))
    def ov_body(sections, areas, theme, basemap, _):
        return overview.render(cache.get(), theme or "light", sections, areas, basemap or "street")

    @app.callback(Output("basemap-store", "data"), Input("basemap", "value"), prevent_initial_call=True)
    def keep_basemap(value):
        return value

    @app.callback(Output("st-body", "children"), Input("st-q", "value"), Input("st-status", "value"), *scope,
                  Input("tick", "n_intervals"))
    def st_body(q, status, sections, areas, _):
        return stations.render(cache.get(), sections, areas, q, status)

    @app.callback(Output("sd-body", "children"), Input("sd-id", "data"), Input("period", "value"),
                  Input("theme", "data"), Input("tick", "n_intervals"))
    def sd_body(sid, period, theme, _):
        return station.render(cache.get(), theme or "light", sid, period or "30")

    @app.callback(Output("an-body", "children"), Input("an-tab", "value"), *scope, Input("period", "value"),
                  Input("burn-store", "data"), Input("theme", "data"))
    def an_body(tab, sections, areas, period, burn, theme):
        return analytics.render(cache.get(), theme or "light", sections, areas, period or "30", tab, burn or "per_week")

    @app.callback(Output("burn-store", "data"), Input("burn-unit", "value"), prevent_initial_call=True)
    def keep_burn(value):
        return value

    @app.callback(Output("ex-body", "children"), Input("ex-month", "value"), *scope, Input("theme", "data"))
    def ex_body(month, sections, areas, theme):
        return executive.render(cache.get(), theme or "light", month, sections, areas)

    @app.callback(Output("ac-body", "children"), Input("ac-groups", "value"), Input("ac-q", "value"), *scope,
                  Input("period", "value"), Input("tick", "n_intervals"))
    def ac_body(groups, q, sections, areas, period, _):
        return activity_log.render(cache.get(), sections, areas, period or "30", groups, q)

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

    @app.callback(Output("kml-dl", "data"), Input("kml-btn", "n_clicks"),
                  State("scope-sections", "value"), State("scope-areas", "value"), prevent_initial_call=True)
    def kml(_, sections, areas):
        ds = cache.get()
        ids = ds.ids(section=sections or None, area=areas or None) if (sections or areas) else None
        stamp = ds.as_of.tz_convert(config.LOCAL_TZ)
        title = f"BSU print stations{' (DEMO DATA)' if ds.is_demo else ''} - {stamp:%Y-%m-%d %H:%M}"
        return dict(content=geo.to_kml(geo.building_points(ds, ids), title),
                    filename=f"bsu-print-stations-{stamp:%Y%m%d-%H%M}.kml",
                    type="application/vnd.google-earth.kml+xml")

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

    return app
