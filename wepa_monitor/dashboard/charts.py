"""Plotly figure builders. Every figure takes `theme` and returns a styled go.Figure."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .. import config, support
from .theme import STATUS, TOKENS, ink, layout

SECTION_ORDER = ["ResNet", "Student Computer Labs", "Satellite Campuses"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _section_color(theme: str, section: str) -> str:
    """Color follows the entity: each section keeps its slot whatever the filter."""
    order = SECTION_ORDER + ["Unknown"]
    idx = order.index(section) if section in order else len(order)
    return TOKENS[theme]["series"][idx % 8]


def availability_daily(theme: str, fleet: pd.DataFrame, by_section: pd.DataFrame | None) -> go.Figure:
    t = TOKENS[theme]
    fig = go.Figure()
    if by_section is not None and by_section["section"].nunique() > 1:
        for section in [s for s in SECTION_ORDER if s in set(by_section["section"])]:
            d = by_section[by_section["section"] == section]
            fig.add_scatter(x=d["local_date"], y=d["availability"], name=section, mode="lines",
                            line=dict(width=2, color=_section_color(theme, section)),
                            customdata=np.stack([d["observed_h"], [section] * len(d)], axis=1),
                            hovertemplate="%{y:.1f}%<extra>" + section + "</extra>")
    fig.add_scatter(x=fleet["local_date"], y=fleet["availability"], name="All BSU stations", mode="lines",
                    line=dict(width=2.5, color=t["ink"]),
                    customdata=np.stack([fleet["observed_h"], ["All BSU stations"] * len(fleet)], axis=1),
                    hovertemplate="%{y:.1f}% · %{customdata[0]:,.0f} printer-h observed<extra>All BSU stations</extra>")
    lo = float(np.nanmin(fleet["availability"])) if len(fleet) else 90
    if by_section is not None and len(by_section):
        lo = min(lo, float(np.nanmin(by_section["availability"])))
    fig.update_layout(**layout(theme, 300, hovermode="x unified",
                               yaxis=dict(ticksuffix="%", range=[max(0, lo - 2), 100.5])))
    return fig


def building_bars(theme: str, b: pd.DataFrame) -> go.Figure:
    t = TOKENS[theme]
    b = b.sort_values("any_up", ascending=True)
    fig = go.Figure(go.Bar(
        y=b["building"], x=b["any_up"], orientation="h", marker=dict(color=t["series"][0]),
        customdata=np.stack([b["stations"].fillna(0), b["all_up"]], axis=1),
        hovertemplate="<b>%{y}</b><br>At least one printer up: %{x:.1f}%"
                      "<br>All %{customdata[0]:.0f} printer(s) up: %{customdata[1]:.1f}%<extra></extra>",
    ))
    lo = float(b["any_up"].min()) if len(b) else 90
    fig.update_layout(**layout(theme, max(260, 22 * len(b) + 40),
                               xaxis=dict(ticksuffix="%", range=[max(0, lo - 3), 100], showgrid=True),
                               yaxis=dict(showgrid=False, tickfont=dict(color=t["secondary"]))))
    return fig


def mttr_weekly(theme: str, weekly: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for sev, tone, name in (("red", "critical", "Red (down)"), ("yellow", "warning", "Yellow (warning)")):
        d = weekly[weekly["severity"] == sev]
        if d.empty:
            continue
        fig.add_scatter(x=d["week"], y=d["median_min"], name=name, mode="lines+markers",
                        line=dict(width=2, color=STATUS[tone]), marker=dict(size=8),
                        customdata=np.stack([d["n"], d["mean_min"], [sev] * len(d)], axis=1),
                        hovertemplate="median %{y:.0f} min · mean %{customdata[1]:.0f} min · "
                                      "%{customdata[0]} incidents<extra>" + name + "</extra>")
    fig.update_layout(**layout(theme, 280, hovermode="x unified", yaxis=dict(ticksuffix=" min", rangemode="tozero")))
    return fig


def pareto(theme: str, counts: pd.Series) -> go.Figure:
    t = TOKENS[theme]
    counts = counts.sort_values(ascending=True)
    fig = go.Figure(go.Bar(y=counts.index, x=counts.values, orientation="h", marker=dict(color=t["series"][0]),
                           text=counts.values, textposition="outside", cliponaxis=False,
                           textfont=dict(color=t["secondary"], size=11),
                           customdata=(counts / counts.sum() * 100).values,
                           hovertemplate="<b>%{y}</b><br>%{x} incidents (%{customdata:.0f}%)<extra></extra>"))
    fig.update_layout(**layout(theme, max(220, 30 * len(counts) + 40),
                               xaxis=dict(showgrid=True, title=dict(text="incidents")),
                               yaxis=dict(showgrid=False, tickfont=dict(color=t["secondary"])),
                               margin=dict(r=40)))
    return fig


def _desk_blocks(name: str) -> list[tuple[int, int, float, float]]:
    """Runs of consecutive weekdays with identical desk hours: (first_day, last_day, open, close)."""
    blocks: list[list] = []
    for d in range(7):
        span = support.team(name)["hours"].get(d)
        if span and blocks and blocks[-1][1] == d - 1 and (blocks[-1][2], blocks[-1][3]) == tuple(span):
            blocks[-1][1] = d
        elif span:
            blocks.append([d, d, span[0], span[1]])
    return [tuple(b) for b in blocks]


def heatmap(theme: str, faults: pd.DataFrame, teams: list[str] | None = None) -> go.Figure:
    t = TOKENS[theme]
    grid = (faults.groupby(["weekday", "hour"]).size().unstack(fill_value=0)
            .reindex(index=range(7), columns=range(24), fill_value=0))
    hours = [f"{(h % 12) or 12}{'a' if h < 12 else 'p'}" for h in range(24)]
    scale = [[i / (len(t["seq"]) - 1), c] for i, c in enumerate(t["seq"])]
    fig = go.Figure(go.Heatmap(z=grid.values, x=hours, y=WEEKDAYS, colorscale=scale, xgap=2, ygap=2,
                               colorbar=dict(thickness=10, outlinewidth=0, tickfont=dict(color=t["muted"]),
                                             title=dict(text="faults", font=dict(color=t["muted"], size=11))),
                               hovertemplate="%{y} %{x}: %{z} fault incidents<extra></extra>"))
    # Outline each support team's desk hours so after-hours hot spots stand out.
    styles = [("solid", 0.0), ("dot", 0.12)]
    for i, name in enumerate(teams or []):
        dash, inset = styles[i % len(styles)]
        for d0, d1, a, b in _desk_blocks(name):
            fig.add_shape(type="rect", xref="x", yref="y", x0=a - 0.5 + inset, x1=b - 0.5 - inset,
                          y0=d0 - 0.5 + inset, y1=d1 + 0.5 - inset, line=dict(color=t["ink"], width=1.6, dash=dash),
                          fillcolor="rgba(0,0,0,0)", layer="above")
        fig.add_scatter(x=[None], y=[None], mode="lines", name=f"{name} desk hours", hoverinfo="skip",
                        line=dict(color=t["ink"], width=1.6, dash=dash))
    fig.update_layout(**layout(theme, 260 + (28 if teams else 0), showlegend=bool(teams),
                               legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0),
                               margin=dict(t=36 if teams else 10),
                               xaxis=dict(showline=False, type="category", categoryorder="array", categoryarray=hours,
                                          tickangle=0, tickmode="array", tickvals=hours[::3], ticktext=hours[::3]),
                               yaxis=dict(autorange="reversed", showgrid=False, type="category",
                                          categoryorder="array", categoryarray=WEEKDAYS)))
    return fig


def owner_hours(theme: str, summ: pd.DataFrame) -> go.Figure:
    """Per support team: printer-hours down inside vs outside desk hours."""
    t = TOKENS[theme]
    fig = go.Figure()
    summ = summ.iloc[::-1]
    tot = (summ["staffed_h"] + summ["after_h"]).replace(0, np.nan)
    for col, part, name, color in (("staffed_h", "staffed", "During desk hours", t["series"][0]),
                                   ("after_h", "after", "After hours", t["muted"])):
        share = (summ[col] / tot * 100).fillna(0)
        fig.add_bar(y=summ["owner"], x=summ[col], orientation="h", name=name, marker=dict(color=color),
                    text=[f"{v:.0f}%" if v >= 14 else "" for v in share], textposition="inside",
                    insidetextanchor="middle", textfont=dict(color="#ffffff", size=11),
                    customdata=np.stack([summ["owner"], [part] * len(summ), share, summ["hours"]], axis=1),
                    hovertemplate="<b>%{y}</b> (%{customdata[3]})<br>" + name +
                                  ": %{x:,.0f} printer-hours down (%{customdata[2]:.0f}%)<extra></extra>")
    fig.update_layout(**layout(theme, 70 * len(summ) + 90, barmode="stack", bargap=0.35,
                               legend=dict(orientation="h", y=1.15, x=0, traceorder="normal"),
                               xaxis=dict(showgrid=True, title=dict(text="printer-hours down")),
                               yaxis=dict(showgrid=False, tickfont=dict(color=t["secondary"]))))
    return fig


def cumulative(theme: str, cum: pd.DataFrame, components: list[str]) -> go.Figure:
    t = TOKENS[theme]
    fig = go.Figure()
    comps = [c for c in components if c in cum]
    for comp in comps:
        name = config.COMPONENT_LABELS[comp]
        fig.add_scatter(x=cum["local_date"], y=cum[comp], name=name, mode="lines",
                        line=dict(width=2, color=ink(theme, comp)), customdata=[comp] * len(cum),
                        hovertemplate="%{y:.2f} parts<extra>" + name + "</extra>")
    if len(cum) and comps:
        # End labels, nudged apart when lines finish close together (13 px minimum spacing).
        ends = sorted(((cum[c].iloc[-1], c) for c in comps), reverse=True)
        top = max(v for v, _ in ends) or 1.0
        px_per_unit = 170 / top
        last_px = None
        for value, comp in ends:
            px = value * px_per_unit
            shift = 0 if last_px is None or last_px - px >= 13 else (last_px - 13) - px
            last_px = px + shift
            fig.add_annotation(x=cum["local_date"].iloc[-1], y=value, yshift=shift, showarrow=False,
                               xanchor="left", xshift=6, font=dict(size=11, color=t["secondary"]),
                               text=f"{config.COMPONENT_LABELS[comp].split()[-1]} {value:.1f}")
    fig.update_layout(**layout(theme, 240, hovermode="x unified", showlegend=len(components) > 1,
                               margin=dict(r=56), yaxis=dict(rangemode="tozero", title=dict(text="parts used"))))
    return fig


def daily_quality(theme: str, q: pd.DataFrame, field: str, label: str, as_bar: bool) -> go.Figure:
    t = TOKENS[theme]
    if as_bar:
        trace = go.Bar(x=q["local_date"], y=q[field], marker=dict(color=t["series"][0]),
                       hovertemplate="%{x|%b %d}: %{y:.2f}%<extra>" + label + "</extra>")
    else:
        trace = go.Scatter(x=q["local_date"], y=q[field], mode="lines", line=dict(width=2, color=t["series"][0]),
                           hovertemplate="%{x|%b %d}: %{y:.1f}%<extra>" + label + "</extra>")
    fig = go.Figure(trace)
    fig.update_layout(**layout(theme, 220, yaxis=dict(ticksuffix="%", rangemode="tozero"), bargap=0.2))
    return fig


def monthly_bars(theme: str, labels: list[str], values: list[float], suffix: str, partial: list[bool]) -> go.Figure:
    t = TOKENS[theme]
    colors = [t["neutral_bar"] if p else t["series"][0] for p in partial]
    fig = go.Figure(go.Bar(x=labels, y=values, marker=dict(color=colors),
                           text=[f"{v:.1f}{suffix}" if v is not None and np.isfinite(v) else "" for v in values],
                           textposition="outside", cliponaxis=False, textfont=dict(color=t["secondary"], size=11),
                           hovertemplate="%{x}: %{y:.2f}" + suffix + "<extra></extra>"))
    finite = [v for v in values if v is not None and np.isfinite(v)]
    lo = min(finite) if finite else 0
    yaxis = dict(ticksuffix=suffix, range=[max(0, lo - 3), 100.5]) if suffix == "%" else dict(rangemode="tozero")
    fig.update_layout(**layout(theme, 240, yaxis=yaxis, margin=dict(t=24)))
    return fig


def monthly_stacked(theme: str, frame: pd.DataFrame, components: list[str]) -> go.Figure:
    t = TOKENS[theme]
    fig = go.Figure()
    for comp in components:
        name = config.COMPONENT_LABELS[comp]
        fig.add_bar(x=frame["month"], y=frame[comp], name=name, customdata=[comp] * len(frame),
                    marker=dict(color=ink(theme, comp), line=dict(color=t["surface"], width=2)),
                    hovertemplate="%{y:.1f} parts<extra>" + name + "</extra>")
    fig.update_layout(**layout(theme, 260, barmode="stack", hovermode="x unified", barcornerradius=0,
                               yaxis=dict(title=dict(text="parts used"))))
    return fig


ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
MAP_STATES = [("red", "critical", "Down"), ("yellow", "warning", "Warning"),
              ("stale", "serious", "No data"), ("green", "good", "Printing")]


def _label_positions(pts: pd.DataFrame, zoom: float = 15.6) -> list[str]:
    """Greedy label placement: try four positions around each marker and take the first whose
    estimated text box clears every marker and every label already placed."""
    deg_lon = 360 / (512 * 2 ** zoom)                     # degrees per pixel (MapLibre: 512 px tiles)
    deg_lat = deg_lon * np.cos(np.radians(float(pts["lat"].mean()) if len(pts) else 42.0))
    lat, lon = pts["lat"].to_numpy(), pts["lon"].to_numpy()

    def box(i, pos):
        name = str(pts["short_name"].iloc[i])
        lines = 1 if len(name) <= 16 else 2                # MapLibre wraps long labels
        w = min(len(name), 16) * 6.6 * deg_lon
        h = (lines * 16 + 4) * deg_lat                    # generous: MapLibre adds collision padding
        gap = 7 * deg_lon
        x0 = lon[i] + gap if "right" in pos else lon[i] - gap - w
        # MapLibre's "top" anchor still lets the text hang ~10 px below the marker centre.
        overhang = 10 * deg_lat
        y0 = lat[i] - overhang if "top" in pos else lat[i] - h + overhang
        return x0 - 3 * deg_lon, y0 - 3 * deg_lat, x0 + w + 3 * deg_lon, y0 + h + 3 * deg_lat

    def overlaps(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    r = 7
    markers = [(lon[i] - r * deg_lon, lat[i] - r * deg_lat, lon[i] + r * deg_lon, lat[i] + r * deg_lat)
               for i in range(len(pts))]
    def area(a, b):
        return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))

    placed, out = [], [""] * len(pts)
    for i in np.argsort(-lat):
        # First position with no overlap; failing that, the one with the least overlapped area.
        best, best_cost = "top right", None
        for pos in ("top right", "bottom right", "top left", "bottom left"):
            b = box(i, pos)
            cost = sum(area(b, m) for j, m in enumerate(markers) if j != i) + sum(area(b, q) for q in placed)
            if best_cost is None or cost < best_cost:
                best, best_cost = pos, cost
            if cost == 0:
                break
        placed.append(box(i, best))
        out[i] = best
    return out


def campus_map(theme: str, points: pd.DataFrame, basemap: str = "street") -> go.Figure:
    """Buildings as status-colored markers on a street map or satellite imagery.

    Status is never color alone: each state is its own legend entry, and the hover
    names the state of every station in the building.
    """
    t = TOKENS[theme]
    satellite = basemap == "satellite"
    pts = points.dropna(subset=["lat", "lon"])
    pts = pts.assign(target=pts["target"] if "target" in pts else "",
                     size=12 + 5 * (pts["stations"].clip(upper=3) - 1),
                     textpos=_label_positions(pts),
                     hover=pts.apply(lambda p: f"<b>{p['building']}</b> · {p['area']}<br>" +
                                     "<br>".join(p["lines"]), axis=1))
    fig = go.Figure()
    ring = "#ffffff" if satellite or theme == "light" else t["surface"]
    fig.add_trace(go.Scattermap(lat=pts["lat"], lon=pts["lon"], mode="markers", hoverinfo="skip",
                                marker=dict(size=pts["size"] + 5, color=ring, opacity=1), showlegend=False))
    for state, tone, label in MAP_STATES:
        group = pts[pts["state"] == state]
        # Scattermap takes one label position per trace, so split by position under one legend entry.
        for i, (pos, d) in enumerate(group.groupby("textpos", sort=False)):
            fig.add_trace(go.Scattermap(
                lat=d["lat"], lon=d["lon"], mode="markers+text", name=label,
                legendgroup=state, showlegend=i == 0,
                marker=dict(size=d["size"], color=STATUS[tone], opacity=1),
                text=d["short_name"], textposition=pos,
                textfont=dict(size=12 if satellite else 11,
                              family="Open Sans Bold" if satellite else "Open Sans Regular",
                              color="#ffffff" if satellite or theme == "dark" else t["ink"]),
                customdata=np.stack([d["hover"], d["target"]], axis=1),
                hovertemplate="%{customdata[0]}<br><i>Click to open this building</i><extra></extra>"))
    main = pts[pts["campus"] == "Main"] if (pts["campus"] == "Main").any() else pts
    center = dict(lat=float(main["lat"].mean()), lon=float(main["lon"].mean())) if len(main) else \
        dict(lat=41.9873, lon=-70.9680)
    map_layout = dict(center=center, zoom=15.6)
    if satellite:
        # Imagery, then a light dark wash so white labels stay legible over bright rooftops.
        wash = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
            [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]]]}}
        map_layout.update(style="white-bg", layers=[
            dict(below="traces", sourcetype="raster", source=[ESRI_IMAGERY],
                 sourceattribution="Imagery © Esri, Maxar, Earthstar Geographics"),
            dict(below="traces", sourcetype="geojson", source=wash, type="fill", color="#000000", opacity=0.28),
        ])
    else:
        map_layout.update(style="carto-darkmatter" if theme == "dark" else "carto-positron")
    fig.update_layout(**layout(theme, 480, margin=dict(l=0, r=0, t=0, b=0), map=map_layout,
                               legend=dict(x=0.01, y=0.99, yanchor="top", bgcolor=t["surface"],
                                           bordercolor=t["border"], borderwidth=1,
                                           font=dict(color=t["ink"]))))
    return fig


TIMELINE_STATES = [("red", "critical", "Down"), ("yellow", "warning", "Warning"),
                   ("green", "good", "Printing"), ("nodata", None, "No data")]


def status_timeline(theme: str, segments: pd.DataFrame, start=None, end=None, owner: str | None = None) -> go.Figure:
    """One horizontal band: what state the station was in, when. segments: start, end, state.
    With an owner, that team's desk hours are shaded behind the band."""
    t = TOKENS[theme]
    fig = go.Figure()
    if owner and start is not None and end is not None:
        day = start.tz_convert(config.LOCAL_TZ).normalize()
        last = end.tz_convert(config.LOCAL_TZ)
        while day <= last:
            span = support._span(owner, day)
            if span:
                fig.add_vrect(x0=span[0].tz_localize(None), x1=span[1].tz_localize(None), fillcolor=t["grid"],
                              opacity=0.9, line_width=0, layer="below")
            day = (day + pd.Timedelta(days=1, hours=2)).normalize()
        fig.add_scatter(x=[None], y=[None], mode="markers", name=f"{owner} desk hours", hoverinfo="skip",
                        marker=dict(symbol="square", size=11, color=t["grid"], line=dict(color=t["axis"], width=1)))
    for state, tone, label in TIMELINE_STATES:
        d = segments[segments["state"] == state]
        if d.empty:
            continue
        dur_ms = (d["end"] - d["start"]).dt.total_seconds() * 1000
        local_s = d["start"].dt.tz_convert(config.LOCAL_TZ)
        local_e = d["end"].dt.tz_convert(config.LOCAL_TZ)
        fig.add_bar(y=["Status"] * len(d), x=dur_ms, base=local_s.dt.tz_localize(None), orientation="h",
                    name=label, marker=dict(color=STATUS[tone] if tone else t["neutral_bar"], line=dict(width=0)),
                    customdata=np.stack([local_s.dt.strftime("%a %b %-d %-I:%M %p"),
                                         local_e.dt.strftime("%a %b %-d %-I:%M %p"),
                                         ((d["end"] - d["start"]).dt.total_seconds() / 60).round(),
                                         [state] * len(d), d["start"].map(lambda x: x.isoformat()),
                                         d["end"].map(lambda x: x.isoformat())], axis=1),
                    hovertemplate=label + ": %{customdata[0]} → %{customdata[1]} (%{customdata[2]:,.0f} min)"
                                  "<extra></extra>")
    fig.update_layout(**layout(theme, 150, barmode="overlay", bargap=0.15, barcornerradius=0,
                               xaxis=dict(type="date", showgrid=True, range=None if start is None else [
                                   start.tz_convert(config.LOCAL_TZ).tz_localize(None),
                                   end.tz_convert(config.LOCAL_TZ).tz_localize(None)]),
                               yaxis=dict(showticklabels=False, showgrid=False, type="category"),
                               legend=dict(y=1.08), margin=dict(t=30)))
    return fig


