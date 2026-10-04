"""Software: what version is running, is it safe, is every part working, and the change log.

The Software page reads only facts the app records about itself (self-check, the vulnerability check,
version history, update packages, backup check-ins). Administrators can prepare and start update
packages here; starting one asks for the administrator password again.
"""
from __future__ import annotations

import threading
import time

import pandas as pd
from dash import dcc, html

from ... import __version__, accounts, auth, config, owasp, peer, selfcheck, threatintel, updates, vulns
from ..components import chart_card, headline, icon, tile

TZ = config.LOCAL_TZ
_CHECK = threading.Lock()
TONE = {"ok": "good", "warn": "warning", "fail": "critical", "info": "neutral"}
WORD = {"ok": "OK", "warn": "Check", "fail": "Problem", "info": "Note"}
PRIO_TONE = {"Act now": "critical", "Soon": "warning", "Routine": "neutral"}
OWASP_TONE = {"Met": "good", "Partial": "warning", "Gap": "critical"}
SEV_TONE = {"Critical": "critical", "High": "serious", "Moderate": "warning", "Low": "info", "Not rated": "neutral"}


def _when(ts) -> str:
    if not ts:
        return "—"
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(TZ).strftime("%a %b %-d, %Y %-I:%M %p")


def is_admin() -> bool:
    return accounts.current().get("role") == "admin" or not auth.enabled()


def layout():
    admin = is_admin()
    hide = None if admin else {"display": "none"}
    return [
        dcc.Interval(id="sw-tick", interval=30_000),
        html.Div(className="toolbar sw-actions", children=[
            html.Button([icon("shield"), html.Span("Check for vulnerabilities now")], id="sw-check",
                        className="btn", n_clicks=0),
            html.Button([icon("box"), html.Span("Prepare update package")], id="sw-prepare",
                        className="btn", n_clicks=0, style=hide),
            html.Div(className="sw-start", style=hide, children=[
                dcc.Input(id="sw-pw", type="password", placeholder="Administrator password",
                          autoComplete="current-password", className="acct-input"),
                html.Button([icon("send"), html.Span("Start update")], id="sw-start", className="btn btn--primary",
                            n_clicks=0)]),
            dcc.Link([icon("file"), html.Span("Change log")], href="/changelog", className="btn"),
        ]),
        html.Div(id="sw-msg", className="inv-msg", role="status"),
        dcc.Loading(html.Div(id="sw-body"), type="dot", delay_show=600),
    ]


