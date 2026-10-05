"""IT Outcomes: a ready-to-print feature page for the IT division's annual report.

Laid out like the printed IT Outcomes issues (crimson and gold, a feature story in columns, a
"By the Numbers" sidebar), with every number computed from the monitoring data for the chosen
year. The headline, subtitle, credits and an optional quote come from reference/outcomes.json, so
the page can be tailored without touching code. Print it, or save it as a PDF, from the browser.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from dash import dcc, html

from ... import ai, config, metrics as M, narrative as N
from .. import charts
from ..components import icon
from .common import empty, scope_ids, scope_label

TZ = config.LOCAL_TZ
SETTINGS = config.REFERENCE_DIR / "outcomes.json"


def _settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def years(ds: M.Dataset) -> list[dict]:
    if ds.data_start is None:
        return [{"label": "All time", "value": "all"}]
    first = ds.data_start.tz_convert(TZ).year
    last = ds.as_of.tz_convert(TZ).year
    opts = [{"label": "Since monitoring began", "value": "all"}]
    opts += [{"label": str(y), "value": str(y)} for y in range(last, first - 1, -1)]
    # Academic years (July-June), the way much of campus reports.
    ay0 = ds.data_start.tz_convert(TZ)
    ay0 = ay0.year if ay0.month >= 7 else ay0.year - 1
    ay1 = ds.as_of.tz_convert(TZ)
    ay1 = ay1.year if ay1.month >= 7 else ay1.year - 1
    opts += [{"label": f"Academic year {y}–{str(y + 1)[2:]}", "value": f"ay{y}"} for y in range(ay1, ay0 - 1, -1)]
    return opts


def window(ds: M.Dataset, year: str | None):
    start, end = M.window(ds, None)
    if not year or year == "all":
        return start, end, "since monitoring began"
    if year.startswith("ay"):
        y = int(year[2:])
        a = pd.Timestamp(year=y, month=7, day=1, tz=TZ).tz_convert("UTC")
        b = pd.Timestamp(year=y + 1, month=7, day=1, tz=TZ).tz_convert("UTC")
        label = f"in the {y}–{str(y + 1)[2:]} academic year"
    else:
        y = int(year)
        a = pd.Timestamp(year=y, month=1, day=1, tz=TZ).tz_convert("UTC")
        b = pd.Timestamp(year=y + 1, month=1, day=1, tz=TZ).tz_convert("UTC")
        label = f"in {y}"
    return max(a, start), min(b, end), label


def layout(ds: M.Dataset):
    opts = years(ds)
    return [html.Div(className="toolbar no-print", children=[
        dcc.Dropdown(id="oc-year", options=opts, value=opts[0]["value"], clearable=False, className="dropdown",
                     style={"minWidth": "260px"}, persistence=True, persistence_type="session"),
        html.Button([icon("printer"), "Print or save as PDF"], id="oc-print", className="btn",
                    **{"data-print": "1"}),
        html.Button([icon("sparkle"), "Rewrite with AI"], id="oc-regen", className="btn", n_clicks=0,
                    title="Ask the AI for a fresh draft of the headline and story", hidden=not ai.enabled()),
        html.Span("Tip: in the print dialog choose “Save as PDF”, Letter, and turn on background graphics.",
                  className="toolbar__hint"),
    ]), dcc.Loading(html.Div(id="oc-body"), type="dot", delay_show=400)]


def _num(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    return f"{n:,.0f}"


def render(ds: M.Dataset, theme: str, year, scope, draft: int = 0):
    if ds.empty:
        return empty("No data yet.")
    ids = scope_ids(ds, scope)
    start, end, when = window(ds, year)
    if end <= start:
        return empty("No monitoring data in that period.")
    s = _settings()
    st = ds.stations if ids is None else ds.stations[ds.stations["station_id"].isin(ids)]
    log = ds.log[(ds.log["attempt_ts"] >= start) & (ds.log["attempt_ts"] < end) & ds.log["ok"]]
    checks = float(log["n_stations"].sum())
    days = (end - start).total_seconds() / 86400
    a = M.availability(ds, start, end, ids)
    inc = M._in(ds.sev_inc, "start", start, end, ids)
    red = inc[inc["severity"] == "red"]
    res = M._resolved(red)
    after = (~red["in_hours"].astype(bool)).mean() if len(red) else np.nan
    med_fix = res["duration_s"].median() if len(res) else np.nan
    desk_fix = res["staffed_s"].median() if len(res) else np.nan
    faults = M.faults_in(ds, start, end, ids)
    top_fault = faults["label"].value_counts() if len(faults) else pd.Series(dtype=int)
    trays = M._in(ds.tray_inc, "start", start, end, ids)
    use = M.usage_in(ds, start, end, ids)
    toner_units = use[use["component"].str.startswith("toner")]["used"].sum() / 100
    usage = M.usage_by_station(ds, start, end, ids)
    usage = usage[usage["usage_per_day"].notna()]
    scope_txt = scope_label(ds, scope)
    n = len(st)
    halls = int((st["station_type"] == "residence").sum())

    # --- the story -----------------------------------------------------------------------------
    p1 = (f"Bridgewater State's {n} Wepa print stations, {halls} of them in residence halls and the rest in "
          "labs, the library and the student union, are where students turn in papers, print boarding passes "
          "and pick up last-minute study guides. For years the only window into them was Wepa's own status "
          "page: a list of green, yellow and red rows that showed the moment, but never the pattern.")
    p2 = (f"BSU Student Printing Ops changed that. It reads the status page once a minute, every minute, and keeps "
          f"what it sees: {_num(checks)} station check-ins over {days:,.0f} days {when}. From those readings it "
          "works out when each printer went down, why, how long it took to fix, which supplies are about to run "
          "out, and which printers are busiest, then explains it in plain language for anyone at BSU.")
    findings = []
    if a.value is not None:
        findings.append(f"Across {scope_txt}, printers could print {a.value:.1f}% of the time")
    if len(red):
        findings.append(f"the monitor caught {len(red):,} outages" +
                        (f", typically resolved in {N.dur(med_fix)}" if np.isfinite(med_fix) else ""))
    p3 = (("; ".join(findings) + ". ") if findings else "")
    if np.isfinite(after) and len(red) >= 5:
        p3 += (f"Its clearest lesson is about timing: {after:.0%} of outages began after the support desks "
               f"had closed for the day. Once someone was on shift, the typical fix took {N.dur(desk_fix)} of desk "
               "time; most of the downtime was waiting for the next shift to start. ")
    if len(top_fault):
        p3 += (f"The most common problem was {top_fault.index[0].lower()} ({top_fault.iloc[0]:,} times), "
               "a pattern that now shapes which supplies go on every round.")
    p4 = ("Three tools put those findings to work. Rounds plans the fastest walking or transit-van route to every "
          "printer that needs a visit, starting from the ResNet office or the IT Service Center. Each station's page "
          "points students to the nearest working printer they can walk into, with the distance in feet or miles. "
          "And a report card grades every printer on reliability, faults and parts wear for its workload, "
          "so managers can see at a glance which machines are great and which are a problem.")
    p5 = ("Everything is computed only from time the monitor actually saw, gaps are reported rather than "
          "guessed, and nothing identifies a student: the data is about printers, not people.")

    launch = ds.data_start is not None and start <= ds.data_start < end
    if launch:
        p1, p2, p4, p5, extra = launch_story(ds, s, when, p3)
    else:
        extra = []

    quote = s.get("quote", "").strip()
    pull = (html.Blockquote([html.Span("“", className="oc__qmark"), html.P(quote),
                             html.Cite(s.get("quote_by", ""))], className="oc__quote") if quote else
            html.Blockquote([html.Span("“", className="oc__qmark"),
                             html.P(f"{after:.0%} of outages began after the support desks had closed."
                                    if np.isfinite(after) and len(red) >= 5 else
                                    f"{_num(checks)} printer check-ins, one every minute."),
                             html.Cite("From the monitoring data")], className="oc__quote"))

    # --- by the numbers ------------------------------------------------------------------------
    numbers = [
        ("printer", f"{n}", "Print stations watched around the clock"),
        ("history", _num(checks), "Station check-ins recorded"),
        ("check", f"{a.value:.1f}%" if a.value is not None else "—", "Of the time printers could print"),
        ("alert", f"{len(red):,}", "Outages caught and timed"),
        ("trend", N.dur(med_fix) if np.isfinite(med_fix) else "—", "Typical time to repair"),
        ("calendar", f"{after:.0%}" if np.isfinite(after) else "—", "Of outages began after desk hours"),
        ("box", f"{toner_units:,.0f}", "Toner cartridges' worth of printing"),
        ("file", f"{len(trays):,}", "Empty paper trays spotted"),
    ]
    stats = html.Aside(className="oc__numbers", children=[
        html.H3([html.Span(str(_year_word(year, ds))), html.Br(), "Print Stations", html.Br(), "By the Numbers"]),
        *[html.Div(className="oc__num", children=[
            html.Span(icon(ic), className="oc__num-icon"),
            html.Div([html.Div(v, className=f"oc__num-value oc__num-value--{'gold' if i % 2 else 'crimson'}"),
                      html.Div(lbl, className="oc__num-label")])]) for i, (ic, v, lbl) in enumerate(numbers)],
    ])

    # --- one chart: availability by month -----------------------------------------------------------
    months = []
    for p in pd.period_range(start.tz_convert(TZ).tz_localize(None).to_period("M"),
                             (end - pd.Timedelta(seconds=1)).tz_convert(TZ).tz_localize(None).to_period("M"), freq="M"):
        a0 = max(p.start_time.tz_localize(TZ).tz_convert("UTC"), start)
        a1 = min((p + 1).start_time.tz_localize(TZ).tz_convert("UTC"), end)
        m = M.availability(ds, a0, a1, ids)
        months.append((p.strftime("%b %Y"), m.value if m.value is not None else np.nan,
                       a0 > p.start_time.tz_localize(TZ).tz_convert("UTC") or a1 < (p + 1).start_time.tz_localize(TZ).tz_convert("UTC")))
    fig = charts.monthly_bars("crimson", [m[0] for m in months], [m[1] for m in months], "%", [m[2] for m in months])
    fig.update_layout(height=220)

    credits = s.get("credits", "").strip()
    busiest = usage.head(1)
    title, subtitle = s.get("title", "Every Printer, Every Minute"), s.get("subtitle", "")
    if launch:    # the findings sit inside the "early picture" paragraph, after the purpose and the value
        story = [html.P(p1, className="oc__lede"), html.P(p2), pull, html.P(p4), *[html.P(x) for x in extra],
                 html.P(p5)]
    else:
        story = [html.P(p1, className="oc__lede"), html.P(p2), html.P(p3), pull, html.P(p4), html.P(p5)]
    written_by = None
    copy = None if s.get("keep_my_text") else _ai_copy(
        ds, year, scope, draft, when, [p1, p2, p4, *extra, p5] if launch else [p1, p2, p3, p4, p5], numbers, busiest,
        quote, launch)
    if copy:
        title = copy.get("title") or title
        subtitle = copy.get("subtitle") or subtitle
        paras = [x for x in copy.get("paragraphs", []) if x.strip()]
        pq = copy.get("pull_quote", "").strip()
        if not quote and pq:
            pull = html.Blockquote([html.Span("“", className="oc__qmark"), html.P(pq),
                                    html.Cite("From the monitoring data")], className="oc__quote")
        mid = max(1, len(paras) // 2)
        story = ([html.P(paras[0], className="oc__lede")] + [html.P(x) for x in paras[1:mid]] + [pull] +
                 [html.P(x) for x in paras[mid:]])
        written_by = copy["_by"]
    feature = html.Article(className="oc", children=[
        html.Div(s.get("kicker", "Opportunities. Collaborations. Results."), className="oc__kicker"),
        html.H1(title, className="oc__title"),
        html.P(subtitle, className="oc__subtitle"),
        html.P(s.get("byline", ""), className="oc__byline") if s.get("byline") else None,
        mission_block(s) if launch else None,
        html.Div(className="oc__layout", children=[
            html.Div(className="oc__story", children=[
                *story,
                html.Figure([dcc.Graph(figure=fig, config={"displayModeBar": False, "staticPlot": True},
                                       style={"height": "220px"}),
                             html.Figcaption("Share of the time printers could print, by month. Lighter bars are "
                                             "partial months.")], className="oc__figure"),
                html.Div(className="oc__credit", children=[
                    html.Span(icon("award"), className="oc__credit-icon"),
                    html.P([credits] + ([" Busiest printer " + when + ": ", html.B(busiest.iloc[0]["description"]),
                                         f" ({busiest.iloc[0]['relative']:.1f}× the typical station)."]
                                        if len(busiest) else []))]) if credits else None,
            ]),
            stats,
        ]),
        html.Div([html.Span(s.get("kicker", "")), " // ", html.Span("IT Outcomes")], className="oc__foot"),
    ])
    note = ("Headline and story drafted by " + written_by + " from the numbers in the sidebar, which the app "
            "computes; check them before publishing. 'Rewrite with AI' asks for a fresh draft. To use your own words "
            "instead, set \"keep_my_text\": true in reference/outcomes.json." if written_by else
            "Every figure is computed from the monitoring data for the period chosen above. Edit the headline, "
            "subtitle, credits or quote in reference/outcomes.json" +
            ("." if s.get("keep_my_text") or not ai.enabled() else
             "; the AI draft wasn't available, so this is the built-in text."))
    return [feature, html.P(note, className="footnote no-print")]


def _year_word(year, ds) -> str:
    if not year or year == "all":
        return f"{ds.data_start.tz_convert(TZ):%Y}–{ds.as_of.tz_convert(TZ):%y}" \
            if ds.data_start.tz_convert(TZ).year != ds.as_of.tz_convert(TZ).year else f"{ds.as_of.tz_convert(TZ):%Y}"
    if str(year).startswith("ay"):
        y = int(year[2:])
        return f"{y}–{str(y + 1)[2:]}"
    return str(year)


OUTCOMES_PROMPT = """Write the copy for a one-page feature in Bridgewater State University's annual "IT Outcomes" report
(theme: Opportunities. Collaborations. Results.) about BSU Student Printing Ops, a tool that monitors the campus Wepa print
stations every minute. Match the report's voice: upbeat, proud of the IT and ResNet teams, concrete, readable by
anyone on campus. Use ONLY the facts given; copy numbers exactly; no invented people, quotes, names, titles or dates.
If the data covers only a short time, say so honestly. No prices or costs.