def levels_over_time(theme: str, points: pd.DataFrame, repl: pd.DataFrame, components: list[str],
                     start, end) -> go.Figure:
    """Step lines of each part's level; triangles mark detected replacements."""
    t = TOKENS[theme]
    fig = go.Figure()
    for comp in components:
        d = points[points["component"] == comp]
        if d.empty:
            continue
        name = config.COMPONENT_LABELS[comp]
        x = d["scrape_ts"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None)
        fig.add_scatter(x=x, y=d["level"], mode="lines", line=dict(width=2, color=ink(theme, comp), shape="hv"),
                        name=name, customdata=[[comp, ""]] * len(d),
                        hovertemplate="%{y:.0f}%<extra>" + name + "</extra>")
        r = repl[repl["component"] == comp]
        if len(r):
            fig.add_scatter(x=r["ts"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None), y=r["level_after"],
                            mode="markers", showlegend=False,
                            marker=dict(symbol="triangle-up", size=10, color=ink(theme, comp),
                                        line=dict(color=t["surface"], width=2)),
                            customdata=np.stack([[comp] * len(r), r["level_before"]], axis=1),
                            hovertemplate="Replaced (old part had %{customdata[1]:.0f}% left)<extra>" + name + "</extra>")
    fig.update_layout(**layout(theme, 230, hovermode="x unified",
                               xaxis=dict(range=[start.tz_convert(config.LOCAL_TZ).tz_localize(None),
                                                 end.tz_convert(config.LOCAL_TZ).tz_localize(None)]),
                               yaxis=dict(range=[0, 104], ticksuffix="%")))
    return fig