def render(ds):
    data_dir = ds.data_dir
    checks = selfcheck.run(ds)
    tone, sentence = selfcheck.summary(checks)
    info = updates.build_info()
    rel = updates.release(__version__)
    fs = vulns.findings(data_dir)
    cache = vulns.load(data_dir)
    runtime = [f for f in fs if not f.tooling]
    hist = updates.history(data_dir)
    items = owasp.checklist(data_dir)
    out = [headline(tone, "The app's own check of itself", sentence)]

    out.append(html.Div(className="tiles", children=[
        tile("Version", __version__, (f"released {rel.date}" if rel else "not in the change log")
             + (f" · commit {info['commit']}" if info.get("commit") else "")),
        tile("Known vulnerabilities", str(len(runtime)),
             ("in the app's packages" + (f" (+{len(fs) - len(runtime)} in install tools)" if len(fs) > len(runtime) else ""))
             if cache.get("checked_at") else "not checked yet",
             tone="good" if cache.get("checked_at") and not runtime else
             "critical" if any(f.severity in ("Critical", "High") for f in runtime) else
             "warning" if runtime else None),
        tile("Last vulnerability check", _when(cache.get("checked_at")).split(",")[0] if cache.get("checked_at") else "—",
             f"{cache.get('packages', 0)} packages · OSV.dev" if cache.get("checked_at") else "daily, by the collector"),
        tile("Exploited or weaponized", str(sum(1 for f in fs if f.priority == "Act now")),
             "on CISA's exploited list, or with a Metasploit module" if cache.get("checked_at") else "not checked yet",
             tone=("critical" if any(f.priority == "Act now" for f in fs) else "good") if cache.get("checked_at") else None),
        tile("OWASP Top 10", "{Met} met".format(**owasp.summary(items)),
             "{Partial} partial · {Gap} gaps (2025 list)".format(**owasp.summary(items)),
             tone="critical" if owasp.summary(items)["Gap"] else "warning" if owasp.summary(items)["Partial"] else "good"),
        tile("Backup collectors", str(len(peer.peers(data_dir))),
             "standing by" if peer.key() else "not set up"),
    ]))

    out.append(chart_card("Self-check", "Each part of the app, checked just now. Grouped by what it protects: "
                          "availability, integrity, confidentiality, and staying current.", wide=True,
                          body=_checks(checks)))
    out.append(html.Div(id="vulns", children=chart_card(
        "Known vulnerabilities", "Every installed package and version, looked up in OSV.dev (the PyPI advisory "
        "database and GitHub security advisories), then ranked by what CISA, NIST, Microsoft, FIRST, Exploit-DB and "
        "Metasploit say about each one. Only package names, versions and CVE numbers are sent.", wide=True,
        body=html.Div(_vulns(fs, cache)))))
    out.append(chart_card("Threat-intelligence sources", "Where the ranking comes from, and when each source was "
                          "last read. Catalogs are downloaded whole and searched here, so no source learns which "
                          "packages the app uses (except the CVE numbers looked up one by one).", wide=True,
                          body=_sources(data_dir)))
    out.append(html.Div(id="owasp", children=chart_card(
        "OWASP Top 10 (2025) checklist", "How the app answers each of OWASP's ten most critical web application "
        "risks, with the evidence for every control and a live check of this copy. A self-assessment, not an "
        "independent audit.", wide=True, body=_owasp(items))))
    out.append(chart_card("Update packages", "What fixes the open vulnerabilities, and every update requested "
                          "from here. The app never changes its own code: GitHub builds and tests the update and "
                          "opens a pull request; it's installed by the normal deploy once approved.", wide=True,
                          body=html.Div(_packages(data_dir))))
    out.append(html.Div(id="backups", children=chart_card(
        "Backup collectors", "Second computers that take over collecting within about a minute of the main "
        "collector stopping, and step down within a minute of it coming back.", wide=True,
        body=_backups(data_dir))))
    out.append(chart_card("Versions installed here", "Each version this copy has run, from its first start.",
                          wide=True, body=_history(hist)))
    return out


def _checks(checks):
    rows = []
    for c in checks:
        rows.append(html.Tr([
            html.Td(html.Span(WORD[c.status], className=f"inv-state inv-state--{TONE[c.status]}")),
            html.Td(c.area, className="muted"),
            html.Td(html.B(c.name)),
            html.Td([c.detail, html.Div(c.fix, className="inv-hint") if c.fix else None]),
        ]))
    return html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in ("", "Protects", "Check", "Finding")])),
                                html.Tbody(rows)], className="table"), className="table-wrap")


