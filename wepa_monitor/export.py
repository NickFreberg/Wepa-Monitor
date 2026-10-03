"""Data export: the tables behind the dashboard as CSV, Excel, JSON or a printable PDF.

Every export uses the stations and period chosen in the header, carries a provenance block
(what, which stations, which period, when it was generated, whether it's demo data), and uses
local (Eastern) times. Nothing here changes data.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import activity, config, metrics as M, models, report_card

DATASETS = {
    "report_card": "Station report card",
    "incidents": "Outages and warnings",
    "faults": "Faults",
    "consumables": "Supplies: levels and forecasts",
    "usage": "Usage by printer",
    "activity": "Activity log",
    "everything": "All of these tables",
}
FORMATS = {"csv": "CSV", "xlsx": "Excel", "json": "JSON", "pdf": "PDF"}
MIME = {"csv": "text/csv", "zip": "application/zip", "json": "application/json", "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
TZ = config.LOCAL_TZ


class ExportError(Exception):
    pass


def _local(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True).dt.tz_convert(TZ).dt.strftime("%Y-%m-%d %H:%M")


def _window(ds: M.Dataset, period: str):
    return M.window(ds, None if period in (None, "all") else int(period))


def table(ds: M.Dataset, what: str, ids, start, end) -> pd.DataFrame:
    """One dataset as a tidy, human-labelled table."""
    names = activity.display_names(ds)
    if what == "report_card":
        c = report_card.build(ds, start, end, ids)
        if c.empty:
            return c
        return pd.DataFrame({
            "Station": c["label"], "Building": c["building"], "Area": c["area"], "Supported by": c["owner"],
            "Grade": c["grade"], "Score (0-100)": c["score"].round(1), "Verdict": c["verdict"],
            "Availability %": c["availability"].round(2), "Outages per week": c["outages_per_week"].round(2),
            "Faults vs campus (workload-adjusted)": c["faults_ratio"].round(2),
            "Parts wear vs campus (workload-adjusted)": c["wear_ratio"].round(2),
            "Drums/belt/fuser used per month (parts)": c["parts_per_month"].round(2),
            "Time in warning %": (c["warning_share"] * 100).round(1),
            "Usage vs typical printer": c["usage_relative"].round(2), "Main reason": c["why"],
            "Days observed": c["observed_days"].round(1)})
    if what == "incidents":
        inc = M._in(ds.sev_inc, "start", start, end, ids).copy()
        f = ds.fault_inc.assign(what=ds.fault_inc["label"] + ds.fault_inc["detail"].fillna("").map(
            lambda d: f" ({d})" if d else ""))
        cause = f.groupby(["station_id", "start"])["what"].agg(lambda s: "; ".join(sorted(set(s))))
        return pd.DataFrame({
            "Reference": inc["ref"] if "ref" in inc else "",
            "Station": inc["station_id"].map(names), "Station #": inc["station_id"],
            "Type": inc["severity"].map({"red": "Outage", "yellow": "Degraded"}),
            "Started": _local(inc["start"]), "Ended": _local(inc["end"]).where(inc["end"].notna(), ""),
            "Status": inc["status"].map({"resolved": "Fixed", "open": "Still open", "unknown_end": "Lost track"}),
            "Minutes": (inc["duration_s"] / 60).round(1),
            "Cause": [cause.get((s, t), "") for s, t in zip(inc["station_id"], inc["start"])],
            "Began during desk hours": inc["in_hours"].map({True: "Yes", False: "No"}),
            "Supported by": inc["owner"]}).sort_values("Started", ascending=False)
    if what == "faults":
        f = M._in(ds.fault_inc, "start", start, end, ids)
        return pd.DataFrame({
            "Station": f["station_id"].map(names), "Station #": f["station_id"], "Fault": f["label"],
            "Detail": f["detail"].fillna(""), "Started": _local(f["start"]),
            "Ended": _local(f["end"]).where(f["end"].notna(), ""), "Minutes": (f["duration_s"] / 60).round(1),
            "Fix": f["fix_category"]}).sort_values("Started", ascending=False)
    if what == "consumables":
        fc = models.eol_forecast(ds, ids)
        return pd.DataFrame({
            "Station": fc["station"], "Station #": fc["station_id"], "Part": fc["label"],
            "Level %": fc["level"].round(0), "Days to replacement": fc["days"].round(1),
            "Earliest (90%)": fc.get("days_early", pd.Series(np.nan, index=fc.index)).round(1),
            "Latest (90%)": fc.get("days_late", pd.Series(np.nan, index=fc.index)).round(1),
            "Method": fc["method"]}).sort_values(["Days to replacement", "Level %"])
    if what == "usage":
        u = M.usage_by_station(ds, start, end, ids)
        return pd.DataFrame({
            "Station": u["label"], "Building": u["building"], "Area": u["area"],
            "Black toner pts/day": u["usage_per_day"].round(2), "Color toner pts/day": u["color_per_day"].round(2),
            "Black cartridges per month": u["cartridges_per_month"].round(2),
            "vs typical printer": u["relative"].round(2), "Printers in building": u["printers_in_building"],
            "Days observed": u["observed_days"].round(1)})
    if what == "activity":
        ev = activity.events(ds, since=start, ids=ids)
        ev = ev[ev["ts"] < end]
        return pd.DataFrame({"When": _local(ev["ts"]), "What": ev["kind"].map(activity.KIND_LABEL),
                             "Station": ev["station"], "Building": ev["building"], "Summary": ev["title"],
                             "Detail": ev["detail"]})
    raise ExportError(f"Unknown dataset: {what}")


def build(ds: M.Dataset, what: str, fmt: str, ids, scope_text: str, period: str) -> tuple[bytes, str, str]:
    """(file bytes, filename, MIME type)."""
    if ds.empty:
        raise ExportError("There's no data to export yet.")
    if fmt not in FORMATS:
        raise ExportError(f"Unknown format: {fmt}")
    start, end = _window(ds, period)
    keys = [k for k in DATASETS if k != "everything"] if what == "everything" else [what]
    tables = {DATASETS[k]: table(ds, k, ids, start, end) for k in keys}
    stamp = ds.as_of.tz_convert(TZ)
    meta = {
        "title": "BSU print stations" + (" (DEMO DATA: synthetic, not real printers)" if ds.is_demo else ""),
        "contents": DATASETS[what], "stations": scope_text,
        "period": f"{start.tz_convert(TZ):%b %-d, %Y %-I:%M %p} to {end.tz_convert(TZ):%b %-d, %Y %-I:%M %p}",
        "generated": f"{datetime.now(ZoneInfo(TZ)):%Y-%m-%d %H:%M %Z}",
        "data_as_of": f"{stamp:%Y-%m-%d %H:%M %Z}",
        "source": "Wepa status page, collected every minute by ResNet Print Ops",
        "notes": "Times are US Eastern. Usage is toner burned (Wepa publishes no page counts). Supplies are "
                 "counted in parts, not dollars.",
    }
    base = f"bsu-print-{what.replace('_', '-')}-{stamp:%Y%m%d-%H%M}"
    if fmt == "csv":
        if len(tables) == 1:
            t = next(iter(tables.values()))
            return t.to_csv(index=False).encode("utf-8-sig"), base + ".csv", MIME["csv"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, t in tables.items():
                z.writestr(name.lower().replace(":", "").replace(" ", "-") + ".csv", t.to_csv(index=False))
            z.writestr("README.txt", "\n".join(f"{k}: {v}" for k, v in meta.items()))
        return buf.getvalue(), base + ".zip", MIME["zip"]
    if fmt == "json":
        doc = {"about": meta, "tables": {name: json.loads(t.to_json(orient="records")) for name, t in tables.items()}}
        return json.dumps(doc, indent=2).encode(), base + ".json", MIME["json"]
    if fmt == "xlsx":
        return _xlsx(tables, meta), base + ".xlsx", MIME["xlsx"]
    return _pdf(tables, meta), base + ".pdf", MIME["pdf"]


def _xlsx(tables: dict[str, pd.DataFrame], meta: dict) -> bytes:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        about = pd.DataFrame({"": list(meta.keys()), " ": list(meta.values())})
        about.to_excel(xw, sheet_name="About", index=False)
        for name, t in tables.items():
            t.to_excel(xw, sheet_name=name[:31].replace(":", ""), index=False)
        head = PatternFill("solid", fgColor="89191F")
        for ws in xw.book.worksheets:
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = head
                cell.alignment = Alignment(vertical="center", wrap_text=True)
            ws.freeze_panes = "A2"
            for i, col in enumerate(ws.columns, start=1):
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))
    return buf.getvalue()


_LATIN = str.maketrans({"–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "×": "x", "…": "...",
                        "·": "-", "≥": ">=", "≤": "<=", "→": "->", "•": "-"})


def _latin1(v) -> str:
    """The PDF's built-in fonts are Latin-1 only (no font files needed on the server)."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return ""
    s = str(v).translate(_LATIN)
    return s.encode("latin-1", "replace").decode("latin-1")