def hour_bars(theme: str, hours: pd.Series, label: str) -> go.Figure:
    """Counts by local hour of day (single series)."""
    t = TOKENS[theme]
    counts = hours.value_counts().reindex(range(24), fill_value=0)
    names = [f"{(h % 12) or 12}{'a' if h < 12 else 'p'}" for h in range(24)]
    fig = go.Figure(go.Bar(x=names, y=counts.values, marker=dict(color=t["series"][0]),
                           hovertemplate="%{x}: %{y} " + label + "<extra></extra>"))
    small = counts.max() <= 10
    fig.update_layout(**layout(theme, 220, bargap=0.25,
                               yaxis=dict(rangemode="tozero", dtick=1 if small else None, tickformat=",d")))
    return fig


# --- statistical models ----------------------------------------------------------------------------

def eol_projection(theme: str, points: pd.DataFrame, fit: dict, component: str, as_of, floor: float) -> go.Figure:
    """Scatter of a part's readings since its last replacement, the Theil-Sen line of best fit,
    and its extension to the replacement point (dashed only where it is a projection)."""
    t = TOKENS[theme]
    color = ink(theme, component)
    local = lambda s: s.dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None)  # noqa: E731
    fig = go.Figure()
    fig.add_scatter(x=local(points["scrape_ts"]), y=points["level"], mode="markers", name="Readings",
                    marker=dict(size=8, color=color, opacity=0.55, line=dict(color=t["surface"], width=1)),
                    hovertemplate="%{x|%b %-d %-I:%M %p}: %{y:.0f}%<extra>Reading</extra>")
    t0 = points["scrape_ts"].min()
    days_back = (t0 - as_of).total_seconds() / 86400
    days_fwd = min(fit["days"], 120) if np.isfinite(fit["days"]) else 30
    xs_hist = np.linspace(days_back, 0, 20)
    xs_proj = np.linspace(0, days_fwd, 20)
    to_ts = lambda d: (as_of + pd.to_timedelta(d, unit="D")).tz_convert(config.LOCAL_TZ).tz_localize(None)  # noqa
    fig.add_scatter(x=[to_ts(d) for d in xs_hist], y=fit["intercept"] + fit["slope_per_day"] * xs_hist,
                    mode="lines", name="Best fit", line=dict(color=t["ink"], width=2),
                    hovertemplate="Fit: %{y:.1f}%<extra></extra>")
    fig.add_scatter(x=[to_ts(d) for d in xs_proj], y=fit["intercept"] + fit["slope_per_day"] * xs_proj,
                    mode="lines", name="Projection", line=dict(color=t["ink"], width=2, dash="dot"),
                    hovertemplate="Projected: %{y:.1f}%<extra></extra>")
    fig.add_hline(y=floor, line=dict(color=STATUS["critical"], width=1), annotation_text=f"replace at {floor:.0f}%",
                  annotation_position="bottom right", annotation_font=dict(size=11, color=t["secondary"]))
    if np.isfinite(fit["days"]) and fit["days"] <= 120:
        eol = to_ts(fit["days"])
        fig.add_scatter(x=[eol], y=[floor], mode="markers", name="Projected end of life",
                        marker=dict(size=11, symbol="diamond", color=STATUS["critical"],
                                    line=dict(color=t["surface"], width=2)),
                        hovertemplate="End of life ≈ %{x|%a %b %-d}<extra></extra>")
    fig.update_layout(**layout(theme, 260, yaxis=dict(range=[0, 104], ticksuffix="%"), showlegend=True))
    return fig