def _vulns(fs, cache):
    if not vulns.enabled():
        return html.P("The vulnerability check is turned off on this copy (WEPA_VULN_CHECK=0).", className="card__note")
    if not cache.get("checked_at"):
        return html.P("Not checked yet. The collector checks once a day; or use 'Check for vulnerabilities now'.",
                      className="card__note")
    note = []
    if cache.get("error"):
        note.append(html.P(f"The last attempt failed ({cache['error']}); showing the result from "
                           f"{_when(cache['checked_at'])}.", className="inv-hint"))
    if not fs:
        return [html.P(f"No known vulnerabilities in the {cache.get('packages', 0)} installed packages "
                       f"(checked {_when(cache['checked_at'])}).", className="card__note"), *note]
    items = []
    for f in fs:
        why = ("Used to install packages, not by the running app." if f.tooling else
               "Listed directly in the app's requirements." if f.direct else
               f"Installed because {', '.join(f.required_by[:3]) or 'another package'} needs it.")
        items.append(html.Details(className="vuln", id=f.headline_id, children=[
            html.Summary(className="vuln__head", children=[
                html.Span(f.priority, className=f"inv-state inv-state--{PRIO_TONE[f.priority]}",
                          title="How urgent, from the threat-intelligence sources") if f.priority else None,
                html.Span(f.severity, className=f"inv-state inv-state--{SEV_TONE.get(f.severity, 'neutral')}"),
                html.B(f"{f.package} {f.installed}"),
                html.Span(f.headline_id, className="mono"),
                html.Span(f"fixed in {f.fixed}" if f.fixed else "no fix yet", className="muted"),
            ]),
            html.P(f.summary),
            html.Div([html.B("Why this priority: "), " ".join(f.reasons)], className="vuln__why") if f.reasons else None,
            _intel(f) if f.intel else None,
            html.Dl([html.Dt("Why it's installed"), html.Dd(why),
                     html.Dt("What fixes it"), html.Dd(f"Upgrading to {f.package} {f.fixed} or later."
                                                       if f.fixed else "No fixed version has been published yet."),
                     html.Dt("Also known as"), html.Dd(", ".join(f.ids[1:]) or "—"),
                     html.Dt("Published"), html.Dd(f.published or "—")], className="inv-facts"),
            html.Div([html.B("Sources: "), *[x for lk in f.links for x in
                                              (html.A(lk["label"], href=lk["url"], target="_blank",
                                                      rel="noopener noreferrer", className="link"), " · ")][:-1]],
                     className="vuln__links"),
        ]))
    return [html.P(f"Checked {_when(cache['checked_at'])} · click one for the details and sources.",
                   className="inv-hint"), *note, html.Div(items, className="vuln-list")]


def _yn(v) -> str:
    return "Yes" if v else "No"


def _intel(f):
    i = f.intel
    nvd, ms, vr, ep = i.get("nvd") or {}, i.get("msrc") or {}, i.get("vulnrichment") or {}, i.get("epss") or {}
    kev = i.get("kev")
    rows = [
        ("CISA KEV", f"Exploited in the wild; added {kev['added']}, CISA's deadline for U.S. agencies {kev['due']}"
         + ("; used in ransomware" if kev.get("ransomware") == "Known" else "") if kev else "Not listed"),
        ("CISA Vulnrichment", (f"Exploitation: {vr.get('exploitation', '?')}; automatable: {vr.get('automatable', '?')}; "
                               f"technical impact: {vr.get('technical_impact', '?')}") if vr.get("covered")
         else "Not assessed" if i.get("vulnrichment") is not None else "Couldn't check"),
        ("NIST NVD", (f"CVSS {nvd.get('version', '')} {nvd['score']} ({nvd.get('severity', '').lower()})"
                      + (f"; weakness {', '.join(nvd['cwes'])}" if nvd.get("cwes") else "")
                      if nvd.get("score") is not None else f"Record status: {nvd.get('status') or 'no score yet'}")
         if nvd.get("found") else "Not in NVD yet" if i.get("nvd") is not None else "Couldn't check"),
        ("Microsoft MSRC", (f"Tracked{' for Azure Linux' if ms.get('azure_linux') else ''}; exploited: "
                            f"{ms.get('exploited') or 'not stated'}; publicly disclosed: {ms.get('disclosed') or '—'}")
         if ms.get("tracked") else "Not tracked by Microsoft" if i.get("msrc") is not None else "Couldn't check"),
        ("FIRST EPSS", f"{ep['score']:.2%} chance of exploitation in 30 days (higher than {ep['percentile']:.0%} of CVEs)"
         if ep else "No score"),
        ("Exploit-DB", "; ".join(f"EDB-{e['id']}: {e['title']}" for e in i.get("exploitdb", [])[:3]) or "No public exploit"),
        ("Metasploit", "; ".join(m["module"] for m in i.get("metasploit", [])[:3]) or "No module"),
    ]
    return html.Dl([x for k, v in rows for x in (html.Dt(k), html.Dd(v))], className="inv-facts vuln__intel")


