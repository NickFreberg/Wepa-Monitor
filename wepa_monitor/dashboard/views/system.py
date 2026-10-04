"""System: is the monitor healthy, what is it doing right now, and who is using the site.

Written for someone who has never seen the code: a picture of the moving parts with their live
status, a read-only live log ("the hamster on the wheel"), data quality in plain words, visits,
and sign-in security.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
from dash import dcc, html

from ... import ai, config, metrics as M, security
from .. import charts
from ..components import chart_card, data_table, fmt_minutes, headline, icon, tile
from .common import empty, needs_days

TZ = config.LOCAL_TZ


def layout():
    return [dcc.Interval(id="sy-log-tick", interval=5000),
            dcc.Loading(html.Div(id="sy-body"), type="dot", delay_show=600)]


def _ago(seconds: float) -> str:
    from ... import durations
    return durations.ago(seconds)


def _stage(name: str, what: str, status: str, tone: str, ic: str) -> html.Div:
    return html.Div(className=f"pipe__stage pipe__stage--{tone}", children=[
        html.Span(icon(ic), className="pipe__icon"),
        html.Div([html.Div(name, className="pipe__name"), html.Div(what, className="pipe__what"),
                  html.Div([html.Span(className="pipe__dot", **{"aria-hidden": "true"}), status],
                           className="pipe__status")]),
    ])


def render(ds: M.Dataset, theme: str, cache=None):
    if ds.empty:
        return empty("No data yet: the collector hasn't recorded a snapshot.")
    now = pd.Timestamp.now(tz="UTC")
    log = ds.log
    ok = log[log["ok"]]
    last_ok = ok["attempt_ts"].max() if len(ok) else pd.NaT
    age = (now - last_ok).total_seconds() if pd.notna(last_ok) else np.inf
    if ds.is_demo:
        age = (ds.as_of - last_ok).total_seconds() if pd.notna(last_ok) else np.inf
    today0 = ds.as_of.tz_convert(TZ).normalize().tz_convert("UTC")
    dq_today = M.data_quality(ds, max(today0, ds.data_start or today0), ds.as_of)
    recent = log[log["attempt_ts"] >= ds.as_of - pd.Timedelta(minutes=30)]
    fail_recent = int((~recent["ok"]).sum())
    dur = ok["duration_ms"].tail(60).median() / 1000 if len(ok) and "duration_ms" in ok else np.nan

    # --- headline ----------------------------------------------------------------------------
    if age <= 180 and fail_recent <= 2:
        hl = headline("good", "The monitor is healthy",
                      f"Last reading {_ago(age)}; {dq_today.extra.get('completeness', 0):.1f}% of today's readings "
                      "captured." if dq_today.value is not None else f"Last reading {_ago(age)}.")
    elif age <= 600:
        hl = headline("warning", "The monitor is running, with hiccups",
                      f"{fail_recent} of the last {len(recent)} attempts to read the Wepa page failed. It keeps "
                      "retrying every minute; nothing needs doing unless this lasts.")
    else:
        hl = headline("critical", f"No new readings for {fmt_minutes(age / 60)}",
                      "The collector may have stopped or lost its connection. The System log below shows the "
                      "last thing it did; restarting the app restarts the collector.")

    # --- the moving parts --------------------------------------------------------------------
    n_st = int(log.loc[log["ok"], "n_stations"].iloc[-1]) if len(ok) else 0
    loaded = (time.time() - cache.loaded_at) if cache is not None and cache.loaded_at else np.nan
    pipe = html.Div(className="pipe", role="list", **{"aria-label": "How the monitor works"}, children=[
        _stage("Wepa status page", "Wepa publishes every printer's status, refreshed each minute.",
               f"{n_st} stations listed", "good" if n_st else "warning", "printer"),
        html.Span("→", className="pipe__arrow", **{"aria-hidden": "true"}),
        _stage("Collector", "Reads that page once a minute, like a browser would: one small request.",
               f"Last read {_ago(age)}" + (f" · {dur:.1f} s" if np.isfinite(dur) else ""),
               "good" if age <= 180 else "warning" if age <= 600 else "critical", "history"),
        html.Span("→", className="pipe__arrow", **{"aria-hidden": "true"}),
        _stage("History", "Every reading is kept, one file per day, so trends can be measured.",
               f"{ds.hourly['hour'].dt.tz_convert(TZ).dt.date.nunique():,} days on record", "good", "box"),
        html.Span("→", className="pipe__arrow", **{"aria-hidden": "true"}),
        _stage("Dashboard", "Turns readings into outages, trends and stories, 15 s after each reading.",
               (f"Updated {_ago(loaded)}" + (f" in {cache.load_s:.1f} s" if cache and cache.load_s else ""))
               if np.isfinite(loaded) else "Up to date", "good", "chart"),
    ])
    how = chart_card("How it works, right now", "Each box is one part of the monitor, with what it last did.",
                     body=pipe, wide=True)

    terminal = chart_card(
        "Live log", "What the monitor is doing, newest at the bottom. Read-only; updates every few seconds.",
        wide=True, body=html.Pre(terminal_lines(ds, cache), id="sy-log", className="terminal",
                                 role="log", **{"aria-live": "off", "aria-label": "Live log"}))

    # --- data quality --------------------------------------------------------------------------
    start, end = M.window(ds, 30)
    dq = M.data_quality(ds, start, end)
    q = M.quality_daily(ds, start, end)
    if dq.value is not None:
        quality = [
            html.Div(className="tiles", children=[
                tile("Data quality score", f"{dq.value:.0f} / 100", "last 30 days",
                     tone="good" if dq.value >= 95 else "warning" if dq.value >= 80 else "critical",
                     help_text="30% freshness + 40% completeness + 20% validity + 10% station coverage."),
                tile("Readings captured", f"{dq.extra['completeness']:.1f}%", "of one per minute"),
                tile("Failed reads", f"{dq.extra['failure_rate']:.2f}%", f"{dq.extra['failures']:,} of "
                     f"{dq.extra['attempts']:,} attempts"),
                tile("Freshness", fmt_minutes(dq.extra["age_min"]), "since the last good reading"),
            ]),
            html.Div(className="grid", children=[
                chart_card("Readings captured each day", "100% means a reading every minute of the day. Dips are "
                           "times the page couldn't be read (Wepa down, network, or the monitor restarting).",
                           *needs_days(q, lambda: charts.daily_quality(theme, q, "completeness", "Captured", False)),
                           explain="Why it matters: every statistic on this site is computed only from time the "
                                   "monitor actually saw. Gaps are left out, never guessed, so low days make that "
                                   "day's numbers less certain, not wrong."),
                chart_card("Failed reads each day", "Share of each day's attempts that failed.",
                           *needs_days(q, lambda: charts.daily_quality(theme, q, "failure_rate", "Failed", True)),
                           table=data_table(q, [("local_date", "Date", lambda d: f"{d:%Y-%m-%d}"),
                                                ("attempts", "Attempts", None), ("ok", "Succeeded", None),
                                                ("failure_rate", "Failure rate", lambda v: f"{v:.2f}%")])),
            ]),
        ]
    else:
        quality = [empty("No monitoring history yet.", big=False)]

    return [hl, html.Div(className="grid", children=[how, terminal]),
            html.H2("Data quality", className="section-title"), *quality,
            html.H2("Who's using the site", className="section-title"), *_visits(),
            html.H2("Security", className="section-title"), *_security(),
            html.H2("AI assistant", className="section-title"), _ai_status()]


def terminal_lines(ds: M.Dataset, cache=None, n: int = 60) -> str:
    """The last few things the monitor did, as plain sentences with times."""
    rows = []
    log = ds.log.tail(n)
    for r in log.itertuples(index=False):
        t = r.attempt_ts.tz_convert(TZ)
        if r.ok:
            rows.append((r.attempt_ts.timestamp(), f"{t:%-I:%M:%S %p}  ✓ read the Wepa page: {int(r.n_stations)} "
                         f"stations in {r.duration_ms / 1000:.1f} s"))
        else:
            why = str(r.error or f"HTTP {r.http_status}")[:90]
            rows.append((r.attempt_ts.timestamp(), f"{t:%-I:%M:%S %p}  ✕ couldn't read the Wepa page ({why}); "
                         "will retry next minute"))
    for ts, kind, msg in list(security.console)[-n:]:
        mark = {"warn": "!", "audit": "•", "session": "→", "ai": "✦"}.get(kind, "·")
        local = pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(TZ)
        rows.append((ts, f"{local:%-I:%M:%S %p}  {mark} {msg}"))
    if cache is not None and cache.loaded_at:
        local = pd.Timestamp(cache.loaded_at, unit="s", tz="UTC").tz_convert(TZ)
        rows.append((cache.loaded_at, f"{local:%-I:%M:%S %p}  ✓ dashboard updated with the latest readings "
                                      f"({cache.load_s:.1f} s)"))
    rows.sort(key=lambda x: x[0])
    return "\n".join(r[1] for r in rows[-n:]) or "Waiting for the first reading…"


def _visits():
    s = security.sessions_frame()
    if s.empty:
        return [html.P("No visits recorded yet. Each open browser tab checks in once a minute while it's open.",
                       className="card__note")]
    week = s[s["last"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)]
    tiles = html.Div(className="tiles", children=[
        tile("Active now", str(int(s["active"].sum())), "open in a browser right now", tone="good" if s["active"].any() else None),
        tile("Visits, last 7 days", str(len(week)), f"{week['where'].eq('BSU network').sum()} from the BSU network"),
        tile("Typical visit", fmt_minutes(float(week["minutes"].median())) if len(week) else "—", "median length"),
        tile("Devices", str(week["device"].nunique()) if len(week) else "0", "different browser/device types"),
    ])
    shown = s.head(25).assign(
        started=lambda d: d["start"].dt.tz_convert(TZ).dt.strftime("%a %b %-d, %-I:%M %p"),
        seen=lambda d: [("now" if a else f"{l.tz_convert(TZ):%-I:%M %p}") for a, l in zip(d["active"], d["last"])],
        length=lambda d: d["minutes"].map(lambda m: fmt_minutes(m)))
    return [tiles, chart_card(
        "Recent visits", "One row per browser tab's visit. Addresses are shortened to their network (last part "
        "hidden) and no one is identified; records are kept 90 days.", wide=True,
        body=data_table(shown, [("started", "Started", None), ("seen", "Last seen", None), ("length", "Length", None),
                                ("where", "Where", None), ("network", "Network", None), ("device", "Device", None),
                                ("page", "Last page", None)]))]


def _security():
    enabled = security.login_enabled()
    ev = security.events_frame()
    week = ev[ev["ts"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)] if len(ev) else ev
    fails = week[week["kind"] == "login_failed"] if len(week) else week
    locks = security.locked_networks()
    tone = "good" if enabled and len(fails) < 5 and not locks else "warning" if enabled else "info"
    title = ("Sign-in is on" if enabled else "Sign-in is off (open access)")
    detail = (f"{len(fails)} failed sign-in{'s' if len(fails) != 1 else ''} in the last 7 days"
              + (f"; {len(locks)} network{'s' if len(locks) != 1 else ''} temporarily blocked" if locks else "")
              + ". After 8 wrong passwords in 15 minutes a network is blocked for 15 minutes."
              if enabled else "Anyone with the address can view the site. Set WEPA_BASIC_AUTH to require a password.")
    out = [headline(tone, title, detail)]
    if len(ev):
        rows = ev.head(30).assign(
            when=lambda d: d["ts"].dt.tz_convert(TZ).dt.strftime("%a %b %-d, %-I:%M %p"),
            what=lambda d: d["kind"].map({"login_failed": "Failed sign-in", "lockout": "Network blocked",
                                          "audit": "Action"}),
            detail2=lambda d: [(f"username '{r.get('user', '')}' · {r.get('where', '')} · {r.get('device', '')}"
                                if r["kind"] == "login_failed" else
                                f"{r.get('minutes', '')} minutes" if r["kind"] == "lockout" else
                                f"{r.get('what', '')}: {r.get('detail', '')}") for r in d.to_dict("records")],
            net=lambda d: d.get("network", pd.Series("", index=d.index)).fillna(""))
        out.append(chart_card("Security log", "Failed sign-ins, blocks and exports. Passwords are never recorded, "
                              "not even wrong ones.", wide=True,
                              body=data_table(rows, [("when", "When", None), ("what", "What", None),
                                                     ("net", "Network", None), ("detail2", "Details", None)])))
    else:
        out.append(html.P("Nothing to report: no failed sign-ins or exports recorded.", className="card__note"))
    return out


def _ai_status():
    st = ai.status()
    if not st["enabled"]:
        return headline("info", "AI summaries are off",
                        "Insights use built-in, rule-written text. To turn on AI summaries and answers, set "
                        "WEPA_AI_PROVIDER (copilot or openai) and its key; see the deployment guide.")
    last = (f"last answer {_ago(time.time() - st['last_ok'])}" if st["last_ok"] else "no answers yet")
    if st["last_error"]:
        return headline("warning", f"AI summaries are on ({st['provider']}), but the last request failed",
                        f"{st['last_error']}. Pages fall back to rule-written text until it recovers.")
    return headline("good", f"AI summaries are on ({st['provider']}, model: {st['model']})",
                    f"{st['calls']} answers since the app started; {last}. Only computed printer figures are sent; "
                    "answers are cached for 15 minutes and limited per hour.")
