"""Dash application: layout shell, routing, filters and callbacks."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from dash import Dash, Input, Output, State, dcc, html

from .. import config, geo, metrics as M
from . import pages
from .charts import SECTION_ORDER

ROUTES = {"/": "Operations", "/management": "Management", "/executive": "Executive summary"}


class DataCache:
    """Loads the dataset once; in live mode re-loads when new snapshot files land (checked each minute)."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.ds: M.Dataset | None = None
        self.sig = None
        self.checked = 0.0
        self.lock = threading.Lock()

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
                    self.ds = M.load(self.data_dir)
                    self.sig = sig
            return self.ds


def _chips(id_, options, value, multi=True):
    comp = dcc.Checklist if multi else dcc.RadioItems
    return comp(id=id_, options=options, value=value, inline=True, className="chips",
                labelClassName="chip", inputClassName="chip__input")


def _filters(ds: M.Dataset, extra=None):
    sections = [s for s in SECTION_ORDER if s in set(ds.stations["section"])] + \
               sorted(set(ds.stations["section"]) - set(SECTION_ORDER))
    areas = sorted(set(ds.stations["area"]), key=lambda a: pages.AREA_ORDER.index(a)
                   if a in pages.AREA_ORDER else 99)
    groups = [
        html.Div([html.Span("Section", className="filters__label"),
                  _chips("f-section", [{"label": s, "value": s} for s in sections], [])],
                 className="filters__group"),
        html.Div([html.Span("Area", className="filters__label"),
                  _chips("f-area", [{"label": a, "value": a} for a in areas], [])],
                 className="filters__group"),
    ]
    return html.Div((extra or []) + groups, className="filters", role="group", **{"aria-label": "Filters"})