def _sources(data_dir):
    st = threatintel.status(data_dir)
    rows = []
    for key, name, what, url in threatintel.SOURCES:
        s_ = st.get(key, {})
        if s_.get("error") and s_.get("error_at", 0) >= s_.get("checked", 0):
            state, tone = "Last try failed", "warning"
        elif s_.get("checked"):
            state, tone = "OK", "good"
        else:
            state, tone = "Not read yet", "neutral"
        rows.append(html.Tr([
            html.Td(html.A(name, href=url, target="_blank", rel="noopener noreferrer", className="link")),
            html.Td(what), html.Td(html.Span(state, className=f"inv-state inv-state--{tone}"),
                                   title=s_.get("error", "")),
            html.Td(_when(s_.get("checked"))),
            html.Td(f"{s_['entries']:,} CVEs" if s_.get("entries") is not None and key in ("kev", "exploitdb",
                                                                                         "metasploit") else "—")]))
    rows.insert(0, html.Tr([html.Td(html.A("OSV.dev", href="https://osv.dev/", target="_blank",
                                           rel="noopener noreferrer", className="link")),
                            html.Td("Which known vulnerabilities affect each installed package version"),
                            html.Td(html.Span("OK" if vulns.load(data_dir).get("checked_at") else "Not read yet",
                                              className="inv-state inv-state--" + ("good" if vulns.load(data_dir).get(
                                                  "checked_at") else "neutral"))),
                            html.Td(_when(vulns.load(data_dir).get("checked_at"))), html.Td("—")]))
    return html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in ("Source", "What it tells us", "Status",
                                                                          "Last read", "Catalog size")])),
                                html.Tbody(rows)], className="table"), className="table-wrap")


def _owasp(items):
    out = []
    for it in items:
        out.append(html.Details(className="vuln", id=f"owasp-{it.code}", children=[
            html.Summary(className="vuln__head", children=[
                html.Span(it.status, className=f"inv-state inv-state--{OWASP_TONE[it.status]}"),
                html.B(f"{it.code}:2025 {it.name}"), html.Span(it.risk, className="muted")]),
            html.H4("What the app does"),
            html.Ul([html.Li([c, html.Div(ev, className="inv-hint mono")]) for c, ev in it.controls],
                    className="changelog__list"),
            html.Div([html.H4("Checked on this copy just now"),
                      html.Ul([html.Li(("✓ " if ok else "✕ ") + what, className="inv-ok" if ok else "inv-err")
                               for ok, what in it.live], className="owasp__live")]) if it.live else None,
            html.Div([html.H4("Known gaps"), html.Ul([html.Li(g) for g in it.gaps], className="changelog__list")])
            if it.gaps else None,
            html.A(f"OWASP's description of {it.code}", href=it.url, target="_blank", rel="noopener noreferrer",
                   className="link"),
        ]))
    return html.Div(out, className="vuln-list")


def _packages(data_dir):
    p = updates.plan(data_dir)
    parts = []
    if p["upgrades"]:
        parts.append(html.P([html.B("Ready to prepare: "), ", ".join(
            f"{e['package']} {e['installed']} → {e['target']} (fixes {', '.join(e['fixes'])})" for e in p["upgrades"])]))
    elif vulns.load(data_dir).get("checked_at"):
        parts.append(html.P("Nothing to update: no open vulnerability in the app's packages has a fixed version.",
                            className="card__note"))
    if p["tooling"]:
        parts.append(html.P("Install tools (" + ", ".join(f"{e['package']} {e['installed']}" for e in p["tooling"])
                            + ") come with the Python image and are upgraded with it, not by an update package.",
                            className="inv-hint"))
    pk = updates.packages(data_dir)
    if pk:
        rows = [html.Tr([html.Td(html.Span(r["ref"], className="mono"), id=r["ref"]),
                         html.Td(html.Span(r["state"], className="inv-state inv-state--" +
                                           {"Prepared": "info", "Requested": "warning"}.get(r["state"], "good"))),
                         html.Td(r["spec"], className="mono"),
                         html.Td(f"{r.get('by', '')}, {_when(r.get('created'))}"),
                         html.Td(html.A("GitHub run", href=r["runs"], target="_blank", rel="noopener noreferrer",
                                        className="link") if r.get("runs") else "—")]) for r in pk[:20]]
        parts.append(html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in
                                                              ("Package", "State", "Upgrades", "Prepared", "Build")])),
                                          html.Tbody(rows)], className="table"), className="table-wrap"))
    if not updates.can_start():
        parts.append(html.P("Starting updates from here needs a GitHub token (WEPA_GITHUB_TOKEN; see "
                            "docs/SECURITY.md). Without it, run the 'Software update package' workflow from "
                            "GitHub's Actions tab with the same upgrades.", className="inv-hint"))
    return parts