def control_chart(theme: str, cc) -> go.Figure:
    t = TOKENS[theme]
    d = cc.daily
    fig = go.Figure()
    fig.add_scatter(x=d["local_date"], y=d["count"], mode="lines+markers", name="Faults per day",
                    line=dict(color=t["series"][0], width=2), marker=dict(size=7, color=t["series"][0]),
                    hovertemplate="%{x|%a %b %-d}: %{y} faults<extra></extra>")
    out = d[d["out"]]
    if len(out):
        fig.add_scatter(x=out["local_date"], y=out["count"], mode="markers", name="Out of control",
                        marker=dict(size=13, symbol="circle-open", color=STATUS["critical"], line=dict(width=2.5)),
                        hovertemplate="%{x|%a %b %-d}: %{y} faults, beyond the limit<extra></extra>")
    for y, label in ((cc.center, f"average {cc.center:.1f}"), (cc.ucl, f"upper limit {cc.ucl:.1f}")):
        fig.add_hline(y=y, line=dict(color=t["secondary"] if y == cc.center else STATUS["critical"], width=1),
                      annotation_text=label, annotation_position="top left",
                      annotation_font=dict(size=11, color=t["secondary"]))
    if cc.lcl > 0:
        fig.add_hline(y=cc.lcl, line=dict(color=STATUS["critical"], width=1), annotation_text=f"lower {cc.lcl:.1f}",
                      annotation_position="bottom left", annotation_font=dict(size=11, color=t["secondary"]))
    fig.update_layout(**layout(theme, 280, yaxis=dict(rangemode="tozero"), hovermode="closest"))
    return fig