Return ONLY a JSON object, no other text:
{"title": "a short punchy headline (max 7 words)",
 "subtitle": "one italic-style line (max 14 words)",
 "paragraphs": ["4 or 5 paragraphs, 50-90 words each: the problem students and staff faced, what the tool does,
   what the data showed {when}, how teams use it (Rounds, backup printers, report card), and what's next"],
 "pull_quote": "one striking fact from the numbers as a short sentence (max 14 words), not attributed to a person"}"""


LAUNCH_PROMPT = """
This is the LAUNCH YEAR: monitoring began during the period covered, so write it as a stakeholder briefing for IT
leadership and partners, not just a recap. Over 6 or 7 paragraphs (50-90 words each), cover, in this order:
1. What BSU Student Printing Ops is and its mission (use the mission statement given, in your own words).
2. The business value: faster response, supplies and inventory accountability, evidence for the vendor (Wepa),
   better printer placement, and reporting leadership can trust.
3. Why data matters and how it matures the way the department works: from reacting to reports to measuring service
   the way students experience it, with consistent definitions and a permanent record.
4. The commitment to student success: printers working when assignments are due, and students pointed to the nearest
   working printer.
5. How it uses AI for analytics, with its guardrails exactly as described in the facts.
6. What the first weeks of data show (the numbers), stated honestly as an early, partial picture: say when monitoring
   began and that it does not tell the full story of the year.
7. What's next: value grows as data accumulates; the predictive models are built in and say when they have enough
   history (use the readiness facts given); these measures will be refined as the work continues.
Keep it confident and professional, never hype. Do not claim results the numbers don't show."""


def launch_facts(ds) -> list[str]:
    """What the launch-year briefing may say about the system itself: start date, AI guardrails and how ready
    each predictive model is."""
    from ... import risk
    began = ds.data_start.tz_convert(TZ)
    out = [f"- Monitoring began {began:%A, %B %-d, %Y} at {began:%-I:%M %p}"
           + (" (late at night)" if began.hour >= 21 or began.hour < 4 else "") + "."]
    out.append("- AI: an analyst built on a large language model answers questions in plain English, drafts summaries "
               "like this one, and reads supply invoices for a person to approve. It works only from figures the app "
               "computes; numbers it writes are checked against the data, and anything it can't verify is flagged or "
               "held back. It never receives information about individual students.")
    card = None
    try:
        card = risk.load_card(ds)
    except Exception:  # noqa: BLE001 - readiness is optional context
        card = None
    gate = risk.GATE
    if card and card.get("status") == "live":
        out.append("- Predictive model (outage risk for the next 24 hours): live; it beat the simple baseline in "
                   "walk-forward testing.")
    else:
        out.append(f"- Predictive model (outage risk for the next 24 hours): built in and learning. It goes live "
                   f"only after at least {gate['min_days']} days of history and {gate['min_train_pos']} outages to "
                   "learn from, and only if it beats a simple baseline in testing.")
    out.append("- Supply forecasts project each cartridge's replacement date from its own wear from the first week.")
    out.append("- Seasonal comparisons and the year-end review of whether each printer is needed wait for a full "
               "academic year of data.")
    return out


def mission_block(s: dict):
    pillars = s.get("pillars") or []
    return html.Section(className="oc__mission", children=[
        html.Div("Our mission", className="oc__mission-label"),
        html.P(s.get("mission", ""), className="oc__mission-text"),
        html.Div(className="oc__pillars", children=[
            html.Div([html.B(name), html.P(text)], className="oc__pillar") for name, text in pillars]),
    ])


def launch_story(ds, s: dict, when: str, findings: str) -> tuple[str, str, str, str, list[str]]:
    """The built-in launch-year briefing (used when no AI model is configured, or as the AI's accurate draft)."""
    from ... import risk
    began = ds.data_start.tz_convert(TZ)
    late = began.hour >= 21 or began.hour < 4
    gate = risk.GATE
    p1 = ("Student printing is a small service with a big moment: the paper due at 9 AM, the form that has to be "
          "signed today. This year ResNet launched BSU Student Printing Ops to make sure that moment works. Its "
          "mission: " + (s.get("mission") or "Keep every BSU print station ready when students need it."))
    p2 = ("It replaces guesswork with measurement. Once a minute it reads the status of every Wepa print station on "
          "campus and keeps what it sees, so the department can answer questions it never could before: how often "
          "printers are really available, what breaks and why, how long repairs take, and where supplies are going. "
          "Every figure is defined once and computed the same way every time, which is what makes it trustworthy "
          "enough to act on and to report.")
    p4 = ("The value is practical. Alerts and a prioritized Rounds route send staff to the right printer first. "
          "Supply forecasts and a full inventory trail, from delivery to the printer it went into, keep shelves "
          "stocked and every cartridge accounted for. Investigations build a documented evidence trail for "
          "conversations with Wepa, and placement reviews show where a printer is missing or no longer needed. "
          "For students, the result is simple: more printers working when it counts, and directions to the nearest "
          "one that is.")
    ai_p = ("The app also uses AI responsibly. A built-in analyst, powered by a large language model, answers "
            "questions in plain English, drafts summaries such as this one, and reads supply invoices for a person "
            "to approve. It works only from numbers the app has computed; figures it writes are checked against the "
            "data, and anything it can't verify is flagged or held back. It never sees information about "
            "individual students.")
    early = (f"Monitoring began {'late on the night of ' if late else 'on '}{began:%A, %B %-d}, {began:%Y}"
             + (f", at {began:%-I:%M %p}" if late else "") + ", so this year's figures are an early, partial "
             "picture rather than the full story of the year. " + findings).strip()
    nxt = ("Value grows with every week of data. Prediction is built in: supply forecasts already project when each "
           "cartridge will need replacing, and an outage-risk model compares several methods against a simple "
           f"baseline, going live only once it has at least {gate['min_days']} days of history and "
           f"{gate['min_train_pos']} outages to learn from, and only if it proves more accurate. A full academic "
           "year will add seasonal comparisons and a review of whether every printer earns its place. These "
           "measures will be refined as the work continues.")
    p5 = ("Underneath it all is a commitment to students: measure the service the way they experience it, act on "
          "what the data shows, and keep improving. Everything is computed only from time the monitor actually saw, "
          "gaps are reported rather than guessed, and nothing identifies a student.")
    return p1, p2, p4, p5, [ai_p, early, nxt]


def _ai_copy(ds, year, scope, draft, when, paragraphs, numbers, busiest, quote, launch=False) -> dict | None:
    """AI-written headline, subtitle, story and pull quote, from the computed figures only."""
    if not ai.enabled():
        return None
    import json as _json
    facts = ["Built-in draft (accurate, but plain):"] + [f"- {p}" for p in paragraphs if p]
    facts += ["By the numbers " + when + ":"] + [f"- {v}: {label}" for _, v, label in numbers]
    if len(busiest):
        b = busiest.iloc[0]
        facts.append(f"- Busiest printer: {b['description']} ({b['relative']:.1f}x the typical station)")
    if quote:
        facts.append("- A staff quote will appear on the page separately; don't write another.")
    if launch:
        s = _settings()
        facts += ["Launch-year facts:", f"- Mission: {s.get('mission', '')}"]
        facts += [f"- {name}: {text}" for name, text in s.get("pillars", [])]
        facts += launch_facts(ds)
    if ds.is_demo:
        facts.append("- NOTE: this is DEMO DATA (synthetic); say it's a preview.")
    try:
        from ... import analyst
        from .common import scope_ids
        prompt = OUTCOMES_PROMPT.replace("{when}", when) + (LAUNCH_PROMPT if launch else "")
        r = ai.ask(prompt, "\n".join(facts),
                   cache_key=f"outcomes|{year}|{scope}|{draft}|{launch}|{ds.as_of.floor('1h').isoformat()}", timeout=120,
                   toolkit=analyst.Toolkit(ds, scope_ids(ds, scope)), effort="medium")
        if r.unverified:            # a published report gets the built-in text rather than an unchecked figure
            return None
        text = r.text.strip()
        text = text[text.find("{"): text.rfind("}") + 1]
        copy = _json.loads(text)
        if not isinstance(copy.get("paragraphs"), list) or not copy["paragraphs"]:
            return None
        copy["_by"] = r.provider
        return copy
    except (ai.AIError, ValueError, KeyError, TypeError):
        return None