def _pdf(tables: dict[str, pd.DataFrame], meta: dict) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    class Doc(FPDF):
        def header(self):
            self.set_fill_color(137, 25, 31)
            self.rect(0, 0, self.w, 14, "F")
            self.set_xy(10, 4)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 11)
            self.cell(0, 6, _latin1("ResNet Print Ops - " + meta["title"]))
            self.set_text_color(0, 0, 0)
            self.ln(14)

        def footer(self):
            self.set_y(-10)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(110, 110, 110)
            self.cell(0, 5, _latin1(f"Generated {meta['generated']} - {meta['source']} - page {self.page_no()}"),
                      align="C")

    pdf = Doc(orientation="L", unit="mm", format="Letter")
    pdf.set_auto_page_break(True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, _latin1(meta["contents"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for k in ("stations", "period", "data_as_of", "notes"):
        pdf.multi_cell(0, 4.5, _latin1(f"{k.replace('_', ' ').capitalize()}: {meta[k]}"), new_x="LMARGIN",
                       new_y="NEXT")
    for name, t in tables.items():
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 7, _latin1(f"{name} ({len(t):,} rows{', first 300 shown' if len(t) > 300 else ''})"),
                 new_x="LMARGIN", new_y="NEXT")
        if t.empty:
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 6, "Nothing in this period.", new_x="LMARGIN", new_y="NEXT")
            continue
        t = t.head(300)
        cols = list(t.columns)[:12]
        pdf.set_font("Helvetica", "", 6.5 if len(cols) > 9 else 7.5)
        with pdf.table(headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=(137, 25, 31)),
                       line_height=3.6, text_align="LEFT", first_row_as_headings=True,
                       cell_fill_color=(246, 243, 238), cell_fill_mode="ROWS") as tb:
            tb.row([_latin1(c) for c in cols])
            for row in t[cols].itertuples(index=False):
                tb.row([_latin1(v)[:80] for v in row])
    return bytes(pdf.output())