def km_curves(theme: str, ttf) -> go.Figure:
    t = TOKENS[theme]
    fig = go.Figure()
    for i, (name, km) in enumerate(ttf.curves.items()):
        x = np.r_[km["t"].to_numpy(), max(km["t"].max(), 24)]
        y = np.r_[km["survival"].to_numpy(), km["survival"].iloc[-1]] * 100
        med = ttf.medians[name]
        med_txt = f", median {med:.1f} h" if np.isfinite(med) else ""
        fig.add_scatter(x=x, y=y, mode="lines", line=dict(shape="hv", width=2.5, color=t["series"][i]),
                        name=f"{name} (n={ttf.n[name]}{med_txt})", customdata=[name] * len(x),
                        hovertemplate="After %{x:.1f} h: %{y:.0f}% still down<extra>" + name + "</extra>")
        if np.isfinite(med):   # where the curve crosses 50%
            fig.add_scatter(x=[med], y=[50], mode="markers", showlegend=False, hoverinfo="skip",
                            marker=dict(size=9, color=t["series"][i], line=dict(color=t["surface"], width=2)))
    fig.add_hline(y=50, line=dict(color=t["grid"], width=1))
    fig.update_layout(**layout(theme, 300, xaxis=dict(title=dict(text="hours since the station went down"),
                                                      range=[0, 24], showgrid=True),
                               yaxis=dict(ticksuffix="%", range=[0, 102], title=dict(text="still down")),
                               legend=dict(y=1.02)))
    return fig


