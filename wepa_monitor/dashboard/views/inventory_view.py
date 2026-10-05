"""Inventory page: consumables and paper on hand at every level (central storage, closets, under each kiosk),
the trail of every unit, receiving invoices with a second approver, moves, counts, weekly paper checks,
storage locations, and the Wepa kiosk key log."""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path

import pandas as pd
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from ... import accounts, config, inventory as inv, keys as K, metrics as M, refs, reference
from ..components import data_table, icon, page_tabs, segmented, tile
from .common import empty

TABS = [("stock", "Stock"), ("record", "Record"), ("paper", "Paper checks"), ("keys", "Keys"), ("setup", "Setup")]
RECORD = [("receive", "Delivery received"), ("move", "Moved"), ("count", "Counted"), ("writeoff", "Written off")]
OLD_TABS = {"trail": ("stock", None), "receive": ("record", "receive"), "move": ("record", "move"),
            "count": ("record", "count"), "locations": ("setup", None)}     # links from earlier versions
RECEIPT_ROWS = 8
MAX_UPLOAD = 5 * 1024 * 1024          # the server takes 8 MB a request; base64 adds a third
KIND_ORDER = {"central": 0, "closet": 1, "paper": 2, "kiosk": 3}
STATUS_TONE = {"overdue": "critical", "due Friday": "warning", "done this week": "good"}
MAGIC = {b"%PDF": "application/pdf", b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg"}
EXT = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


# --- who may do what ---------------------------------------------------------------------------------------

def perms() -> dict:
    """Changes are recorded under a person's name, so they need a named staff account (or no sign-in at all,
    when running locally). Storage locations are the administrator's to define. The key log holds staff and
    student workers' personal details: staff and the administrator only."""
    u = accounts.current()
    local = not os.environ.get("WEPA_BASIC_AUTH")
    admin = u.get("role") == "admin"
    return {"user": u, "change": bool(u.get("can_edit")), "configure": admin or local,
            "keys_view": admin or u.get("role") == "staff" or local,
            "keys_edit": admin or bool(u.get("can_edit")),
            "second_person": not local}


def _actor(p: dict) -> dict:
    u = p["user"]
    return {"username": u.get("username") or "", "name": u.get("name") or u.get("username") or ""}


def invoices_dir(data_dir) -> Path:
    return refs.records_dir(data_dir) / "inventory" / "invoices"


# --- layout ---------------------------------------------------------------------------------------------

def layout(ds: M.Dataset, params: dict):
    tab = params.get("tab", "stock")
    tab, sub = OLD_TABS.get(tab, (tab, params.get("do")))
    tab = tab if tab in dict(TABS) else "stock"
    p = perms()
    tabs = [(k, t) for k, t in TABS if k != "keys" or p["keys_view"]]
    return [
        html.Div(className="inv-intro", children=[
            html.P("Supplies on hand and where every unit went. Parts (toner, drums, belts, fusers) are kept in central "
                   "storage; paper is kept there, in building closets and under each kiosk. Installed parts and tray "
                   "refills are deducted automatically from what the printers report, and physical counts correct "
                   "the estimates. Every change is permanent and records who made it.", className="muted"),
            _who(p)]),
        page_tabs("iv-tab", [{"label": t, "value": k} for k, t in tabs], tab),
        dcc.Store(id="iv-sub", data=sub if sub in dict(RECORD) else "receive"),
        dcc.Store(id="iv-rev", data=0),
        html.Div(id="iv-msg", className="inv-msg", role="status"),
        dcc.Loading(html.Div(id="iv-body"), type="dot", delay_show=400),
    ]


def _who(p: dict) -> html.Div:
    u = p["user"]
    if p["change"]:
        txt = [icon("users"), html.Span(["Signed in as ", html.B(u["name"]), ": changes are recorded under your name."])]
    elif u.get("role") == "admin":
        txt = [icon("lock"), html.Span("Shared administrator account: you can set up locations and the key log. "
                                       "Moving and counting stock needs a named staff account, so each change has "
                                       "a person behind it.")]
    else:
        txt = [icon("lock"), html.Span("Read only.")]
    return html.Div(txt, className="inv-who")


def _loc_options(st: dict, kinds=None, with_kiosks: bool = True) -> list[dict]:
    locs = [L for L in st["locations"].values() if L.get("active", True)
            and (kinds is None or L["kind"] in kinds) and (with_kiosks or L["kind"] != "kiosk")]
    locs.sort(key=lambda L: (KIND_ORDER.get(L["kind"], 9), L.get("building", ""), L["name"]))
    return [{"label": f"{L['name']}" + (f" · {L['building']}" if L.get("building") and L["kind"] != "kiosk" else ""),
             "value": L["id"]} for L in locs]


ITEM_OPTIONS = [{"label": v, "value": k} for k, v in inv.ITEMS.items()]


def _q(item):
    return lambda v: "" if v is None or v != v else inv.fmt_qty(float(v), item)


def _num(v, item="x"):
    if v is None or v != v:
        return ""
    return f"{v:,.2f}" if item == inv.PAPER else f"{v:,.0f}"


def _when(ts) -> str:
    if ts is None or ts == "" or (isinstance(ts, float) and ts != ts):
        return "—"
    t = pd.Timestamp(ts, unit="s", tz="UTC") if isinstance(ts, (int, float)) else pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert(config.LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p")


def render(ds: M.Dataset, tab: str, sub: str = "receive"):
    p = perms()
    st = inv.state(ds.data_dir, ds.stations)
    if tab == "record":
        return _record(ds, st, p, sub)
    fn = {"stock": _stock, "paper": _paper, "setup": _locations, "keys": _keys}.get(tab, _stock)
    if fn is _keys and not p["keys_view"]:
        return empty("The key log is for staff only.")
    return fn(ds, st, p)


# --- stock ---------------------------------------------------------------------------------------------------

def _stock(ds, st, p):
    if st["started"] is None:
        return [empty("Inventory isn't started yet. Receive the first delivery or count what's on the shelves "
                      "(parts in central storage; paper in central storage, the closets and under the kiosks), and "
                      "automatic deductions start from that moment. The administrator records where central storage "
                      "and the closets are on the Locations tab.", big=False)]
    t = inv.totals(st)
    paper = t[t["item"] == inv.PAPER].iloc[0]
    parts = t[t["item"] != inv.PAPER]
    neg = int(t["negative"].sum())
    ok, chain = inv.ledger(ds.data_dir).verify()
    pending = sum(r["status"] == "awaiting approval" for r in st["receipts"].values())
    tiles = html.Div(className="tiles", children=[
        tile("Paper on hand", f"{paper['total']:,.2f}", "reams, all locations"),
        tile("Paper in storage", f"{paper['central'] + paper['closets']:,.2f}",
             f"{paper['central']:,.2f} central · {paper['closets']:,.2f} in closets"),
        tile("Parts on hand", f"{int(parts['total'].sum()):,}", "toner, drums, belts, fusers"),
        tile("Awaiting approval", str(pending), "deliveries entered but not yet added", ok=pending == 0,
             href="/inventory?tab=receive"),
    ])
    shown = t.copy()
    for c in ("central", "closets", "kiosks", "total"):
        shown[c] = [_num(v, i) if i == inv.PAPER or c in ("central", "total") or abs(v) > 1e-9 else "—"
                    for v, i in zip(t[c], t["item"])]
    shown["item_label"] = [lbl + (" (reams)" if i == inv.PAPER else "") for lbl, i in zip(t["item_label"], t["item"])]
    table = data_table(shown, [("item_label", "Item", None), ("central", "Central", None), ("closets", "Closets", None),
                               ("kiosks", "Under kiosks", None), ("total", "Total", None)])
    b = inv.balances(st)
    b = b[b["qty"].abs() > 1e-9].copy()
    b["_o"] = b["kind"].map(KIND_ORDER)
    b = b.sort_values(["_o", "location_name", "item"])
    b["on_hand"] = [html.Span(inv.fmt_qty(v, i), className="inv-bad" if v < 0 else None)
                    for v, i in zip(b["qty"], b["item"])]
    rows = data_table(b, [("location_name", "Location", None), ("building", "Building", None),
                          ("item_label", "Item", None), ("on_hand", "On hand", lambda v: v)],
                      max_rows=500, empty="Nothing on hand anywhere yet.")
    return [
        tiles,
        html.P([icon("alert"), html.Span(f"{neg} balance{'s are' if neg != 1 else ' is'} below zero: something was "
                                         "used from a place the records say was empty. Count those locations.")],
               className="footnote footnote--icon inv-bad") if neg else None,
        html.Div(className="card inv-tot", children=[html.H3("On hand by level"), table]),
        html.Div(className="card inv-tot inv-tot--last", children=[html.H3("By location"), rows]),
        html.Div(className="card", children=[html.H3("Trail: every movement, newest first"), *_trail(ds, st, p)]),
        html.P([icon("shield" if ok else "alert"), html.Span(("Inventory record verified. " if ok else
                                                              "INVENTORY RECORD PROBLEM: ") + chain)],
               className="footnote footnote--icon" + ("" if ok else " inv-bad")),
    ]


def _record(ds, st, p, sub):
    """Record what happened: a delivery, a move, a count or a write-off. One form at a time."""
    if not p["change"]:
        return [empty("Recording changes needs a named staff account, so each change has a person behind it. "
                      "Deliveries awaiting approval are listed below.", big=False), *_receive(ds, st, p)[1:]]
    count, writeoff = _count(ds, st, p)
    forms = {"receive": _receive(ds, st, p), "move": _move(ds, st, p), "count": count, "writeoff": writeoff}
    return [html.Div(className="inv-record", children=[
        html.Span("What happened?", className="inv-label"),
        segmented("iv-rec", [{"label": t, "value": k} for k, t in RECORD], sub)]),
        *[html.Div(forms[k], id=f"iv-rec-{k}", hidden=k != sub) for k, _ in RECORD]]


# --- trail ---------------------------------------------------------------------------------------------------

def _trail(ds, st, p):
    return [html.Div(className="toolbar inv-filters", children=[
        dcc.Dropdown(id="iv-tr-item", options=ITEM_OPTIONS, placeholder="Every item", className="inv-dd"),
        dcc.Dropdown(id="iv-tr-loc", options=_loc_options(st), placeholder="Every location", className="inv-dd"),
    ]), html.Div(id="iv-tr-body", children=trail_table(ds, st, None, None))]


ACTION_LABEL = {"receipt.approve": "Received", "move": "Moved", "install": "Installed", "refill": "Tray refilled", "adjust": "Written off",
                "count": "Counted"}


def trail_table(ds, st, item, loc):
    t = inv.trail(ds.data_dir, item, loc)
    names = {k: v["name"] for k, v in st["locations"].items()}
    if len(t):
        t["what"] = [ACTION_LABEL.get(a, a) if not (a == "adjust" and q and r in ("found", "correction"))
                     else ("Found" if r == "found" else "Corrected") for a, q, r in zip(t["action"], t["qty"], t["reason"])]
        t["item_label"] = t["item"].map(inv.ITEMS)
        t["from_name"] = t["from"].map(lambda x: names.get(x, x) if x else "")
        t.loc[t["action"] == "receipt.approve", "from_name"] = t["from"]
        t["to_name"] = [names.get(x, x) if x else (f"Printer #{s}" if a in ("install", "refill") else "")
                        for x, a, s in zip(t["to"], t["action"], t["station_id"])]
        t["amount"] = [inv.fmt_qty(q, i) + (" (est.)" if a == "refill" else "") for q, i, a in
                       zip(t["qty"], t["item"], t["action"])]
        def variance(v, item):
            if not isinstance(v, (int, float)) or v != v:
                return ""
            if abs(v) < 1e-9:
                return "matches the record"
            return f"{inv.fmt_qty(abs(v), item)} {'short' if v < 0 else 'over'}"
        t["detail"] = [" · ".join(x for x in (r, n, variance(v, i)) if x) for r, n, v, i in
                       zip(t["reason"], t["note"], t["variance"], t["item"])]
    return data_table(t, [("ts", "When", _when), ("what", "What", None), ("item_label", "Item", None),
                          ("amount", "How much", None), ("from_name", "From", None), ("to_name", "To", None),
                          ("by", "By", None), ("detail", "Notes", None)], max_rows=300,
                      empty="No movements recorded yet.")


# --- receive ------------------------------------------------------------------------------------------------

def _receive(ds, st, p):
    dest = _loc_options(st, with_kiosks=False)
    default = next((o["value"] for o in dest if o["value"].startswith("central:")), dest[0]["value"] if dest else None)
    form = None
    if p["change"]:
        lines = [html.Div(className="inv-line", children=[
            dcc.Dropdown(id={"type": "rcv-item", "i": i}, options=ITEM_OPTIONS, placeholder="Item", className="inv-dd"),
            dcc.Input(id={"type": "rcv-qty", "i": i}, type="number", min=0, step=0.01, placeholder="Qty",
                      className="inv-num", inputMode="decimal"),
            dcc.Dropdown(id={"type": "rcv-to", "i": i}, options=dest, value=default, clearable=False, className="inv-dd"),
            dcc.Input(id={"type": "rcv-text", "i": i}, type="text", placeholder="As printed (optional)", maxLength=200),
        ]) for i in range(RECEIPT_ROWS)]
        form = html.Div(className="card", children=[
            html.H3("Receive a delivery"),
            html.P("Upload the invoice, packing slip or a photo of the receipt and the lines are filled in for you; "
                   "or type them. Check every line against the boxes: reading invoices and typing numbers both make "
                   "mistakes. Nothing is added to stock until a second person approves it.", className="muted"),
            dcc.Upload(id="rcv-upload", className="acct-upload", accept=".pdf,.png,.jpg,.jpeg,.webp",
                       max_size=MAX_UPLOAD, children=html.Div([icon("file"), html.Span(
                           "Upload an invoice or receipt (PDF or photo, up to 5 MB)")])),
            dcc.Store(id="rcv-file"),
            dcc.Loading(html.Div(id="rcv-read", className="inv-hint"), type="dot"),
            html.Div(className="inv-form inv-form--wide", children=[
                html.Div(className="inv-pair", children=[
                    html.Label(["Vendor", dcc.Input(id="rcv-vendor", type="text", maxLength=80)]),
                    html.Label(["Invoice or PO number", dcc.Input(id="rcv-no", type="text", maxLength=60)])]),
                html.Div(className="inv-line inv-line--head", children=[
                    html.Span("Item"), html.Span("Quantity (paper in reams)"), html.Span("Goes to"),
                    html.Span("Line on the invoice")]),
                *lines,
                html.Button("Submit for approval", id="rcv-go", className="btn btn--primary", n_clicks=0),
            ]),
        ])
    pending = [r for r in st["receipts"].values() if r["status"] == "awaiting approval"]
    names = {k: v["name"] for k, v in st["locations"].items()}

    def lines_of(r):
        return html.Ul([html.Li(f"{inv.fmt_qty(ln['qty'], ln['item'])} of {inv.ITEMS[ln['item']]} → "
                                f"{names.get(ln['to'], ln['to'])}" + (f"  ({ln['text']})" if ln.get("text") else ""))
                        for ln in r["lines"]], className="inv-lines")

    def file_link(r):
        return html.A([icon("file"), " Invoice"], href=f"/inventory/invoices/{r['file']}", target="_blank",
                      className="link") if r.get("file") else None

    me = p["user"].get("username")
    approvals = html.Div(className="card", children=[
        html.H3(f"Awaiting approval ({len(pending)})"),
        html.Div(className="inv-receipts", children=[html.Div(className="inv-receipt", children=[
            html.Div([html.B(r["receipt"], className="mono"), html.Span(f" · {r.get('vendor') or 'vendor not given'}"),
                      html.Span(f" · invoice {r['invoice_no']}" if r.get("invoice_no") else ""), " ", file_link(r)]),
            html.Div(f"Entered by {r['drafted_by_name']}, {_when(r['drafted_at'])}"
                     + (f"; lines read by {r['read_by']}" if r.get("read_by") else ""), className="inv-hint"),
            lines_of(r),
            html.Div(className="inv-buttons", children=[
                dcc.Input(id={"type": "rcv-reason", "rid": r["receipt"]}, type="text", placeholder="Reason, if rejecting",
                          maxLength=300),
                html.Button("Approve: add to stock", id={"type": "rcv-approve", "rid": r["receipt"]}, n_clicks=0,
                            className="btn btn--primary",
                            disabled=not p["change"] or (p["second_person"] and r["drafted_by"] == me),
                            title="Someone other than whoever entered it approves" if r["drafted_by"] == me else None),
                html.Button("Reject", id={"type": "rcv-reject", "rid": r["receipt"]}, n_clicks=0, className="btn",
                            disabled=not p["change"]),
            ]),
        ]) for r in sorted(pending, key=lambda r: r["drafted_at"])] or [html.P("Nothing waiting.", className="muted")]),
    ])
    done = [r for r in st["receipts"].values() if r["status"] != "awaiting approval"]
    hist = pd.DataFrame([{"receipt": r["receipt"], "status": r["status"], "vendor": r.get("vendor", ""),
                          "invoice": r.get("invoice_no", ""), "when": r["drafted_at"],
                          "lines": "; ".join(f"{inv.fmt_qty(ln['qty'], ln['item'])} {inv.ITEMS[ln['item']]}"
                                             for ln in r["lines"]),
                          "who": f"{r['drafted_by_name']} → {r.get('approved_by_name') or r.get('rejected_by') or ''}",
                          "file": r.get("file", "")} for r in done])
    if len(hist):
        hist = hist.sort_values("when", ascending=False)
    history = html.Div(className="card", children=[html.H3("Received"), data_table(
        hist, [("receipt", "Receipt", None), ("when", "Entered", _when), ("status", "Status", None),
               ("vendor", "Vendor", None), ("invoice", "Invoice", None), ("lines", "What", None),
               ("who", "Entered → decided by", None),
               ("file", "File", lambda f: html.A("open", href=f"/inventory/invoices/{f}", target="_blank",
                                                 className="link") if f else "")], empty="No deliveries yet.")])
    return [form, approvals, history]


def read_upload(ds, contents: str, filename: str) -> tuple[str, str, bytes]:
    """Check and store an uploaded invoice. Returns (stored name, mime, bytes)."""
    if not contents or "," not in contents:
        raise inv.InventoryError("That upload didn't arrive.")
    data = base64.b64decode(contents.split(",", 1)[1], validate=False)
    if len(data) > MAX_UPLOAD:
        raise inv.InventoryError("That file is over 5 MB.")
    mime = next((m for sig, m in MAGIC.items() if data.startswith(sig)), None)
    if mime is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    if mime is None:
        raise inv.InventoryError("Upload a PDF, or a PNG, JPEG or WebP photo.")
    name = f"{hashlib.sha256(data).hexdigest()[:20]}.{EXT[mime]}"
    d = invoices_dir(ds.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return name, mime, data


# --- move -----------------------------------------------------------------------------------------------------

def _move(ds, st, p):
    if not p["change"]:
        return empty("Moving stock needs a named staff account.", big=False)
    opts = _loc_options(st)
    return [html.Div(className="card", children=[
        html.H3("Move stock"),
        html.P("Record it whenever paper changes places: central storage to a telecom or paper closet, a closet to "
               "under a kiosk, one closet to another. Parts (toner, drums, belts, fusers) stay in central storage "
               "until they go into a printer, and that's deducted automatically.", className="muted"),
        html.Div(className="inv-form", children=[
            html.Div(className="inv-pair", children=[
                html.Label(["Item", dcc.Dropdown(id="mv-item", options=ITEM_OPTIONS, value=inv.PAPER,
                                                 clearable=False)]),
                html.Label(["How much (paper in reams, to two decimals)",
                            dcc.Input(id="mv-qty", type="number", min=0, step=0.01, inputMode="decimal")])]),
            html.Div(className="inv-pair", children=[
                html.Label(["From", dcc.Dropdown(id="mv-from", options=opts)]),
                html.Label(["To", dcc.Dropdown(id="mv-to", options=opts)])]),
            html.Label(["Note (optional)", dcc.Input(id="mv-note", type="text", maxLength=300)]),
            html.Button("Record the move", id="mv-go", className="btn btn--primary", n_clicks=0),
        ])])]


# --- count and write-off ------------------------------------------------------------------------------------------

def _count(ds, st, p):
    if not p["change"]:
        return empty("Counting needs a named staff account.", big=False)
    opts = _loc_options(st)
    return [
        html.Div(className="card", children=[
            html.H3("Count a location"),
            html.P(["Count what's physically there and enter it; leave an item blank to skip it. The record is set to "
                    "your count. If anything is missing (more than half a ream of paper, or any part), an Inventory "
                    "investigation opens: missing stock needs an explanation and someone accountable. Paper is "
                    "counted at least weekly, by Friday's refills; part reams to two decimals (a half-used ream is "
                    "0.50)."], className="muted"),
            html.Div(className="inv-form", children=[
                html.Label(["Location", dcc.Dropdown(id="ct-loc", options=opts)]),
                html.Div(id="ct-fields", className="inv-counts"),
                html.Label(["Note (optional)", dcc.Input(id="ct-note", type="text", maxLength=300)]),
                html.Button("Save the count", id="ct-go", className="btn btn--primary", n_clicks=0),
            ])]),
        html.Div(className="card", children=[
            html.H3("Write off damaged, stolen or returned stock"),
            html.P("Damaged, stolen and returned stock is subtracted; found stock is added; a correction takes the "
                   "sign you enter. Link the investigation if there is one.", className="muted"),
            html.Div(className="inv-form", children=[
                html.Div(className="inv-pair", children=[
                    html.Label(["Item", dcc.Dropdown(id="wo-item", options=ITEM_OPTIONS)]),
                    html.Label(["How much", dcc.Input(id="wo-qty", type="number", step=0.01, inputMode="decimal")])]),
                html.Div(className="inv-pair", children=[
                    html.Label(["Location", dcc.Dropdown(id="wo-loc", options=opts)]),
                    html.Label(["Reason", dcc.Dropdown(id="wo-reason", options=[
                        {"label": r.capitalize(), "value": r} for r in inv.ADJUST_REASONS])])]),
                html.Label(["What happened", dcc.Textarea(id="wo-note", maxLength=1000)]),
                html.Label(["Investigation (if there is one)", dcc.Input(id="wo-inc", type="text", maxLength=20,
                                                                          placeholder="INV000000012")]),
                html.Button("Record it", id="wo-go", className="btn", n_clicks=0),
            ])]),
    ]


def count_fields(st: dict, loc: str | None):
    if not loc:
        return html.P("Pick a location.", className="inv-hint")
    kind = st["locations"].get(loc, {}).get("kind", "")
    items = [i for i in inv.ITEMS if inv.allowed(i, kind)]
    return [html.Label([f"{inv.ITEMS[i]}", html.Span(f"on record: {inv.fmt_qty(st['stock'].get((loc, i), 0.0), i)}",
                                                     className="inv-hint"),
                        dcc.Input(id={"type": "ct-n", "item": i}, type="number", min=0,
                                  step=0.01 if i == inv.PAPER else 1, inputMode="decimal")], className="inv-count")
            for i in items]


# --- weekly paper checks --------------------------------------------------------------------------------------

def _paper(ds, st, p):
    now = pd.Timestamp.now(tz="UTC")
    pc = inv.paper_checks(st, now)
    over = int((pc["status"] == "overdue").sum()) if len(pc) else 0
    pill = lambda s: html.Span(s, className=f"inv-state inv-state--{STATUS_TONE.get(s, 'neutral')}")  # noqa: E731
    return [
        html.P(["Paper is counted at least once a week at every place it's kept; Friday is refill day, so this week's "
                "count is due by Friday. Counts will be uneven, and that's fine: between counts, the app estimates "
                "with one tray's worth per refill it sees.",
                html.B(f" {over} overdue." if over else "")], className="muted"),
        data_table(pc, [("location_name", "Where", None), ("building", "Building", None),
                        ("reams", "Reams on record", lambda v: f"{v:,.2f}"),
                        ("last_counted", "Last counted", lambda t: _when(t) if t is not None and t == t else "never"),
                        ("status", "This week", pill)], max_rows=500,
                   empty="No paper locations yet: add paper closets on the Locations tab."),
        html.Div(dcc.Link("Count a location", href="/inventory?tab=count", className="btn btn--primary"),
                 className="inv-buttons") if p["change"] else None,
    ]


# --- locations ----------------------------------------------------------------------------------------------

def _buildings(ds) -> list[str]:
    b = set(ds.stations["building"]) | set(reference.load_buildings()["building"])
    return sorted(x for x in b if x)


def _locations(ds, st, p):
    locs = pd.DataFrame([L for L in st["locations"].values() if L["kind"] != "kiosk"],
                        columns=["id", "kind", "name", "building", "notes", "active"])
    if len(locs):
        locs["kind_label"] = locs["kind"].map(inv.LOCATION_KINDS)
        locs["state"] = locs["active"].map({True: "in use", False: "retired"})
        locs = locs.sort_values(["active", "kind", "name"], ascending=[False, True, True])
    table = data_table(locs, [("name", "Name", None), ("kind_label", "Kind", None), ("building", "Building", None),
                              ("notes", "Notes", None), ("state", "", None)], empty="No storage locations yet.")
    out = [html.P("Parts are kept only in central storage; paper can be in central storage, a building's telecom "
                  "closet, a hall's paper closet, or under a kiosk (those are automatic). Central storage exists "
                  "from the start even if nobody knows where it is: edit it to record the room once it's found. "
                  "Closets move too, so edit a location rather than adding a new one, and its history stays with it.",
                  className="muted"), html.Div(className="card", children=[html.H3("Storage locations"), table])]
    if not p["configure"]:
        return out + [html.P("Only the administrator adds or changes locations.", className="inv-hint")]
    bopts = [{"label": b, "value": b} for b in _buildings(ds)]
    mine = _loc_options(st, kinds=("central", "closet", "paper"))
    kiosks = ds.stations.sort_values(["building", "station_id"])
    out += [
        html.Div(className="card", children=[html.H3("Add a location"), html.Div(className="inv-form", children=[
            html.Label(["Kind", dcc.Dropdown(id="lc-kind", value="closet", clearable=False, options=[
                {"label": "Central storage", "value": "central"},
                {"label": "Telecom closet (a building's)", "value": "closet"},
                {"label": "Paper closet (a residence hall's)", "value": "paper"}])]),
            html.Label(["Name", dcc.Input(id="lc-name", type="text", maxLength=80,
                                          placeholder="e.g. Shea Hall telecom closet, room 012")]),
            html.P("Paper only: parts always stay in central storage.", className="inv-hint"),
            html.Label(["Building", dcc.Dropdown(id="lc-bld", options=bopts, placeholder="Building (not needed for "
                                                                                       "central storage)")]),
            html.Label(["Notes", dcc.Input(id="lc-notes", type="text", maxLength=300,
                                           placeholder="Who has the key, how to find it")]),
            html.Button("Add location", id="lc-go", className="btn btn--primary", n_clicks=0)])]),
        html.Div(className="card", children=[html.H3("Change or retire a location"), html.Div(className="inv-form",
                                                                                             children=[
            html.Label(["Location", dcc.Dropdown(id="le-loc", options=mine)]),
            html.Label(["New name", dcc.Input(id="le-name", type="text", maxLength=80)]),
            html.Label(["New building", dcc.Dropdown(id="le-bld", options=bopts)]),
            html.Label(["Notes", dcc.Input(id="le-notes", type="text", maxLength=300)]),
            html.Div(className="inv-buttons", children=[
                html.Button("Save changes", id="le-go", className="btn btn--primary", n_clicks=0),
                html.Button("Retire (must be empty)", id="le-retire", className="btn", n_clicks=0)])])]),
        html.Div(className="card", children=[html.H3("Paper per refill"), html.Div(className="inv-form", children=[
            html.P(f"How many reams one refill of a kiosk's tray uses (default {inv.DEFAULT_TRAY_REAMS:.2f}: a "
                   "550-sheet tray). Used for the estimates between counts.", className="inv-hint"),
            html.Label(["Kiosk", dcc.Dropdown(id="tr-sid", options=[
                {"label": f"{r.label} · {r.building}" + (f" (now {st['trays'][r.station_id]:.2f})"
                                                         if r.station_id in st["trays"] else ""),
                 "value": r.station_id} for r in kiosks.itertuples()])]),
            html.Label(["Reams per refill", dcc.Input(id="tr-reams", type="number", min=0.1, max=10, step=0.01)]),
            html.Button("Save", id="tr-go", className="btn", n_clicks=0)])]),
    ]
    return out


# --- keys -------------------------------------------------------------------------------------------------

def _keys(ds, st_inv, p):
    st = K.state(ds.data_dir)
    c = K.counts(st)
    ok, chain = K.ledger(ds.data_dir).verify()
    t = K.table(st)
    tiles = html.Div(className="tiles", children=[
        tile("Keys out", str(c["out"]), "regular and master"), tile("Master keys out", str(c["master_out"]), ""),
        tile("Lost, not recovered", str(c["lost"]), "each one is an investigation", ok=c["lost"] == 0),
        tile("People in the log", str(c["holders"]), "staff and student workers")])
    table = data_table(t, [
        ("name", "Name", None), ("email", "BSU email", None), ("position", "Position", None),
        ("resident", "Resident student", lambda v: "Yes" if v else "No"), ("hall", "Hall", None),
        ("key_type", "Key", None), ("key_no", "Key #", None), ("issued", "Issued", None),
        ("returned", "Returned", lambda v: v or "—"), ("status", "Status", None),
        ("keys_lost", "Keys lost", None),
        ("incident", "Investigation", lambda r: dcc.Link(r, href=f"/investigations/{r}", className="link mono")
         if r else "")], max_rows=500, empty="No keys recorded yet.")
    out = [html.P("Who holds a Wepa kiosk key. A lost key is an incident: it opens an investigation, and the holder "
                  "has a counseling conversation with management. This page holds personal details: staff only, "
                  "never shared with the AI assistant or included in exports.", className="muted"),
           tiles, html.Div(className="card", children=[html.H3("Keys"), table]),
           html.P([icon("shield" if ok else "alert"), html.Span(("Key log verified. " if ok else
                                                                 "KEY LOG PROBLEM: ") + chain)],
                  className="footnote footnote--icon" + ("" if ok else " inv-bad"))]
    if not p["keys_edit"]:
        return out
    halls = sorted(set(ds.stations[ds.stations["station_type"] == "residence"]["building"]))
    people = sorted(st["holders"].values(), key=lambda h: h["name"])
    keys_out = [k for k in st["keys"].values() if k["status"] in ("held", "lost")]
    today = pd.Timestamp.now(tz=config.LOCAL_TZ).date().isoformat()
    out += [
        html.Details(className="card inv-new", children=[
            html.Summary([icon("users"), html.Span("Add a person")]), html.Div(className="inv-form", children=[
                html.Div(className="inv-pair", children=[
                    html.Label(["Name", dcc.Input(id="kh-name", type="text", maxLength=80)]),
                    html.Label(["BSU email address", dcc.Input(id="kh-email", type="email", maxLength=120,
                                                               placeholder="name@bridgew.edu")])]),
                html.Label(["Position", dcc.Dropdown(id="kh-pos", options=list(K.POSITIONS))]),
                dcc.Checklist(id="kh-res", options=[{"label": " Resident student", "value": "y"}], value=[],
                              className="inv-check"),
                html.Label(["Residence hall (if a resident)", dcc.Dropdown(id="kh-hall", options=halls)]),
                html.Button("Add", id="kh-go", className="btn btn--primary", n_clicks=0)])]),
        html.Details(className="card inv-new", children=[
            html.Summary([icon("key"), html.Span("Hand out a key")]), html.Div(className="inv-form", children=[
                html.Label(["Person", dcc.Dropdown(id="ki-who", options=[
                    {"label": f"{h['name']} · {h['position']}", "value": h["id"]} for h in people])]),
                html.Div(className="inv-pair", children=[
                    html.Label(["Key", dcc.Dropdown(id="ki-type", value="regular", clearable=False, options=[
                        {"label": v, "value": k} for k, v in K.KEY_TYPES.items()])]),
                    html.Label(["Key number (stamped on it)", dcc.Input(id="ki-no", type="text", maxLength=30)])]),
                html.Label(["Date given", dcc.Input(id="ki-date", type="date", value=today)]),
                html.Button("Record", id="ki-go", className="btn btn--primary", n_clicks=0)])]),
        html.Details(className="card inv-new", children=[
            html.Summary([icon("alert"), html.Span("Key returned, lost or found")]), html.Div(className="inv-form", children=[
                html.Label(["Key", dcc.Dropdown(id="kr-key", options=[
                    {"label": f"{K.KEY_TYPES[k['key_type']]} {k.get('key_no') or ''} · "
                              f"{st['holders'][k['holder']]['name']} ({k['status']})", "value": k["id"]}
                    for k in keys_out])]),
                segmented("kr-what", [{"label": "Returned", "value": "return"}, {"label": "Lost", "value": "lost"},
                                      {"label": "Found after loss", "value": "found"}], "return"),
                html.Label(["Date", dcc.Input(id="kr-date", type="date", value=today)]),
                html.Label(["What happened (required when lost)", dcc.Textarea(id="kr-note", maxLength=1000)]),
                html.Button("Record", id="kr-go", className="btn btn--primary", n_clicks=0)])]),
    ]
    return out


# --- callbacks ---------------------------------------------------------------------------------------------

def _ok(text, extra=None):
    return html.Span([text, extra] if extra is not None else text, className="inv-ok")


def _err(exc):
    return html.Span(str(exc), className="inv-err")


def _clicked() -> bool:
    return bool(ctx.triggered and ctx.triggered[0].get("value"))


def register(app, cache) -> None:
    from flask import abort, send_file

    from ... import security

    @app.server.get("/inventory/invoices/<name>")
    def _invoice(name):
        p = perms()
        if not (p["change"] or p["configure"] or p["user"].get("role") == "staff"):
            abort(403)
        if not re.fullmatch(r"[0-9a-f]{20}\.(pdf|png|jpg|webp)", name):
            abort(404)
        path = invoices_dir(cache.get().data_dir) / name
        if not path.exists():
            abort(404)
        security.audit("invoice viewed", name)
        mime = {v: k for k, v in EXT.items()}[name.rsplit(".", 1)[1]]
        return send_file(path, mimetype=mime, download_name=name, max_age=0)

    @app.callback(Output("iv-body", "children"), Input("iv-tab", "value"), Input("iv-rev", "data"),
                  State("iv-sub", "data"))
    def body(tab, _rev, sub):
        return render(cache.get(), tab or "stock", sub or "receive")

    @app.callback([Output(f"iv-rec-{k}", "hidden") for k, _ in RECORD] + [Output("iv-sub", "data")],
                  Input("iv-rec", "value"), prevent_initial_call=True)
    def record_form(sub):
        return [k != sub for k, _ in RECORD] + [sub]

    app.clientside_callback(
        """function(tab, sub) {
            var q = '?tab=' + encodeURIComponent(tab || 'stock') + (tab === 'record' && sub ? '&do=' + sub : '');
            return window.location.search === q ? window.dash_clientside.no_update : q;
        }""", Output("url", "search", allow_duplicate=True), Input("iv-tab", "value"), Input("iv-sub", "data"),
        prevent_initial_call=True)

    @app.callback(Output("iv-tr-body", "children"), Input("iv-tr-item", "value"), Input("iv-tr-loc", "value"),
                  prevent_initial_call=True)
    def trail_filter(item, loc):
        ds = cache.get()
        return trail_table(ds, inv.state(ds.data_dir, ds.stations), item, loc)

    def done(msg, rev):
        return _ok(msg) if isinstance(msg, str) else msg, (rev or 0) + 1

    msg_rev = [Output("iv-msg", "children", allow_duplicate=True), Output("iv-rev", "data", allow_duplicate=True)]

    # receive: read an upload into the draft lines

    @app.callback(Output("rcv-read", "children"), Output("rcv-file", "data"), Output("rcv-vendor", "value"),
                  Output("rcv-no", "value"), Output({"type": "rcv-item", "i": ALL}, "value"),
                  Output({"type": "rcv-qty", "i": ALL}, "value"), Output({"type": "rcv-text", "i": ALL}, "value"),
                  Input("rcv-upload", "contents"), State("rcv-upload", "filename"), prevent_initial_call=True)
    def rcv_upload(contents, filename):
        if not contents:
            raise PreventUpdate
        if not perms()["change"]:
            return _err("Sign in with a staff account."), None, no_update, no_update, *([[no_update] * RECEIPT_ROWS] * 3)
        ds = cache.get()
        try:
            name, mime, data = read_upload(ds, contents, filename or "")
        except inv.InventoryError as exc:
            return _err(exc), None, no_update, no_update, *([[no_update] * RECEIPT_ROWS] * 3)
        got = inv.read_invoice(data, mime)
        lines = got["lines"][:RECEIPT_ROWS]
        items = [ln["item"] for ln in lines] + [None] * (RECEIPT_ROWS - len(lines))
        qtys = [ln["qty"] for ln in lines] + [None] * (RECEIPT_ROWS - len(lines))
        texts = [str(ln.get("text", ""))[:200] for ln in lines] + [""] * (RECEIPT_ROWS - len(lines))
        if lines:
            note = html.Span([icon("check"), f" Read {len(lines)} line{'s' if len(lines) != 1 else ''} using "
                              f"{got['read_by']}. Check each against the invoice and the boxes before submitting."])
        else:
            note = html.Span([icon("info"), " Saved the file, but couldn't read lines from it"
                              + (" (photos are read only when the AI model is configured)" if mime != "application/pdf"
                                 else "") + ". Type them in below."])
        return note, {"name": name, "read_by": got["read_by"] if lines else ""}, got.get("vendor", ""), got.get("invoice_no", ""), items, qtys, texts

    @app.callback(*msg_rev, Input("rcv-go", "n_clicks"), State("rcv-file", "data"), State("rcv-vendor", "value"),
                  State("rcv-no", "value"), State({"type": "rcv-item", "i": ALL}, "value"),
                  State({"type": "rcv-qty", "i": ALL}, "value"), State({"type": "rcv-to", "i": ALL}, "value"),
                  State({"type": "rcv-text", "i": ALL}, "value"), State("iv-rev", "data"), prevent_initial_call=True)
    def rcv_submit(n, file, vendor, no, items, qtys, tos, texts, rev):
        if not _clicked():
            raise PreventUpdate
        ds, p = cache.get(), perms()
        lines = [{"item": i, "qty": q, "to": t, "text": x or ""} for i, q, t, x in zip(items, qtys, tos, texts) if i]
        try:
            if not p["change"]:
                raise inv.InventoryError("Sign in with a staff account.")
            if any(ln["qty"] in (None, "") for ln in lines):
                raise inv.InventoryError("Every line with an item needs a quantity.")
            file = file or {}
            rid = inv.draft_receipt(ds.data_dir, _actor(p), lines, vendor or "", no or "", file.get("name", ""),
                                    source="upload" if file else "manual", stations=ds.stations,
                                    read_by=file.get("read_by", ""))
        except inv.InventoryError as exc:
            return _err(exc), no_update
        return done(f"{rid} entered. Someone else now checks it and approves it.", rev)

    @app.callback(*msg_rev, Input({"type": "rcv-approve", "rid": ALL}, "n_clicks"),
                  Input({"type": "rcv-reject", "rid": ALL}, "n_clicks"),
                  State({"type": "rcv-reason", "rid": ALL}, "value"), State({"type": "rcv-reason", "rid": ALL}, "id"),
                  State("iv-rev", "data"), prevent_initial_call=True)
    def rcv_decide(_a, _r, reasons, ids, rev):
        if not _clicked():
            raise PreventUpdate
        trig = ctx.triggered_id
        ds, p = cache.get(), perms()
        rid = trig["rid"]
        try:
            if not p["change"]:
                raise inv.InventoryError("Sign in with a staff account.")
            if trig["type"] == "rcv-approve":
                inv.approve_receipt(ds.data_dir, _actor(p), rid, sign_in_required=p["second_person"])
                return done(f"{rid} approved and added to stock.", rev)
            reason = dict(zip([i["rid"] for i in ids], reasons)).get(rid) or ""
            inv.reject_receipt(ds.data_dir, _actor(p), rid, reason)
            return done(f"{rid} rejected.", rev)
        except inv.InventoryError as exc:
            return _err(exc), no_update

    @app.callback(*msg_rev, Input("mv-go", "n_clicks"), State("mv-item", "value"), State("mv-qty", "value"),
                  State("mv-from", "value"), State("mv-to", "value"), State("mv-note", "value"), State("iv-rev", "data"),
                  prevent_initial_call=True)
    def mv(n, item, q, frm, to, note, rev):
        if not _clicked():
            raise PreventUpdate
        ds, p = cache.get(), perms()
        try:
            if not p["change"]:
                raise inv.InventoryError("Sign in with a staff account.")
            if q in (None, ""):
                raise inv.InventoryError("Enter how much.")
            inv.move(ds.data_dir, _actor(p), item, q, frm or "", to or "", note or "", stations=ds.stations)
        except inv.InventoryError as exc:
            return _err(exc), no_update
        return done(f"Moved {inv.fmt_qty(inv.qty(q, item), item)} of {inv.ITEMS[item]}.", rev)

    @app.callback(Output("ct-fields", "children"), Input("ct-loc", "value"))
    def ct_fields(loc):
        ds = cache.get()
        return count_fields(inv.state(ds.data_dir, ds.stations), loc)

    @app.callback(*msg_rev, Input("ct-go", "n_clicks"), State("ct-loc", "value"),
                  State({"type": "ct-n", "item": ALL}, "value"), State({"type": "ct-n", "item": ALL}, "id"),
                  State("ct-note", "value"), State("iv-rev", "data"), prevent_initial_call=True)
    def ct(n, loc, values, ids, note, rev):
        if not _clicked():
            raise PreventUpdate
        ds, p = cache.get(), perms()
        user = _actor(p)
        try:
            if not p["change"]:
                raise inv.InventoryError("Sign in with a staff account.")
            if not loc:
                raise inv.InventoryError("Pick a location.")
            out = inv.count(ds.data_dir, user, loc, {i["item"]: v for i, v in zip(ids, values)}, note or "",
                            stations=ds.stations,
                            on_shortfall=lambda name, short: inv.open_incident(ds.data_dir, user, name, short))
        except inv.InventoryError as exc:
            return _err(exc), no_update
        if out["incident"]:
            ref = out["incident"]
            return html.Span(["Count saved. Something is missing, so ", dcc.Link(ref, href=f"/investigations/{ref}",
                                                                                className="link mono"),
                              " is open to find out where it went."], className="inv-err"), (rev or 0) + 1
        return done("Count saved.", rev)

    @app.callback(*msg_rev, Input("wo-go", "n_clicks"), State("wo-item", "value"), State("wo-qty", "value"),
                  State("wo-loc", "value"), State("wo-reason", "value"), State("wo-note", "value"),
                  State("wo-inc", "value"), State("iv-rev", "data"), prevent_initial_call=True)
    def wo(n, item, q, loc, reason, note, incident, rev):
        if not _clicked():
            raise PreventUpdate
        ds, p = cache.get(), perms()
        try:
            if not p["change"]:
                raise inv.InventoryError("Sign in with a staff account.")
            if not item or not loc:
                raise inv.InventoryError("Pick the item and the location.")
            inv.adjust(ds.data_dir, _actor(p), item, q, loc, reason or "", note or "", stations=ds.stations,
                       incident=(incident or "").strip()[:20])
        except inv.InventoryError as exc:
            return _err(exc), no_update
        return done("Recorded.", rev)

    def need_configure():
        if not perms()["configure"]:
            raise inv.InventoryError("Only the administrator changes locations.")

    @app.callback(*msg_rev, Input("lc-go", "n_clicks"), State("lc-kind", "value"), State("lc-name", "value"),
                  State("lc-bld", "value"), State("lc-notes", "value"), State("iv-rev", "data"), prevent_initial_call=True)
    def lc(n, kind, name, bld, notes, rev):
        if not _clicked():
            raise PreventUpdate
        ds = cache.get()
        try:
            need_configure()
            if kind != "central" and not bld:
                raise inv.InventoryError("Pick the building.")
            inv.add_location(ds.data_dir, _actor(perms()), kind, name or "", bld or "", notes or "")
        except inv.InventoryError as exc:
            return _err(exc), no_update
        return done(f"Added {name}.", rev)

    @app.callback(*msg_rev, Input("le-go", "n_clicks"), Input("le-retire", "n_clicks"), State("le-loc", "value"),
                  State("le-name", "value"), State("le-bld", "value"), State("le-notes", "value"),
                  State("iv-rev", "data"), prevent_initial_call=True)
    def le(_g, _r, loc, name, bld, notes, rev):
        if not _clicked():
            raise PreventUpdate
        ds = cache.get()
        try:
            need_configure()
            if not loc:
                raise inv.InventoryError("Pick a location.")
            if ctx.triggered_id == "le-retire":
                inv.retire_location(ds.data_dir, _actor(perms()), loc)
                return done("Retired.", rev)
            inv.update_location(ds.data_dir, _actor(perms()), loc, name or None, bld, notes)
        except inv.InventoryError as exc:
            return _err(exc), no_update
        return done("Saved.", rev)

    @app.callback(*msg_rev, Input("tr-go", "n_clicks"), State("tr-sid", "value"), State("tr-reams", "value"),
                  State("iv-rev", "data"), prevent_initial_call=True)
    def tray(n, sid, reams, rev):
        if not _clicked():
            raise PreventUpdate
        ds = cache.get()
        try:
            need_configure()
            if not sid or reams in (None, ""):
                raise inv.InventoryError("Pick a kiosk and enter the reams.")
            inv.set_tray(ds.data_dir, _actor(perms()), sid, reams)
        except inv.InventoryError as exc:
            return _err(exc), no_update
        return done("Saved.", rev)

    def need_keys():
        if not perms()["keys_edit"]:
            raise K.KeyLogError("Only staff change the key log.")

    @app.callback(*msg_rev, Input("kh-go", "n_clicks"), State("kh-name", "value"), State("kh-email", "value"),
                  State("kh-pos", "value"), State("kh-res", "value"), State("kh-hall", "value"), State("iv-rev", "data"),
                  prevent_initial_call=True)
    def kh(n, name, email, pos, res, hall, rev):
        if not _clicked():
            raise PreventUpdate
        try:
            need_keys()
            K.add_holder(cache.get().data_dir, _actor(perms()), name or "", email or "", pos or "", bool(res), hall or "")
        except K.KeyLogError as exc:
            return _err(exc), no_update
        return done(f"Added {name}.", rev)

    @app.callback(*msg_rev, Input("ki-go", "n_clicks"), State("ki-who", "value"), State("ki-type", "value"),
                  State("ki-no", "value"), State("ki-date", "value"), State("iv-rev", "data"), prevent_initial_call=True)
    def ki(n, who, kind, no, date, rev):
        if not _clicked():
            raise PreventUpdate
        try:
            need_keys()
            K.issue(cache.get().data_dir, _actor(perms()), who or "", kind, no or "", date)
        except K.KeyLogError as exc:
            return _err(exc), no_update
        return done("Key recorded.", rev)

    @app.callback(*msg_rev, Input("kr-go", "n_clicks"), State("kr-key", "value"), State("kr-what", "value"),
                  State("kr-date", "value"), State("kr-note", "value"), State("iv-rev", "data"), prevent_initial_call=True)
    def kr(n, kid, what, date, note, rev):
        if not _clicked():
            raise PreventUpdate
        d, user = cache.get().data_dir, None
        try:
            need_keys()
            user = _actor(perms())
            if not kid:
                raise K.KeyLogError("Pick the key.")
            if what == "lost":
                ref = K.lose_key(d, user, kid, date, note or "")
                return html.Span(["Recorded as lost. ", dcc.Link(ref, href=f"/investigations/{ref}", className="link mono"),
                                  " is open: schedule the counseling conversation with management."],
                                 className="inv-err"), (rev or 0) + 1
            if what == "found":
                K.found_key(d, user, kid, date, note or "")
                return done("Recorded as found. The loss stays on the record.", rev)
            K.return_key(d, user, kid, date, note or "")
        except K.KeyLogError as exc:
            return _err(exc), no_update
        return done("Recorded as returned.", rev)
