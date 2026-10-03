"""Outage-risk model on the dashboard: its report card (Analytics) and, once it has earned it, the
'likely to go down in the next 24 hours' list (Overview and station pages)."""
from __future__ import annotations

import pandas as pd
from dash import html

from ... import metrics as M, risk
from .. import charts
from ..components import chart_card, data_table, icon

NERD = [
    "Each example is one printer at one hour, using only what was known before that hour: time of day and week, "
    "whether its support desk is open and how long until it opens, the academic calendar, its own long-run outage "
    "rate (empirical-Bayes shrunk toward the campus rate), outages in the last day/week/month, time since the last "
    "one, current and recent warnings, faults, jams and paper problems in the last week, lowest toner and drum, "
    "fuser and belt life, and toner used in the last day and week. Label: a new outage starts within 24 hours. "
    "Hours already down or unmonitored are excluded.",
    "Candidates: logistic regression, histogram gradient-boosted trees, and a small neural network (multi-layer "
    "perceptron, one hidden layer of 16, L2 penalty 0.1, settings chosen on an earlier week than any test week). "
    "Baseline: each printer's own past rate through a one-feature logistic fit.",
    "Walk-forward evaluation: for each of the last 4 weeks, train only on data from before it (24-hour gap, recent "
    "history weighted with a 21-day half-life) and predict the week. Scores pool the 4 weeks. Brier score "
    "(mean squared error of the probabilities; lower is better), Brier skill vs the baseline, ROC AUC (chance a "
    "random outage-hour is ranked above a random quiet hour), and top-3 hit rate (of the 3 riskiest printers each "
    "hour, the share that really went down).",
    "Explanations: permutation importance by feature group on the latest test week; per printer, the groups whose "
    "change from typical values raises its risk most. Every displayed prediction is logged and scored 24 hours "
    "later for the live track record. Retrained nightly (scikit-learn).",
]


def _pct(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{v:.0%}"


def _ago(ts: str) -> str:
    h = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(ts)).total_seconds() / 3600
    return "just now" if h < 1 else f"{h:.0f} hours ago" if h < 48 else f"{h / 24:.0f} days ago"


def _gate_rows(card: dict) -> html.Ul:
    g, m = card.get("gate", risk.GATE), card.get("models", {}).get(card.get("champion") or "", {})
    items = [
        (card.get("days", 0) >= g["min_days"], f"{min(card.get('days', 0), g['min_days']):.0f} of {g['min_days']} days "
         "of history"),
        (card.get("train_pos", 0) >= g["min_train_pos"], f"{min(card.get('train_pos', 0), g['min_train_pos'])} of "
         f"{g['min_train_pos']} outages to learn from"),
        (card.get("test_pos", 0) >= g["min_test_pos"], f"{min(card.get('test_pos', 0), g['min_test_pos'])} of "
         f"{g['min_test_pos']} outages to test on"),
        (m.get("auc", 0) >= g["min_auc"], f"Ranks risky hours above quiet ones (AUC {m.get('auc', float('nan')):.2f}, "
         f"needs {g['min_auc']})" if "auc" in m else "Ranks risky hours above quiet ones"),
        (m.get("skill", -1) >= g["min_skill"], f"Beats each printer's own track record ({m.get('skill', float('nan')):+.1%}, "
         f"needs {g['min_skill']:+.0%})" if "skill" in m else "Beats each printer's own track record"),
    ]
    return html.Ul([html.Li([icon("check" if ok else "clock"), html.Span(text)],
                            className="gate__item" + (" is-met" if ok else "")) for ok, text in items], className="gate")


