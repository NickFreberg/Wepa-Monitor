"""Plotly figure builders. Every figure takes `theme` and returns a styled go.Figure."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .. import config
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
                            hovertemplate="%{y:.1f}%<extra>" + section + "</extra>")
    fig.add_scatter(x=fleet["local_date"], y=fleet["availability"], name="Fleet", mode="lines",
                    line=dict(width=2.5, color=t["ink"]),
                    customdata=fleet["observed_h"],
                    hovertemplate="%{y:.1f}% · %{customdata:,.0f} printer-h observed<extra>Fleet</extra>")
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
                        customdata=np.stack([d["n"], d["mean_min"]], axis=1),
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


def heatmap(theme: str, faults: pd.DataFrame) -> go.Figure:
    t = TOKENS[theme]
    grid = (faults.groupby(["weekday", "hour"]).size().unstack(fill_value=0)
            .reindex(index=range(7), columns=range(24), fill_value=0))
    hours = [f"{(h % 12) or 12}{'a' if h < 12 else 'p'}" for h in range(24)]
    scale = [[i / (len(t["seq"]) - 1), c] for i, c in enumerate(t["seq"])]
    fig = go.Figure(go.Heatmap(z=grid.values, x=hours, y=WEEKDAYS, colorscale=scale, xgap=2, ygap=2,
                               colorbar=dict(thickness=10, outlinewidth=0, tickfont=dict(color=t["muted"]),
                                             title=dict(text="faults", font=dict(color=t["muted"], size=11))),
                               hovertemplate="%{y} %{x}: %{z} fault incidents<extra></extra>"))
    fig.update_layout(**layout(theme, 260, xaxis=dict(showline=False), yaxis=dict(autorange="reversed",
                                                                                    showgrid=False)))
    return fig


def cumulative(theme: str, cum: pd.DataFrame, components: list[str]) -> go.Figure:
    t = TOKENS[theme]
    fig = go.Figure()
    comps = [c for c in components if c in cum]
    for comp in comps:
        name = config.COMPONENT_LABELS[comp]
        fig.add_scatter(x=cum["local_date"], y=cum[comp], name=name, mode="lines",
                        line=dict(width=2, color=ink(theme, comp)),
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
        fig.add_bar(x=frame["month"], y=frame[comp], name=name,
                    marker=dict(color=ink(theme, comp), line=dict(color=t["surface"], width=2)),
                    hovertemplate="%{y:.1f} parts<extra>" + name + "</extra>")
    fig.update_layout(**layout(theme, 260, barmode="stack", hovermode="x unified", barcornerradius=0,
                               yaxis=dict(title=dict(text="parts used"))))
    return fig
