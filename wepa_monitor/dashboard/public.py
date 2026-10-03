"""Printer status for students: a small, fast page per printer, reached from a QR code on the printer.

    /status             every printer, grouped by building, working or not
    /status/<id>        this printer: working or not, and the nearest working printers with walk times
    /status/qr/<id>.svg the QR code that opens /status/<id>
    /status/signs       printable signs, one per printer (staff only, always behind the login)

Facts only: status comes from the latest reading of Wepa's public status page, with its time shown;
if a printer hasn't reported recently the page says "unknown" instead of guessing. No tracking, no
cookies, nothing about who visits. Plain server-rendered HTML (no JavaScript) so it opens instantly
on a phone.

The student pages are behind the dashboard login unless WEPA_PUBLIC_STATUS=1. Turn that on only
with Wepa's and BSU's written OK: the data and the university's name are theirs.
"""
from __future__ import annotations

import html
import os

import pandas as pd

from .. import config, nearby, ops

STATE = {"green": ("Working", "ok"), "yellow": ("Working", "ok"), "red": ("Not working", "down"),
         "stale": ("Status unknown", "unknown")}

CSS = """
:root{--bg:#f6f4f0;--card:#fff;--ink:#1c1717;--muted:#6b6560;--ok:#0b7a32;--down:#b42323;--unk:#8a5a00;
--line:#e4e0d8;--brand:#89191f}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1c1c1a;--ink:#f3f2ee;--muted:#a8a59e;--ok:#3ccf6d;
--down:#ff8a8a;--unk:#ffd166;--line:#2c2c2a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}main{max-width:560px;margin:0 auto;padding:18px 16px 40px}
header{display:flex;align-items:center;gap:10px;margin-bottom:14px}header b{font-size:15px}
header span{color:var(--muted);font-size:13px}.bar{height:4px;background:var(--brand);border-radius:2px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:0 0 12px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:18px 0 8px}.muted{color:var(--muted);font-size:13.5px}
.pill{display:inline-flex;align-items:center;gap:8px;font-weight:700;font-size:18px;margin:8px 0 4px}
.dot{width:12px;height:12px;border-radius:50%;display:inline-block}.ok{color:var(--ok)}.ok .dot{background:var(--ok)}
.down{color:var(--down)}.down .dot{background:var(--down)}.unknown{color:var(--unk)}.unknown .dot{background:var(--unk)}
ul{list-style:none;padding:0;margin:0}li{display:flex;justify-content:space-between;gap:10px;padding:11px 0;
border-top:1px solid var(--line)}li:first-child{border-top:0}li a{color:inherit;text-decoration:none;font-weight:600}
.walk{color:var(--muted);white-space:nowrap;font-size:14px}a.all{display:block;margin-top:14px;color:var(--brand);
font-weight:600}footer{color:var(--muted);font-size:12.5px;margin-top:20px}
"""


def enabled() -> bool:
    return os.environ.get("WEPA_PUBLIC_STATUS", "") == "1"


def _page(title: str, body: str, refresh: int = 60) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='refresh' content='{refresh}'><title>{html.escape(title)}</title>"
            f"<meta name='robots' content='noindex'><style>{CSS}</style></head><body><main>"
            f"<div class='bar'></div><header><b>BSU print stations</b><span>live status</span></header>"
            f"{body}<footer>From Wepa's public status page, checked about every minute. "
            f"This page refreshes itself.</footer></main></body></html>")


def _when(ts) -> str:
    return pd.Timestamp(ts).tz_convert(config.LOCAL_TZ).strftime("%-I:%M %p")


def _pill(state: str) -> str:
    word, cls = STATE.get(state, STATE["stale"])
    return f"<div class='pill {cls}'><span class='dot'></span>{word}</div>"


def station_html(ds, sid: str) -> str | None:
    cur = ops.current_status(ds)
    me = cur[cur["station_id"] == sid]
    if me.empty:
        return None
    r = me.iloc[0]
    name = html.escape(str(r["description"]))
    from .views.common import station_messages
    msgs = station_messages(r) if r["state"] == "red" else []
    body = [f"<section class='card'><h1>{name}</h1><div class='muted'>{html.escape(str(r['building']))}</div>",
            _pill(r["state"]),
            f"<div class='muted'>As of {_when(r['scrape_ts'])}"
            + (f" · {html.escape(', '.join(msgs[:2]))}" if msgs else "") + "</div></section>"]
    alt = nearby.backups(cur, sid, limit=4)
    if len(alt):
        items = []
        for _, a in alt.iterrows():
            walk = "same building" if a["kind"] == "same building" else nearby.fmt_walk(a["seconds"])
            items.append(f"<li><a href='/status/{html.escape(a['station_id'])}'>{html.escape(str(a['description']))}"
                         f"</a><span class='walk'>{html.escape(walk)}</span></li>")
        head = "Other working printers nearby" if r["state"] != "red" else "Working printers nearby"
        body.append(f"<section class='card'><h2>{head}</h2><ul>{''.join(items)}</ul>"
                    "<div class='muted' style='margin-top:8px'>Walking times are estimates along campus paths. "
                    "Residence-hall printers are only listed for their own building.</div></section>")
    body.append("<a class='all' href='/status'>See every printer →</a>")
    return _page(f"{r['description']}: {STATE.get(r['state'], STATE['stale'])[0]}", "".join(body))