def usage_scatter(theme: str, uf) -> go.Figure:
    t = TOKENS[theme]
    p = uf.points
    fig = go.Figure()
    fig.add_scatter(x=np.r_[uf.band["x"], uf.band["x"][::-1]], y=np.r_[uf.band["hi"], uf.band["lo"][::-1]],
                    fill="toself", fillcolor=t["grid"], line=dict(width=0), name="95% confidence band",
                    hoverinfo="skip")
    fig.add_scatter(x=uf.band["x"], y=uf.band["fit"], mode="lines", line=dict(color=t["ink"], width=2),
                    name=f"Best fit (R² = {uf.r2:.2f})", hoverinfo="skip")
    normal = p[~p["outlier"]]
    fig.add_scatter(x=normal["usage"], y=normal["failures_per_week"], mode="markers", name="Station",
                    marker=dict(size=10, color=t["series"][0], line=dict(color=t["surface"], width=2)),
                    customdata=np.stack([normal["label"], normal["station_id"]], axis=1),
                    hovertemplate="%{customdata[0]}<br>%{x:.2f} pts toner/day · %{y:.1f} failures/week<extra></extra>")
    out = p[p["outlier"]]
    if len(out):
        fig.add_scatter(x=out["usage"], y=out["failures_per_week"], mode="markers+text", name="Fails more than usage explains",
                        marker=dict(size=12, color=STATUS["critical"], symbol="diamond",
                                    line=dict(color=t["surface"], width=2)),
                        text=out["label"].str.replace(r" \(\d+\)$", "", regex=True), textposition="top center",
                        textfont=dict(size=11, color=t["secondary"]),
                        customdata=np.stack([out["label"], out["station_id"]], axis=1),
                        hovertemplate="%{customdata[0]}<br>%{x:.2f} pts toner/day · %{y:.1f} failures/week<extra></extra>")
    fig.update_layout(**layout(theme, 320, hovermode="closest",
                               xaxis=dict(title=dict(text="usage: black toner burned per day (pts)"), showgrid=True),
                               yaxis=dict(title=dict(text="red incidents per week"),
                                          range=[0, float(p["failures_per_week"].max()) * 1.2 + 0.2]),
                               margin=dict(t=30)))
    return fig
