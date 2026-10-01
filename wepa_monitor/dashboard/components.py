"""Small building blocks: headlines, stat tiles, cards, tables, status pills, links."""
from __future__ import annotations

import math
import numbers

import numpy as np

import pandas as pd
from dash import dcc, html

from ..metrics import Metric
from .theme import GRAPH_CONFIG, STATE_STYLE


def fmt_num(v, digits=1, suffix=""):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return f"{v:,.{digits}f}{suffix}"


def fmt_minutes(m):
    if m is None or (isinstance(m, float) and not math.isfinite(m)):
        return "—"
    if m < 90:
        return f"{m:,.0f} min"
    if m < 48 * 60:
        return f"{m / 60:,.1f} h"
    return f"{m / 1440:,.1f} d"


def fmt_hours(h):
    if h is None or (isinstance(h, float) and not math.isfinite(h)):
        return "—"
    return f"{h:,.0f} h" if h < 96 else f"{h / 24:,.1f} d"


def icon(name: str, label: str | None = None) -> html.Span:
    """Monochrome SVG icon drawn by CSS (mask), so it takes the current text color."""
    attrs = {"role": "img", "aria-label": label} if label else {"aria-hidden": "true"}
    return html.Span(className=f"icon icon--{name}", **attrs)


def headline(tone: str, title: str, detail: str = "") -> html.Div:
    """The one-sentence answer at the top of a page: what is going on, in plain words."""
    glyph = {"good": "check", "warning": "alert", "critical": "x", "serious": "alert", "info": "info"}[tone]
    return html.Div(className=f"headline headline--{tone}", role="status", children=[
        html.Span(icon(glyph), className="headline__icon"),
        html.Div([html.Div(title, className="headline__title"),
                  html.Div(detail, className="headline__detail") if detail else None]),
    ])


def station_link(station_id: str, text: str, className: str = "link") -> dcc.Link:
    return dcc.Link(text, href=f"/station/{station_id}", className=className)


def tile(label: str, value: str, sub: str = "", ok: bool = True, help_text: str = "",
         tone: str | None = None, compare: str = "", href: str | None = None):
    classes = "tile" + ("" if ok else " tile--muted") + (f" tile--{tone}" if tone else "")
    children = [
        html.Div(label, className="tile__label"),
        html.Div(value, className="tile__value"),
        html.Div(compare, className="tile__compare") if compare else None,
        html.Div(sub, className="tile__sub"),
    ]
    if href:
        return dcc.Link(children, href=href, className=classes + " tile--link", title=help_text)
    return html.Div(className=classes, title=help_text, children=children)


def metric_tile(label: str, m: Metric, formatter, help_text: str = "", unit_note: str = "") -> html.Div:
    if m.value is None:
        return tile(label, "—", m.note or "insufficient data", ok=False, help_text=help_text)
    sub = m.note if not m.ok else unit_note
    return tile(label, formatter(m.value), sub, ok=m.ok, help_text=help_text)


def status_pill(state: str) -> html.Span:
    tone, icon, text = STATE_STYLE.get(state, STATE_STYLE["stale"])
    return html.Span(className=f"pill pill--{tone}", children=[
        html.Span(icon, className="pill__icon", **{"aria-hidden": "true"}), text])


def data_table(df: pd.DataFrame, columns: list[tuple[str, str, object]], max_rows: int = 200,
               empty: str = "Nothing to show.", link_col: tuple[str, str] | None = None) -> html.Div:
    """columns: (field, header, formatter or None). Numbers right-aligned with tabular figures.
    link_col=(display field, station id field) turns that column into links to the station page."""
    if df is None or df.empty:
        return html.Div(empty, className="empty")
    head = html.Thead(html.Tr([html.Th(h) for _, h, _ in columns]))
    rows = []
    for _, r in df.head(max_rows).iterrows():
        cells = []
        for field, _, fmt in columns:
            v = r.get(field)
            if callable(fmt):
                content = fmt(v)
            else:
                content = "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
            if link_col and field == link_col[0] and r.get(link_col[1]):
                content = station_link(str(r.get(link_col[1])), content)
            numeric = isinstance(v, numbers.Number) and not isinstance(v, (bool, np.bool_))
            cells.append(html.Td(content, className="num" if numeric else None))
        rows.append(html.Tr(cells))
    return html.Div(className="table-wrap", children=html.Table([head, html.Tbody(rows)], className="table"))


def chart_card(title: str, subtitle: str, figure=None, body=None, table: html.Div | None = None,
               wide: bool = False, note: str = "", graph_id: str | None = None, action=None) -> html.Section:
    head = [html.Div([html.H3(title), html.P(subtitle, className="card__sub") if subtitle else None])]
    if action is not None:
        head.append(html.Div(action, className="card__action"))
    children = [html.Header(head, className="card__head")]
    if figure is not None:
        height = figure.layout.height or 300
        extra = {"id": graph_id} if graph_id else {}
        children.append(dcc.Graph(figure=figure, config=GRAPH_CONFIG, className="graph",
                                  style={"height": f"{height}px"}, **extra))
    if body is not None:
        children.append(body)
    if note:
        children.append(html.P(note, className="card__note"))
    if table is not None:
        children.append(html.Details([html.Summary("Show data"), table], className="card__data"))
    return html.Section(children, className="card" + (" card--wide" if wide else ""))


def level_bar(label: str, level, low: int = 10, critical: int = 5) -> html.Div:
    if level is None or (isinstance(level, float) and math.isnan(level)):
        pct, text, tone = 0, "—", "none"
    else:
        pct, text = max(0.0, min(100.0, float(level))), f"{level:.0f}"
        tone = "critical" if level <= critical else ("warning" if level <= low else "ok")
    return html.Div(className=f"lvl lvl--{tone}", title=f"{label}: {text}%", children=[
        html.Span(label, className="lvl__label"),
        html.Span(className="lvl__track", children=html.Span(className="lvl__fill", style={"width": f"{pct}%"})),
        html.Span(text, className="lvl__value"),
    ])


def segmented(id_, options: list[dict], value, multi: bool = False, persistence: str | None = None, **kw):
    """Pill-style segmented control (radio) or chip set (checklist)."""
    comp = dcc.Checklist if multi else dcc.RadioItems
    extra = {"persistence": True, "persistence_type": persistence} if persistence else {}
    return comp(id=id_, options=options, value=value, inline=True, className="seg" + (" seg--multi" if multi else ""),
                labelClassName="seg__opt", inputClassName="seg__input", **extra, **kw)


def section_title(text: str, sub: str = "") -> html.Div:
    return html.Div([html.H2(text, className="section-title"),
                     html.P(sub, className="section-sub") if sub else None], className="section-head")
