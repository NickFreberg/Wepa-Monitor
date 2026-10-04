"""Planning: what to stock, where coverage is thin, what extra desk hours would buy, where one more
printer would help, and how printing follows the class schedule."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dash import html

from ... import advanced as A, courses, metrics as M
from .. import charts
from ..components import chart_card, data_table, fmt_hours, fmt_minutes, fmt_num, headline, prose
from .common import empty


def _bullet(seg):
    return html.Li(prose([seg], className="prose prose--inline"))


def _m(ds, name, fn, *args, **kw):
    """Compute once per dataset refresh (simulations and walking matrices are the slow part)."""
    key = ("planning", name, tuple(sorted(args[0])) if args and args[0] is not None else None, args[1:],
           tuple(sorted(kw.items())))
    return M.memo(ds, key, lambda: fn(ds, *args, **kw))


def render(ds: M.Dataset, theme: str, ids, start, end, plabel: str):
    blocks = []

    # --- supplies to stock (Monte Carlo) ------------------------------------------------------------
    mc = _m(ds, "mc", lambda d, i: A.supplies_monte_carlo(d, 30, i), ids)
    lead = A.lead_days()
    mc_lead = _m(ds, "mc_lead", lambda d, i, n: A.supplies_monte_carlo(d, n, i), ids, lead)
    if mc is not None and len(mc["table"]):
        t = mc["table"]
        if mc_lead is not None and len(mc_lead["table"]):
            t = t.merge(mc_lead["table"][["component", "p95"]].rename(columns={"p95": "reorder"}), on="component",
                        how="left")
        k = t.set_index("component").loc["toner_k"] if "toner_k" in set(t["component"]) else None
        story = (["For the next 30 days, plan on about ", ("b", f"{k['p50']:.0f} black toners"),
                  "; stocking ", ("b", f"{k['p95']:.0f}"), " covers 19 months out of 20."] if k is not None else None)
        blocks.append(chart_card(
            "What to stock for the next 30 days", "Parts each printer is likely to need, added up across campus, at "
            "three levels of confidence.", wide=True, story=story,
            body=data_table(t, [("label", "Part", None), ("p50", "Typical month", lambda v: f"{v:.0f}"),
                                ("p90", "Busy month (90%)", lambda v: f"{v:.0f}"),
                                ("p95", "To be safe (95%)", lambda v: f"{v:.0f}"),
                                ("max", "Worst case seen", lambda v: f"{v:.0f}"),
                                ("reorder", f"Reorder below ({lead}-day delivery)",
                                 lambda v: "—" if pd.isna(v) else f"{max(v, 1):.0f}")]),
            explain=["'Typical' is what a normal month needs. 'To be safe' is enough to run out only about one "
                     "month in twenty. The gap between them is the price of certainty.",
                     f"'Reorder below': when fewer than this many are left on the shelf, order more. It covers what "
                     f"printers would use while waiting {lead} days for delivery, 19 times out of 20. The delivery "
                     "time is an assumption: set WEPA_SUPPLY_LEAD_DAYS to the real one.",
                     "It starts from each part's level today, so a printer about to run out counts right away."],
            nerd=[f"Monte Carlo: {mc['sims']:,} simulated months. For each printer and part, {mc['horizon']} days of "
                  f"use are drawn with replacement from its own last {mc['history_days']} observed days (a "
                  "bootstrap), summed, and converted to replacements from today's level (first at the replacement "
                  "point, then one per full part). Totals across printers give the distribution; columns are its "
                  "50th, 90th and 95th percentiles.",
                  "Assumes the next month resembles the last four weeks. Before finals or move-in, lean on the 95% "
                  "column."]))
    else:
        blocks.append(chart_card("What to stock for the next 30 days", "", wide=True,
                                 body=empty("Needs at least a few days of supply readings.", big=False)))

    # --- staffing what-ifs ------------------------------------------------------------------------------
    cards = []
    for key in A.SCENARIOS:
        r = _m(ds, "staff", lambda d, i, s0, e0, sc: A.staffing_whatif(d, s0, e0, i, scenario=sc), ids, start, end, key)
        if r is None:
            continue
        lo, mid, hi = r["saved_h"]
        cards.append(html.Div(className="scenario", children=[
            html.Div(r["name"], className="scenario__name"),
            html.Div([html.Span(fmt_hours(mid), className="scenario__value"), " of printer downtime saved"],
                     className="scenario__big"),
            html.Div(f"likely {fmt_hours(lo)} to {fmt_hours(hi)} · about {fmt_hours(r['saved_h_per_week'])} a week · "
                     f"{r['affected']} of {r['after_hours']} after-hours outages picked up sooner",
                     className="scenario__meta"),
        ]))
    blocks.append(chart_card(
        "What extra coverage would buy", f"Replaying the last {plabel}'s real outages as if someone checked printers "
        "at these extra times.", wide=True,
        body=html.Div(cards, className="scenarios") if cards else empty("Needs more outages, including some fixed "
                                                                         "during desk hours, to replay.", big=False),
        explain="Each card answers: if someone had also checked printers at these times, how much printing time "
                "would students have gotten back? Compare it with the staff hours the extra check costs.",
        nerd="Each after-hours outage is re-timed: it's picked up at the first covered moment under the scenario, then "
             "takes a fix time drawn from fixes actually observed during desk hours; it can only get shorter. 1,000 "
             "bootstrap runs resample both the outages and the fix times; the range is the 10th–90th percentile."))

    # --- coverage gaps & placement ------------------------------------------------------------------------
    cov = _m(ds, "cov", lambda d, i, s0, e0: A.coverage(d, s0, e0, i), ids, start, end)
    if len(cov) and A.routing_ok():
        gaps = cov[cov["gap"]]
        story = ([("b", f"{len(gaps)} printer{'s are' if len(gaps) != 1 else ' is'}"),
                  f" alone in {'their' if len(gaps) != 1 else 'its'} building with the nearest open printer more than "
                  f"{A.WALK_ALERT_MIN:.0f} minutes' walk away: " + ", ".join(gaps["building"].unique()[:4]) + "."]
                 if len(gaps) else ["Every printer has a backup within a short walk."])
        blocks.append(chart_card(
            "If a printer is down, how far is the backup?", "Walk to the nearest printer anyone can use, for printers "
            "that are the only one in their building.", charts.walk_bars(theme, cov, A.WALK_ALERT_MIN),
            graph_id="walk-bars", story=story,
            explain="Residence-hall printers are behind card access, so the backup is the nearest academic building, "
                    "library, student union or East Campus Commons printer (ECC is run by ResNet, but its dining "
                    "hall, Dunkin', bookstore and ResNet office make it open to every student). Red bars are "
                    "coverage gaps.",
            nerd="Walking times are shortest paths (Dijkstra) on the OpenStreetMap footpath network at 3 mph, "
                 "door to door between building centers.",
            table=data_table(cov, [("label", "Printer", None), ("nearest", "Nearest open printer", None),
                                   ("walk_min", "Walk", fmt_minutes),
                                   ("down_h", f"Time down, {plabel}", fmt_hours)],
                             link_col=("label", "station_id"))))
        pl = _m(ds, "place", lambda d, i, s0, e0: A.placement(d, s0, e0, i), ids, start, end)
        if len(pl):
            best = pl.iloc[0]
            blocks.append(chart_card(
                "Where one more printer would help most", "Walking students would have saved, had there been one more "
                "printer in that building.",
                story=["A printer in ", ("b", best["building"]),
                       f" would have saved about {best['minutes_saved_per_week']:,.0f} minutes of walking a week, "
                       f"mostly for people in {best['helps']}."],
                body=data_table(pl, [("building", "Add a printer in", None),
                                     ("minutes_saved_per_week", "Walking saved (min/week)", lambda v: f"{v:,.0f}"),
                                     ("helps", "Mostly helps", None),
                                     ("has_printer", "Already has one", lambda v: "yes" if v else "no")]),
                explain="Counts only time when a printer was actually down, weighted by how busy that printer is: "
                        "the walk students really took, not a hypothetical.",
                nerd="A one-step greedy p-median: for each candidate building, recompute every down printer's walk "
                     "to its nearest backup with the candidate added; saved minutes = Σ (old walk − new walk) × hours "
                     "down × relative usage, per week of the period."))

    # --- classes vs printing --------------------------------------------------------------------------------
    cvp = _m(ds, "cvp", lambda d, i, s0, e0: courses.class_vs_printing(d, s0, e0, i), ids, start, end)
    if cvp is not None:
        rho = cvp["rho"]
        b = cvp["buildings"]
        strength = ("closely" if rho >= 0.7 else "somewhat" if rho >= 0.4 else "only loosely")
        story = [f"On weekdays, printing rises and falls {strength} with the number of classes in session "
                 f"(correlation {rho:.2f}). "]
        pre = b[b["pre_class_ratio"].notna()].sort_values("pre_class_ratio", ascending=False)
        if len(pre) and pre.iloc[0]["pre_class_ratio"] > 1.15:
            r = pre.iloc[0]
            story.append(f"In {r['building']}, printing in the hour before classes start runs "
                         f"{r['pre_class_ratio']:.1f}× its usual pace: the time to have trays full.")
        blocks.append(chart_card(
            "Does printing follow the class schedule?", f"Weekday classes in session (BSU course search, {cvp['term']}) "
            "and printing, by hour, each shown against its own average.", charts.classes_printing(theme, cvp["hours"]),
            graph_id="classes-printing", story=story,
            explain=["Both lines are scaled so 100 is their own weekday average, so their shapes can be compared on one "
                     "axis. When they rise together, printing follows classes.",
                     "Use it to time paper and toner checks: fill trays before the busiest class hours, not after."],
            nerd=[f"{cvp['sections']:,} in-person section meetings parsed from bridgew.edu's course search (sections, not "
                  "head counts; instructor names aren't kept). Printing = toner points used per weekday hour.",
                  "Spearman rank correlation across weekday hours 7 AM–10 PM; per building, across the 5 × 16 "
                  "weekday-hour grid. 'Before class' compares printing in the hour before section start times with "
                  "the building's average hour. Correlation, not causation."],
            table=data_table(b, [("building", "Building", None), ("sections", "Sections", None),
                                 ("rho", "Follows classes (ρ)", lambda v: fmt_num(v, 2)),
                                 ("pre_class_ratio", "Hour before class vs usual", lambda v: fmt_num(v, 1, "×"))])))

    n_ok = len(blocks)
    hl = headline("info", "Planning: what to stock, where coverage is thin, and what changes would buy",
                  "Each card simulates or replays the real data; open 'For nerds' for the method and its assumptions.") \
        if n_ok else headline("info", "Not enough data for planning yet")
    return [hl, html.Div(className="grid", children=blocks)]


def stats_extras(ds: M.Dataset, theme: str, ids, start, end, plabel: str) -> list:
    """Bayesian rates, warnings that turn into outages, recent changes, and before/after studies."""
    cards = []
    b = _m(ds, "bayes", lambda d, i, s0, e0: A.bayes_rates(d, s0, e0, i), ids, start, end)
    if len(b):
        camp = b.attrs.get("campus", np.nan)
        above = b[b["above_campus"]]
        story = ([("b", f"{len(above)} printer{'s' if len(above) != 1 else ''}"),
                  f" {'go' if len(above) != 1 else 'goes'} down more often than the campus rate even allowing for luck: " +
                  ", ".join(above["label"].head(4)) + "."] if len(above) else
                 ["No printer is clearly worse than the campus rate once chance is allowed for."])
        cards.append(chart_card(
            "Which printers really go down more often?", f"Outages per week with the likely range, last {plabel}. "
            "Printers with little history are pulled toward the campus rate.", charts.forest(theme, b, camp),
            graph_id="forest", story=story, wide=True,
            explain=["The dot is the best estimate; the line is where the true rate likely lies (90%). Red: clearly "
                     "above the campus rate. Green: clearly below.",
                     "A printer with two outages in its only week of history won't look like the worst on campus: "
                     "short histories get wide lines and are pulled toward the average."],
            nerd=["Empirical-Bayes Gamma-Poisson: outages ~ Poisson(rate × weeks observed), rate ~ Gamma(α, β) with "
                  f"α, β fitted by the method of moments across printers (α = {b.attrs['alpha']:.2f}, "
                  f"β = {b.attrs['beta']:.2f}). Posterior Gamma(α + n, β + weeks); intervals are its 5th–95th "
                  "percentiles."]))
    w = _m(ds, "w2o", lambda d, i, s0, e0: A.warning_to_outage(d, s0, e0, i), ids, start, end)
    if w is not None:
        lo, hi = w["ci"]
        cards.append(chart_card(
            "When a warning shows up, does an outage follow?", f"Warnings in the last {plabel}, and whether the same "
            "printer went out of service within 24 hours.",
            story=[("b", f"{w['share']:.0%}"), f" of warnings were followed by an outage within a day (likely "
                   f"{lo:.0%}–{hi:.0%})" + (f", typically {fmt_hours(w['median_lag_h'])} later" if np.isfinite(
                       w["median_lag_h"]) else "") + "."],
            body=data_table(w["by_cause"], [("cause", "Warning", None), ("warnings", "Times", None),
                                            ("led_to_outage", "Then out of service", None),
                                            ("share", "Share", lambda v: f"{v:.0%}")]),
            explain="Warnings that often turn into outages are worth a visit before they do. A warning type that "
                    "rarely leads anywhere can wait for the next round.",
            nerd="A two-step Markov view of each printer's states (warning → down within 24 h). The range is a 90% "
                 "Beta(1 + hits, 1 + misses) credible interval."))
    ch = _m(ds, "chg", lambda d, i: A.recent_changes(d, i), ids)
    cards.append(chart_card(
        "What changed recently?", "Printers whose problem rate in the last two weeks clearly differs from their own "
        "earlier rate.",
        body=data_table(ch, [("label", "Printer", None), ("direction", "Change", None),
                             ("before_per_week", "Before (per week)", lambda v: fmt_num(v, 1)),
                             ("recent_per_week", "Last 2 weeks", lambda v: fmt_num(v, 1)),
                             ("main_recent", "Mostly", None)], link_col=("label", "station_id"),
                        empty="Nothing stands out: no printer's problem rate shifted beyond normal ups and downs "
                              "(needs about four weeks of history)."),
        explain="Spots a printer starting to struggle (or a fix that worked) weeks before monthly averages would.",
        nerd="Exact conditional Poisson test: given n problems in total, the recent count is Binomial(n, recent share "
             "of observed time); listed when p < 0.01."))
    ba = A.before_after(ds)
    cards.append(chart_card(
        "Did a change work?", "Before and after each change logged in reference/changes.csv (a new tray, an evening "
        "round, a replaced printer), compared with the team's other printers over the same weeks.",
        body=data_table(ba, [("date", "Changed", None), ("what", "What", None), ("stations", "Printers", None),
                             ("before", "Outages/week before", lambda v: fmt_num(v, 2)),
                             ("after", "After", lambda v: fmt_num(v, 2)),
                             ("did", "Effect vs comparison", lambda v: fmt_num(v, 2)),
                             ("enough", "Enough data", lambda v: "yes" if v else "not yet")],
                        empty="No changes logged yet. Add a line to reference/changes.csv (date, station numbers or "
                              "building, what changed) and this card will measure it."),
        explain="A negative effect means fewer outages after the change than the comparison printers saw: the change "
                "likely helped. Wait at least two weeks after a change before reading much into it.",
        nerd="Difference-in-differences over 28 days either side: (changed after − before) − (comparison after − "
             "before), comparison = the same support team's other printers. Removes campus-wide swings such as "
             "finals or breaks; assumes both groups would otherwise have moved in parallel."))
    return cards