def create_app(data_dir: Path, preload: bool = False) -> Dash:
    cache = DataCache(data_dir)
    if preload:
        cache.get()   # load and process the data now, so the first page view is instant
    app = Dash(__name__, title="ResNet Print Ops", suppress_callback_exceptions=True,
               update_title=None, assets_folder=str(Path(__file__).parent / "assets"))

    app.layout = html.Div(id="app", className="app", children=[
        dcc.Location(id="url"),
        dcc.Store(id="theme", storage_type="local"),
        dcc.Interval(id="tick", interval=config.EXPECTED_INTERVAL_S * 1000),
        html.Header(className="topbar", children=[
            html.Div(className="brand", children=[
                html.Span(className="brand__mark", **{"aria-hidden": "true"}),
                html.Div([html.Div("ResNet Print Ops", className="brand__name"),
                          html.Div("Bridgewater State University · Wepa print stations", className="brand__sub")]),
            ]),
            html.Nav(id="nav", className="nav"),
            html.Div(id="strip", className="strip"),
            html.Button("◐", id="theme-toggle", className="theme-toggle", title="Switch light / dark",
                        **{"aria-label": "Switch light or dark theme"}),
        ]),
        html.Div(id="banner"),
        html.Main(id="page", className="page"),
        html.Div(id="theme-sink", style={"display": "none"}),
    ])

    # Theme: default to the OS preference, toggle on click, stamp <html data-theme>.
    app.clientside_callback(
        """
        function(n, current) {
            if (!current) {
                const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
                current = dark ? 'dark' : 'light';
                if (!n) return current;
            }
            if (n) return current === 'dark' ? 'light' : 'dark';
            return current;
        }
        """,
        Output("theme", "data"), Input("theme-toggle", "n_clicks"), State("theme", "data"),
    )
    app.clientside_callback(
        "function(t){ if(t){ document.documentElement.dataset.theme = t; } return ''; }",
        Output("theme-sink", "children"), Input("theme", "data"),
    )

    @app.callback(Output("nav", "children"), Input("url", "pathname"))
    def nav(path):
        path = path if path in ROUTES else "/"
        return [dcc.Link(label, href=href, className="nav__link" + (" is-active" if href == path else ""))
                for href, label in ROUTES.items()]

    @app.callback(Output("strip", "children"), Output("banner", "children"), Input("tick", "n_intervals"))
    def strip(_):
        ds = cache.get()
        local = ds.as_of.tz_convert(config.LOCAL_TZ)
        mode = html.Span("DEMO", className="badge badge--demo") if ds.is_demo else \
            html.Span("LIVE", className="badge badge--live")
        dq = M.data_quality(ds, *M.window(ds, 1))
        dq_txt = f"Data quality {dq.value:.0f}" if dq.value is not None else "No data yet"
        strip_children = [mode, html.Span(f"As of {local:%a %b %-d, %-I:%M %p}"),
                          html.Span(dq_txt, title="Last 24 h · see Management → Data quality")]
        banner = None
        if ds.is_demo:
            banner = html.Div(className="banner", role="note", children=[
                html.Strong("Demo data. "),
                "Everything on this page is synthetic, generated to behave like the real Wepa status page "
                "(same stations, codes and formats). No figure here describes actual BSU printers.",
            ])
        return strip_children, banner

    @app.callback(Output("page", "children"), Input("url", "pathname"))
    def shell(path):
        ds = cache.get()
        path = path if path in ROUTES else "/"
        if path == "/":
            extra = [html.Div([html.Span("Map", className="filters__label"),
                               _chips("f-basemap", [{"label": "Street", "value": "street"},
                                                    {"label": "Aerial", "value": "satellite"}],
                                      "street", multi=False)], className="filters__group"),
                     html.Button("Open in Google Earth (.kml)", id="kml-btn", className="btn",
                                 title="Download the current building pins as KML for Google Earth"),
                     dcc.Download(id="kml-dl")]
            return [html.Div(className="page__head", children=[
                        html.H1("Operations"),
                        html.P("What needs attention right now. Refreshes every minute.", className="lede")]),
                    _filters(ds, extra), dcc.Loading(html.Div(id="ops-content"), type="dot", delay_show=400)]
        if path == "/management":
            extra = [
                html.Div([html.Span("Period", className="filters__label"),
                          _chips("f-period", [{"label": "7 days", "value": "7"}, {"label": "30 days", "value": "30"},
                                              {"label": "90 days", "value": "90"}, {"label": "All", "value": "all"}],
                                 "30", multi=False)], className="filters__group"),
                html.Div([html.Span("Burn rate per", className="filters__label"),
                          _chips("f-burn", [{"label": v, "value": k} for k, v in pages.BURN_UNITS.items()],
                                 "per_week", multi=False)], className="filters__group"),
            ]
            return [html.Div(className="page__head", children=[
                        html.H1("Management"),
                        html.P("Reliability, response times, consumable usage and data quality over a period.",
                               className="lede")]),
                    _filters(ds, extra), dcc.Loading(html.Div(id="mgmt-content"), type="dot", delay_show=400)]
        opts = pages.month_options(ds)
        extra = [html.Div([html.Span("Month", className="filters__label"),
                           _chips("f-month", opts, pages.default_month(ds), multi=False)],
                          className="filters__group")]
        return [html.Div(className="page__head", children=[
                    html.H1("Executive summary"),
                    html.P("Month and year-to-date results, with the observations worth knowing.", className="lede")]),
                _filters(ds, extra), dcc.Loading(html.Div(id="exec-content"), type="dot", delay_show=400)]

    @app.callback(Output("ops-content", "children"),
                  Input("f-section", "value"), Input("f-area", "value"), Input("f-basemap", "value"),
                  Input("theme", "data"), Input("tick", "n_intervals"))
    def ops_content(sections, areas, basemap, theme, _):
        return pages.render_ops(cache.get(), theme or "light", sections, areas, basemap or "street")

    @app.callback(Output("kml-dl", "data"), Input("kml-btn", "n_clicks"),
                  State("f-section", "value"), State("f-area", "value"), prevent_initial_call=True)
    def kml(_, sections, areas):
        ds = cache.get()
        ids = ds.ids(section=sections or None, area=areas or None) if (sections or areas) else None
        stamp = ds.as_of.tz_convert(config.LOCAL_TZ)
        title = f"BSU print stations{' (DEMO DATA)' if ds.is_demo else ''} - {stamp:%Y-%m-%d %H:%M}"
        return dict(content=geo.to_kml(geo.building_points(ds, ids), title),
                    filename=f"bsu-print-stations-{stamp:%Y%m%d-%H%M}.kml", type="application/vnd.google-earth.kml+xml")

    @app.callback(Output("mgmt-content", "children"),
                  Input("f-section", "value"), Input("f-area", "value"), Input("f-period", "value"),
                  Input("f-burn", "value"), Input("theme", "data"))
    def mgmt_content(sections, areas, period, burn, theme):
        return pages.render_management(cache.get(), theme or "light", sections, areas, period or "30",
                                       burn or "per_week")

    @app.callback(Output("exec-content", "children"),
                  Input("f-month", "value"), Input("f-section", "value"), Input("f-area", "value"),
                  Input("theme", "data"))
    def exec_content(month, sections, areas, theme):
        return pages.render_executive(cache.get(), theme or "light", month, sections, areas)

    return app