def index_html(ds) -> str:
    cur = ops.current_status(ds)
    up = int((~cur["state"].isin(["red", "stale"])).sum())
    body = [f"<section class='card'><h1>{up} of {len(cur)} printers working</h1>"
            f"<div class='muted'>As of {_when(cur['scrape_ts'].max())}</div></section>"]
    for b, g in cur.sort_values(["building", "description"]).groupby("building", sort=True):
        items = "".join(f"<li><a href='/status/{html.escape(r['station_id'])}'>{html.escape(str(r['description']))}</a>"
                        f"<span class='{STATE.get(r['state'], STATE['stale'])[1]}'><span class='dot'></span> "
                        f"{STATE.get(r['state'], STATE['stale'])[0]}</span></li>" for _, r in g.iterrows())
        body.append(f"<section class='card'><h2 style='margin-top:0'>{html.escape(str(b))}</h2><ul>{items}</ul></section>")
    return _page("BSU print stations: live status", "".join(body))


def qr_svg(url: str) -> bytes:
    import io

    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, dark="#1c1717", xmldecl=False)
    return buf.getvalue()


def signs_html(ds, base: str) -> str:
    """One printable sign per printer: name, a QR code, and what it does."""
    st = ds.stations.sort_values(["building", "description"])
    cards = []
    for _, r in st.iterrows():
        sid = html.escape(r["station_id"])
        cards.append(f"<div class='sign'><div class='q'><img src='/status/qr/{sid}.svg' alt=''></div>"
                     f"<div><div class='t'>Is this printer working?</div><div class='n'>{html.escape(str(r['label']))}"
                     f"</div><p>Scan to see its live status and the nearest working printers.</p>"
                     f"<div class='u'>{html.escape(base)}/status/{sid}</div></div></div>")
    css = ("body{font:14px system-ui,sans-serif;margin:20px;color:#1c1717}.sign{display:flex;gap:18px;align-items:center;"
           "border:2px solid #89191f;border-radius:14px;padding:16px;margin:0 0 14px;break-inside:avoid;width:560px}"
           ".q img{width:150px;height:150px}.t{font-size:22px;font-weight:800;color:#89191f}.n{font-size:16px;"
           "font-weight:700;margin:4px 0}.u{color:#555;font-size:11px;word-break:break-all}"
           "@media print{.noprint{display:none}}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Printer QR signs</title><style>{css}</style>"
            f"</head><body><p class='noprint'>Print this page (one sign per printer), cut, and post each sign on its "
            f"printer. Codes open {html.escape(base)}/status/&lt;printer&gt;."
            + ("" if enabled() else " <b>Note: the student pages are currently behind the dashboard login "
                                    "(WEPA_PUBLIC_STATUS is off), so students can't open them yet.</b>")
            + f"</p>{''.join(cards)}</body></html>")


def register(server, cache) -> None:
    from flask import Response, abort, request

    def base_url() -> str:
        return os.environ.get("WEPA_PUBLIC_URL", request.host_url).rstrip("/")

    @server.get("/status")
    def _status_index():
        return Response(index_html(cache.get()), mimetype="text/html")

    @server.get("/status/<sid>")
    def _status_station(sid):
        page = station_html(cache.get(), sid)
        if page is None:
            abort(404)
        return Response(page, mimetype="text/html")

    @server.get("/status/qr/<sid>.svg")
    def _status_qr(sid):
        ds = cache.get()
        if sid not in set(ds.stations["station_id"]):
            abort(404)
        return Response(qr_svg(f"{base_url()}/status/{sid}"), mimetype="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    @server.get("/status/signs")
    def _status_signs():
        return Response(signs_html(cache.get(), base_url()), mimetype="text/html")


PUBLIC_PREFIXES = ("/status",)


def is_public(path: str) -> bool:
    """Student pages skip the login only when turned on; the printable signs never do."""
    return enabled() and path.startswith(PUBLIC_PREFIXES) and not path.startswith("/status/signs")