def _backups(data_dir):
    if peer.key() is None:
        return html.P("No backup collector is set up. To add one, give the app and a second computer the same "
                      "WEPA_PEER_KEY (32+ random characters) and run: python -m wepa_monitor backup --primary "
                      "https://<this app>.", className="card__note")
    ps = peer.peers(data_dir)
    if not ps:
        return html.P("The backup key is set, but no backup has checked in yet.", className="card__note")
    rows = []
    for bid, p in sorted(ps.items()):
        age = time.time() - p.get("last_seen", 0)
        st = p.get("state", "standby")
        word, tone = (("Collecting", "warning") if st == "active" else ("Standing by", "good")) if age < 120 \
            else ("Not checking in", "critical")
        cover = p.get("last_cover")
        rows.append(html.Tr([html.Td(html.B(bid)), html.Td(html.Span(word, className=f"inv-state inv-state--{tone}")),
                             html.Td(p.get("version") or "—"), html.Td(_when(p.get("last_seen"))),
                             html.Td(f"{_when(cover['since'])} – {_when(cover['until']).split(', ', 1)[-1]}"
                                     if cover else "—")]))
    return html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in
                                                    ("Backup", "Now", "Version", "Last check-in", "Last covered")])),
                                html.Tbody(rows)], className="table"), className="table-wrap")


def _history(hist):
    if not hist:
        return html.P("No versions recorded yet.", className="card__note")
    rows = [html.Tr([html.Td(dcc.Link(h["version"], href=f"/changelog/{h['version']}", className="link mono")),
                     html.Td(h.get("previous") or "—"), html.Td(h.get("commit") or "—", className="mono"),
                     html.Td(_when(h.get("ts")))]) for h in reversed(hist)]
    return html.Div(html.Table([html.Thead(html.Tr([html.Th(h) for h in ("Version", "Replaced", "Commit", "First ran")])),
                                html.Tbody(rows)], className="table"), className="table-wrap")


# --- the change log ------------------------------------------------------------------------------------

def _items(items):
    return html.Ul([html.Li(dcc.Markdown(i, link_target="_blank", className="md-inline")) for i in items],
                   className="changelog__list")


def _release(r: updates.Release, full: bool = True):
    head = html.Div(className="changelog__head", children=[
        dcc.Link(f"Version {r.version}", href=f"/changelog/{r.version}", className="changelog__ver"),
        html.Span(pd.Timestamp(r.date).strftime("%B %-d, %Y"), className="muted"),
        html.Span("Running now", className="inv-state inv-state--good") if r.version == __version__ else None,
        html.Span(f"{len(r.sections.get('Security', []))} security", className="inv-state inv-state--warning")
        if r.sections.get("Security") else None,
    ])
    body = [html.P(r.summary)] if r.summary else []
    if full:
        for name in ("New", "Changed", "Fixed", "Security", "Sources"):
            if r.sections.get(name):
                body += [html.H4(name), _items(r.sections[name])]
        for name, items in r.sections.items():
            if name not in ("New", "Changed", "Fixed", "Security", "Sources") and items:
                body += [html.H4(name), _items(items)]
        if r.security_ids:
            body.append(html.P(["Vulnerabilities fixed: ", *[x for i in r.security_ids for x in (
                html.A(i, href=(f"https://nvd.nist.gov/vuln/detail/{i}" if i.startswith("CVE-")
                                else f"https://github.com/advisories/{i}"), target="_blank",
                       rel="noopener noreferrer", className="link mono"), " ")]], className="inv-hint"))
    return html.Section([head, *body], className="card changelog")


