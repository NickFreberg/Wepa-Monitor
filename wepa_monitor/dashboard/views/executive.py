"""Executive summary: month vs prior month vs year to date, plus generated observations."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dash import html

from ... import config, insights, metrics as M, narrative as N
from .. import charts
from ..components import chart_card, data_table, fmt_hours, fmt_minutes, fmt_num, headline, prose
from .common import empty, scope_ids


def _delta(cur, prev, higher_is_better: bool | None, pct_points=False):
    """Change vs prior period. higher_is_better=None means neutral (volume, not performance)."""
    if cur is None or prev is None or not np.isfinite(cur) or not np.isfinite(prev):
        return html.Span("—", className="delta")
    d = cur - prev
    if pct_points:
        text, negligible = f"{abs(d):.1f} pts", abs(d) < 0.05
    elif prev:
        ratio = cur / prev
        text = f"{ratio:.1f}×" if ratio >= 2 else f"{abs(ratio - 1):.0%}"
        negligible = abs(ratio - 1) < 0.005
    else:
        text, negligible = f"{abs(d):.1f}", abs(d) < 1e-9
    if negligible:
        return html.Span("no change", className="delta")
    arrow = "▲" if d > 0 else "▼"
    if higher_is_better is None:
        return html.Span([html.Span(arrow, **{"aria-hidden": "true"}), f" {text}"], className="delta")
    good = (d > 0) == higher_is_better
    return html.Span([html.Span(arrow, **{"aria-hidden": "true"}), f" {text}",
                      html.Span(" better" if good else " worse", className="sr")],
                     className=f"delta delta--{'good' if good else 'bad'}")


def month_options(ds: M.Dataset) -> list[dict]:
    return [{"label": p.label.replace(" (to date)", "*"), "value": i} for i, p in enumerate(insights.months(ds))]


def default_month(ds: M.Dataset) -> int | None:
    ms = insights.months(ds)
    if not ms:
        return None
    return len(ms) - 2 if len(ms) > 1 and ms[-1].label.endswith("(to date)") else len(ms) - 1


def render(ds: M.Dataset, theme: str, month_idx, scope):
    if ds.empty:
        return empty("No data yet.")
    ids = scope_ids(ds, scope)
    ms = insights.months(ds)
    month_idx = default_month(ds) if month_idx is None or month_idx >= len(ms) else month_idx
    cur = ms[month_idx]
    prev = ms[month_idx - 1] if month_idx > 0 else None
    ytd = insights.ytd(ds)
    sc_cur, sc_ytd = insights.scorecard(ds, cur, ids), insights.scorecard(ds, ytd, ids)
    sc_prev = insights.scorecard(ds, prev, ids) if prev else None

    def val(sc, key):
        if sc is None:
            return None
        v = sc[key]
        return v.value if isinstance(v, M.Metric) else v

    def units(sc, comps):
        return None if sc is None else sum(sc["units"].get(c, 0.0) for c in comps)

    rows = [
        ("Availability", "availability", lambda v: fmt_num(v, 2, "%"), True, True),
        ("Red incidents", "red_incidents", lambda v: fmt_num(v, 0), False, False),
        ("Mean time to repair (MTTR) · down", "mttr_red", fmt_minutes, False, False),
        ("Mean time to clear · warnings", "mttr_yellow", fmt_minutes, False, False),
        ("Outages begun after desk hours", "after_hours", lambda v: fmt_num(v, 0, "%"), False, True),
        ("Desk time to fix (median)", "desk_fix", fmt_minutes, False, False),
        ("Mean time between failures (MTBF)", "mtbf", fmt_hours, True, False),
        ("Paper refill time", "paper", fmt_minutes, False, False),
        ("Data quality score", "dq", lambda v: fmt_num(v, 0, " / 100"), True, True),
    ]
    body = []
    for label, key, fmt, hib, pts in rows:
        c, p, y = val(sc_cur, key), val(sc_prev, key), val(sc_ytd, key)
        body.append(html.Tr([html.Th(label, scope="row"), html.Td(fmt(c), className="num"),
                             html.Td(fmt(p) if prev else "—", className="num"),
                             html.Td(_delta(c, p, hib, pts) if prev else "—", className="num"),
                             html.Td(fmt(y), className="num")]))
    for label, comps in (("Black toner used (parts)", ["toner_k"]),
                         ("Color toner used (parts)", ["toner_c", "toner_m", "toner_y"]),
                         ("Drums used (parts)", ["drum_k", "drum_c", "drum_m", "drum_y"]),
                         ("Belts + fusers used (parts)", ["belt", "fuser"])):
        c, p, y = units(sc_cur, comps), units(sc_prev, comps), units(sc_ytd, comps)
        body.append(html.Tr([html.Th(label, scope="row"), html.Td(fmt_num(c, 1), className="num"),
                             html.Td(fmt_num(p, 1) if prev else "—", className="num"),
                             html.Td(_delta(c, p, None) if prev else "—", className="num"),
                             html.Td(fmt_num(y, 1), className="num")]))
    table = html.Div(className="table-wrap", children=html.Table(className="table table--exec", children=[
        html.Thead(html.Tr([html.Th(""), html.Th(cur.label), html.Th(prev.label if prev else "Prior month"),
                            html.Th("Change"), html.Th(ytd.label)])),
        html.Tbody(body)]))

    obs = insights.observations(ds, cur, prev, ids)

    # The month as a story: same engine as Insights, compared with the previous month.
    partial = cur.label.endswith("(to date)")
    month_name = cur.label.replace(" (to date)", "")
    per = N.Period("custom", month_name if not partial else f"{month_name} so far",
                   ("So far in " if partial else "In ") + month_name, cur.start, cur.end,
                   prev.start if prev else cur.start, prev.end if prev else cur.start,
                   prev.label.replace(" (to date)", "") if prev else "the month before", partial)
    story = N.story(ds, per, ids)

    labels, avail_vals, partial, unit_rows = [], [], [], []
    for p in ms:
        a = M.availability(ds, p.start, p.end, ids)
        labels.append(p.start.tz_convert(config.LOCAL_TZ).strftime("%b %Y"))
        avail_vals.append(a.value if a.value is not None else np.nan)
        partial.append(p.label.endswith("(to date)") or p.start == ds.data_start)
        burn = M.burn_rates(ds, p.start, p.end, ids).set_index("component")["used_units"]
        unit_rows.append({"month": labels[-1], **burn.to_dict()})
    units_frame = pd.DataFrame(unit_rows)

    demand = insights.demand_forecast(ds, 30, ids)

    a_cur, a_prev = val(sc_cur, "availability"), val(sc_prev, "availability")
    if a_cur is not None:
        change = ""
        if a_prev is not None:
            d = a_cur - a_prev
            change = f", {'up' if d >= 0 else 'down'} {abs(d):.1f} pts from {prev.label.split(' (')[0]}"
        hl = headline(N.availability_tone(a_cur),
                      f"{cur.label.replace(' (to date)', ' so far')}: printers were available {a_cur:.1f}% "
                      f"of the time{change}",
                      obs[1] if len(obs) > 1 else "")
    else:
        hl = headline("info", "Not enough data for this month yet")

    return [
        hl,
        html.Div(className="grid grid--exec", children=[
            html.Section(className="card card--wide story-card", children=[
                html.Header(html.Div([html.H3(f"The story of {month_name}"),
                                      html.P("Written from the data. The numbers behind it are in the scorecard below.",
                                             className="card__sub")]), className="card__head"),
                prose(story.paragraphs),
            ]),
            chart_card(f"Scorecard · {cur.label}", "Compared with the prior month and the year to date.",
                       explain=["Each row compares the month with the one before and with the year so far. Green "
                                "arrows are improvements and red arrows are declines, whichever direction 'good' "
                                "is for that measure.",
                                "'After desk hours' means outside the owning team's staffed hours (ResNet: "
                                "Mon–Thu 10–6, Fri 10–4; IT Service Center: Mon–Fri 9–4). 'Desk time to fix' "
                                "counts only staffed minutes, so it measures response once someone is in, while "
                                "MTTR also includes the wait for the desk to open.",
                                "Parts rows are volumes, not scores, so they have no color."],
                       body=table, wide=True),
            chart_card("Key observations", "Generated from the data; each line appears only when the evidence "
                       "behind it is sufficient.", wide=True,
                       body=html.Ul([html.Li(o) for o in obs], className="observations") if obs
                       else empty("Not enough data for observations yet.", big=False)),
            chart_card("Availability by month", "Gray bars are partial months.",
                       charts.monthly_bars(theme, labels, avail_vals, "%", partial), graph_id={"type": "xg", "chart": "monthly_avail"}),
            chart_card("Toner used by month", "Parts' worth of toner, stacked by color.",
                       charts.monthly_stacked(theme, units_frame, ["toner_k", "toner_c", "toner_m", "toner_y"]),
                       graph_id={"type": "xg", "chart": "monthly_toner"},
                       table=data_table(units_frame, [("month", "Month", None)] +
                                        [(c, config.COMPONENT_LABELS[c], lambda v: fmt_num(v, 2))
                                         for c in config.COMPONENTS])),
            chart_card("Parts needed in the next 30 days", "Projected from the last "
                       f"{config.BURN_RATE_LOOKBACK_DAYS} days' burn rate - a starting point for ordering.",
                       body=data_table(demand, [("label", "Part", None),
                                                ("next_units", "Parts (30 days)", lambda v: fmt_num(v, 1)),
                                                ("per_day", "Burn / day (pts)", lambda v: fmt_num(v, 1))])),
        ]),
        html.P("* month to date. Partial months are not comparable with full months.", className="footnote"),
    ]