def model_card(ds: M.Dataset, theme: str):
    """The model's report card for Analytics > Forecasts & statistics."""
    risk.ensure_fresh(ds)
    card = risk.load_card(ds)
    title = "Outage risk model (machine learning)"
    sub = "Predicts each printer's chance of going down in the next 24 hours, and only switches on once it proves it."
    if card is None:
        return [chart_card(title, sub, icon_name=("bot", "blue"), wide=True,
                           story=["Training for the first time; this takes about a minute. Refresh to see it."
                                  if risk.training_now() else "Not trained yet."])]
    live = card.get("status") == "live"
    champ = card.get("champion")
    models = card.get("models", {})
    best = models.get(champ or "", {})
    when = (f"Trained {_ago(card['trained_at'])} on {card.get('rows', 0):,} printer-hours ({card.get('positives', 0):,} "
            f"outage starts){' of demo data' if card.get('demo') else ''}; retrains nightly.")
    if live:
        story = [("b", "Live. "), f"The best model ({risk.MODEL_NAMES.get(champ, champ)}) predicts outages "
                 f"{best.get('skill', 0):+.0%} better than each printer's own track record, and its three riskiest "
                 f"printers each hour really had an outage {_pct(best.get('top3_hit'))} of the time "
                 f"(vs {_pct(card.get('base_rate'))} for a typical printer-hour)."]
    else:
        story = [("b", "Learning, not shown yet. "), "It switches on by itself once it passes every check below. "
                 + (" ".join(card.get("reasons", [])))]
        if card.get("demo"):
            story.append(" On demo data the simulator's outages are mostly random, so there's little to learn: "
                         "refusing to go live is the right call.")
    body = [html.P(when, className="card__sub"), _gate_rows(card)]
    rows = [{"model": risk.MODEL_NAMES.get(k, k) + (" ★" if k == champ else ""), **v}
            for k, v in models.items() if "brier" in v]
    if rows:
        body.append(data_table(pd.DataFrame(rows), [
            ("model", "Model", None), ("brier", "Brier (lower is better)", lambda v: f"{v:.3f}"),
            ("skill", "vs baseline", lambda v: "—" if pd.isna(v) else f"{v:+.1%}"),
            ("auc", "AUC", lambda v: "—" if pd.isna(v) else f"{v:.2f}"),
            ("top3_hit", "Top-3 hit rate", _pct)]))
    tr = risk.track_record(ds)
    if tr:
        body.append(html.P([("Live track record: "), html.B(f"{tr['predictions']:,} predictions"),
                            f" over {tr['hours']:,} hours have been checked against what happened. The riskiest "
                            f"fifth had an outage {_pct(tr['top20_rate'])} of the time, vs {_pct(tr['base_rate'])} "
                            f"overall (AUC {tr['auc']:.2f})." if pd.notna(tr["auc"]) else ""], className="prose"))
    if live:
        f = risk.predict_now(ds)
        if len(f.table):
            body.append(_risk_table(f.table.head(10)))
    cards = [chart_card(title, sub, body=html.Div(body), story=story, wide=True, icon_name=("bot", "blue"),
                        explain=["Every hour, for every printer that's up, the model estimates the chance it goes "
                                 "down in the next 24 hours from what the monitor already knows: recent outages and "
                                 "faults, warnings, worn supplies, use, the time, desk hours and the calendar.",
                                 "It is tested the way it would be used: trained on the past, scored on weeks it "
                                 "hadn't seen. It has to beat a simple rule (each printer's own track record) before "
                                 "the dashboard shows it. Until then it keeps learning in the background."],
                        nerd=NERD)]
    if rows and len(rows) > 1:
        cards.append(chart_card("How each model did against the baseline", "Improvement over each printer's own "
                                "track record, on the held-out weeks. ★ is the one in use.",
                                charts.model_skill(theme, models, risk.MODEL_SHORT, champ), graph_id="risk-skill",
                                icon_name=("trend", "blue"),
                                explain="Right of zero means better than the simple rule. A model left of zero is "
                                        "worse than just knowing which printers usually break, so it isn't used."))
    if card.get("calibration"):
        cards.append(chart_card("When it says 30%, does it happen 30% of the time?", "Predicted chance vs what "
                                "actually happened on the held-out weeks.",
                                charts.calibration(theme, card["calibration"]), graph_id="risk-calibration",
                                icon_name=("target", "blue"),
                                explain="Points on the dotted line mean the percentages can be taken at face value. "
                                        "Above the line: it underestimates; below: it overestimates."))
    if card.get("importance"):
        cards.append(chart_card("What the model pays attention to", "How much worse its predictions get when each "
                                "kind of information is scrambled.", charts.importance_bars(theme, card["importance"]),
                                graph_id="risk-importance", icon_name=("glasses", "blue")))
    return cards


def _risk_table(t: pd.DataFrame):
    return data_table(t, [("label", "Printer", None), ("p", "Chance of an outage in 24 h", _pct),
                          ("relative", "vs typical", lambda v: "—" if pd.isna(v) else f"{v:.1f}×"),
                          ("reasons", "Mostly because of", None)], link_col=("label", "station_id"))


def overview_card(ds: M.Dataset, ids=None):
    """'Likely to go down in the next 24 hours': only once the model is live."""
    card = risk.load_card(ds)
    if not card or card.get("status") != "live":
        return None
    f = risk.predict_now(ds, ids)
    t = f.table[f.table["band"].isin(["high", "elevated"])] if len(f.table) else f.table
    if t.empty:
        return None
    return chart_card("Likely to go down in the next 24 hours", "Worth a look on the next round, before they break.",
                      body=_risk_table(t.head(6)), icon_name=("bolt", "gold"),
                      story=[("b", f"{(t['band'] == 'high').sum()} printer{'s' if (t['band'] == 'high').sum() != 1 else ''}"),
                             " at high risk. Predicted by the outage-risk model (see Analytics → Forecasts & statistics "
                             "for how well it has done)."])


def station_line(ds: M.Dataset, sid: str):
    card = risk.load_card(ds)
    if not card or card.get("status") != "live":
        return None
    f = risk.predict_now(ds, [sid])
    if f.table.empty:
        return None
    r = f.table.iloc[0]
    return html.P([icon("bolt"), html.Span([html.B(f"{r['p']:.0%}"), " chance of an outage in the next 24 hours "
                                            f"({r['relative']:.1f}× typical)" + (f", mostly because of {r['reasons']}"
                                                                                  if r["reasons"] else "") + "."])],
                  className="footnote footnote--icon")