def changelog_layout(version: str | None = None):
    releases = updates.changelog()
    if not releases:
        return html.P("The change log isn't available on this copy.", className="empty")
    if version:
        r = next((x for x in releases if x.version == version), None)
        if r is None:
            return html.Div([html.P(f"There's no version {version} in the change log."),
                             dcc.Link("See every version", href="/changelog", className="link")],
                            className="empty empty--page")
        i = releases.index(r)
        nav = html.Div(className="changelog__nav", children=[
            dcc.Link("← Every version", href="/changelog", className="link"),
            dcc.Link(f"Older: {releases[i + 1].version}", href=f"/changelog/{releases[i + 1].version}",
                     className="link") if i + 1 < len(releases) else None,
            dcc.Link(f"Newer: {releases[i - 1].version}", href=f"/changelog/{releases[i - 1].version}",
                     className="link") if i > 0 else None])
        return [nav, _release(r)]
    return [html.P(["Every release, newest first. You're running version ", html.B(__version__), "."],
                   className="muted"), *[_release(r, full=True) for r in releases]]


# --- actions -------------------------------------------------------------------------------------------

def act(ds, trigger: str, password: str | None):
    """Returns (message, tone)."""
    me = accounts.current()
    data_dir = ds.data_dir
    from ... import security
    if trigger == "sw-check":
        if not vulns.enabled():
            return "The vulnerability check is turned off (WEPA_VULN_CHECK=0).", "err"
        c = vulns.load(data_dir)
        if time.time() - c.get("checked_at", 0) < vulns.MIN_MANUAL_GAP_S and "error" not in c:
            return "Checked less than 10 minutes ago; showing that result.", "ok"
        if not _CHECK.acquire(blocking=False):
            return "A check is already running; results appear here when it finishes.", "ok"

        def run():
            try:
                vulns.refresh(data_dir, force=True, log=lambda m: security.log(m, "audit"))
            finally:
                _CHECK.release()
        threading.Thread(target=run, name="vuln-check", daemon=True).start()
        security.audit("vulnerability check started", me.get("username", ""))
        return ("Checking OSV.dev, then CISA, NIST, Microsoft, FIRST, Exploit-DB and Metasploit. This takes up to "
                "a few minutes; this page updates by itself."), "ok"
    if not is_admin():
        return "Only the administrator can prepare or start updates.", "err"
    if trigger == "sw-prepare":
        try:
            row = updates.prepare(data_dir, by=me.get("name") or me.get("username", "admin"))
        except ValueError as exc:
            return str(exc), "err"
        security.audit("update package prepared", f"{row['ref']}: {row['spec']}")
        return f"{row['ref']} prepared: {row['spec']}. Start it with your password to send it to GitHub.", "ok"
    if trigger == "sw-start":
        if auth.enabled():
            if not password or accounts.authenticate(accounts.admin_name(), password) is None:
                security.audit("update start refused", "wrong administrator password")
                return "That isn't the administrator password; the update wasn't started.", "err"
        pending = [p for p in updates.packages(data_dir) if p["state"] == "Prepared"]
        if not pending:
            return "Prepare an update package first.", "err"
        try:
            row = updates.start(data_dir, pending[0]["ref"], by=me.get("name") or "admin")
        except ValueError as exc:
            return str(exc), "err"
        security.audit("update started", f"{row['ref']}: {row['spec']}")
        return f"{row['ref']} sent to GitHub. A pull request appears once every test and scan passes.", "ok"
    return "", "ok"
