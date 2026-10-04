"""Threat intelligence for the vulnerabilities found in the app's packages: is anyone exploiting them?

OSV.dev (vulns.py) says *which* known vulnerabilities affect the installed packages. This module looks
each CVE up in the sources security teams use to decide *how urgent* it is:

  CISA KEV            Known Exploited Vulnerabilities catalog: confirmed exploitation in the wild
  CISA Vulnrichment   CISA's SSVC decision points for a CVE (exploitation none/poc/active, automatable)
  NIST NVD            the official CVE record: CVSS score and vector, weakness (CWE), description
  Microsoft MSRC      Microsoft's Security Update Guide: whether Microsoft tracks it (Azure, Azure
                      Linux and Windows images), and whether Microsoft reports it exploited
  FIRST EPSS          the probability the CVE is exploited in the next 30 days
  Exploit-DB          public exploit code (Offensive Security's archive), by CVE
  Metasploit          a ready-made Metasploit Framework module, by CVE

Nothing is ever run against anything: these are look-ups of public catalogs, used only to rank what to
fix first. Catalogs are cached in records/intel/ (KEV daily; Exploit-DB and Metasploit weekly); per-CVE
answers for a week. Every source's last check, and any failure, is recorded and shown.
Set WEPA_NVD_API_KEY for faster NVD look-ups (otherwise one every 6 seconds, NVD's public limit).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from pathlib import Path

from .refs import records_dir

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
EXPLOITDB_URL = "https://gitlab.com/exploit-database/exploitdb/-/raw/main/files_exploits.csv"
METASPLOIT_URL = "https://raw.githubusercontent.com/rapid7/metasploit-framework/master/db/modules_metadata_base.json"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={}"
MSRC_URL = "https://api.msrc.microsoft.com/sug/v2.0/en-US/vulnerability?$filter=cveNumber%20eq%20%27{}%27"
VULNRICH_URL = "https://raw.githubusercontent.com/cisagov/vulnrichment/develop/{}/{}xxx/{}.json"
EPSS_URL = "https://api.first.org/data/v1/epss?cve={}"

DAY = 24 * 3600
FEED_MAX_AGE = {"kev": DAY, "exploitdb": 7 * DAY, "metasploit": 7 * DAY}
CVE_MAX_AGE = 7 * DAY
_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$")

SOURCES = [
    # key, name, what it tells us, link
    ("kev", "CISA Known Exploited Vulnerabilities", "Confirmed exploitation in the wild, with CISA's required action",
     "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"),
    ("vulnrichment", "CISA Vulnrichment (SSVC)", "Whether exploitation is none, proof-of-concept or active, and if "
     "it can be automated", "https://github.com/cisagov/vulnrichment"),
    ("nvd", "NIST National Vulnerability Database", "Official CVSS score, weakness type (CWE) and description",
     "https://nvd.nist.gov/"),
    ("msrc", "Microsoft Security Response Center", "Whether Microsoft tracks it (Azure, Azure Linux, Windows) and "
     "reports it exploited", "https://msrc.microsoft.com/update-guide"),
    ("epss", "FIRST EPSS", "Probability of exploitation in the next 30 days", "https://www.first.org/epss/"),
    ("exploitdb", "Exploit-DB", "Public exploit code exists", "https://www.exploit-db.com/"),
    ("metasploit", "Metasploit Framework", "A ready-made attack module exists",
     "https://github.com/rapid7/metasploit-framework"),
]
SOURCE_NAME = {k: n for k, n, _, _ in SOURCES}


def _dir(data_dir: Path) -> Path:
    d = records_dir(data_dir) / "intel"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, obj) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=str))
    tmp.replace(p)


def status(data_dir: Path) -> dict:
    """source -> {"checked": ts, "entries": n, "error": str} for the Software page."""
    return _read(_dir(data_dir) / "status.json", {})


def _note(data_dir: Path, source: str, **fields) -> None:
    p = _dir(data_dir) / "status.json"
    st = _read(p, {})
    st[source] = {**st.get(source, {}), **fields}
    if "error" not in fields:
        st[source].pop("error", None)
    _write(p, st)


# --- catalogs: downloaded whole, kept as small CVE indexes ---------------------------------------------

def _parse_kev(text: str) -> dict:
    d = json.loads(text)
    return {v["cveID"]: {"name": v.get("vulnerabilityName", ""), "added": v.get("dateAdded", ""),
                         "due": v.get("dueDate", ""), "action": v.get("requiredAction", ""),
                         "ransomware": v.get("knownRansomwareCampaignUse", "Unknown")}
            for v in d.get("vulnerabilities", []) if _CVE.match(v.get("cveID", ""))}


def _parse_exploitdb(text: str) -> dict:
    out: dict[str, list] = {}
    for row in csv.DictReader(io.StringIO(text)):
        for code in (row.get("codes") or "").split(";"):
            code = code.strip()
            if _CVE.match(code):
                out.setdefault(code, []).append({"id": row["id"], "title": row.get("description", "")[:160],
                                                 "type": row.get("type", ""), "platform": row.get("platform", ""),
                                                 "verified": row.get("verified") == "1",
                                                 "date": row.get("date_published", "")})
    return out


def _parse_metasploit(text: str) -> dict:
    out: dict[str, list] = {}
    for m in json.loads(text).values():
        for ref in m.get("references") or []:
            if _CVE.match(ref):
                out.setdefault(ref, []).append({"module": m.get("fullname", ""), "name": m.get("name", "")[:160],
                                                "type": m.get("type", ""), "rank": m.get("rank"),
                                                "disclosed": m.get("disclosure_date") or ""})
    return out


_PARSERS = {"kev": (KEV_URL, _parse_kev), "exploitdb": (EXPLOITDB_URL, _parse_exploitdb),
            "metasploit": (METASPLOIT_URL, _parse_metasploit)}


def catalog(data_dir: Path, source: str, http, log=print, force: bool = False) -> dict:
    p = _dir(data_dir) / f"{source}.json"
    cached = _read(p, None)
    st = status(data_dir).get(source, {})
    if cached is not None and not force and time.time() - st.get("checked", 0) < FEED_MAX_AGE[source]:
        return cached
    url, parse = _PARSERS[source]
    try:
        r = http.get(url, timeout=120)
        r.raise_for_status()
        index = parse(r.text)
    except Exception as exc:  # noqa: BLE001 - keep the previous copy
        _note(data_dir, source, error=f"{type(exc).__name__}: {exc}"[:200], error_at=time.time())
        log(f"{SOURCE_NAME[source]}: download failed ({type(exc).__name__}); using the copy from before")
        return cached or {}
    _write(p, index)
    _note(data_dir, source, checked=time.time(), entries=len(index))
    return index


# --- per-CVE look-ups ----------------------------------------------------------------------------------

def _nvd(cve: str, http) -> dict:
    headers = {"apiKey": os.environ["WEPA_NVD_API_KEY"]} if os.environ.get("WEPA_NVD_API_KEY") else {}
    r = http.get(NVD_URL.format(cve), headers=headers, timeout=30)
    r.raise_for_status()
    vs = r.json().get("vulnerabilities", [])
    if not vs:
        return {"found": False}
    c = vs[0]["cve"]
    metric = None
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30"):
        ms = c.get("metrics", {}).get(key) or []
        if ms:
            metric = next((m for m in ms if m.get("type") == "Primary"), ms[0])
            break
    cwes = sorted({d["value"] for w in c.get("weaknesses", []) for d in w.get("description", [])
                   if d.get("value", "").startswith("CWE-")})
    desc = next((d["value"] for d in c.get("descriptions", []) if d.get("lang") == "en"), "")
    out = {"found": True, "status": c.get("vulnStatus", ""), "cwes": cwes, "description": desc[:500],
           "published": c.get("published", "")[:10]}
    if metric:
        data = metric["cvssData"]
        out.update(score=data.get("baseScore"), vector=data.get("vectorString", ""),
                   severity=(data.get("baseSeverity") or metric.get("baseSeverity") or "").title(),
                   version=data.get("version", ""))
    if c.get("cisaExploitAdd"):
        out["kev_added"] = c["cisaExploitAdd"]
    return out


def _msrc(cve: str, http) -> dict:
    r = http.get(MSRC_URL.format(cve), timeout=30)
    r.raise_for_status()
    vals = r.json().get("value", [])
    if not vals:
        return {"tracked": False}
    v = vals[0]
    return {"tracked": True, "title": v.get("cveTitle") or "", "exploited": v.get("exploited", ""),
            "disclosed": v.get("publiclyDisclosed", ""), "severity": v.get("severity") or "",
            "released": (v.get("releaseDate") or "")[:10], "azure_linux": bool(v.get("isMariner")),
            "url": f"https://msrc.microsoft.com/update-guide/vulnerability/{cve}"}


def _vulnrichment(cve: str, http) -> dict:
    _, year, num = cve.split("-")
    r = http.get(VULNRICH_URL.format(year, int(num) // 1000, cve), timeout=30)
    if r.status_code == 404:
        return {"covered": False}
    r.raise_for_status()
    out = {"covered": True}
    for adp in r.json().get("containers", {}).get("adp", []):
        for m in adp.get("metrics", []):
            other = m.get("other", {})
            if other.get("type") == "ssvc":
                for opt in other.get("content", {}).get("options", []):
                    for k, v in opt.items():
                        out[k.lower().replace(" ", "_")] = str(v).lower()
    return out


def _epss(cves: list[str], http) -> dict:
    out = {}
    for i in range(0, len(cves), 50):
        r = http.get(EPSS_URL.format(",".join(cves[i:i + 50])), timeout=30)
        r.raise_for_status()
        for d in r.json().get("data", []):
            out[d["cve"]] = {"score": float(d["epss"]), "percentile": float(d["percentile"]), "date": d.get("date", "")}
    return out


def enrich(data_dir: Path, cves: list[str], session=None, log=print, force: bool = False,
           sleep=time.sleep) -> dict[str, dict]:
    """CVE -> what every source says about it. Never raises; a failed source is noted and skipped."""
    import requests
    http = session or requests
    cves = sorted({c for c in cves if _CVE.match(c)})
    kev = catalog(data_dir, "kev", http, log, force)
    edb = catalog(data_dir, "exploitdb", http, log, force)
    msf = catalog(data_dir, "metasploit", http, log, force)
    cache_p = _dir(data_dir) / "cves.json"
    cache = _read(cache_p, {})
    fresh = {c for c in cves if not force and time.time() - cache.get(c, {}).get("fetched", 0) < CVE_MAX_AGE}
    todo = [c for c in cves if c not in fresh]
    nvd_gap = 0.7 if os.environ.get("WEPA_NVD_API_KEY") else 6.2
    epss = {}
    if todo:
        try:
            epss = _epss(todo, http)
            _note(data_dir, "epss", checked=time.time(), entries=len(epss))
        except Exception as exc:  # noqa: BLE001
            _note(data_dir, "epss", error=f"{type(exc).__name__}: {exc}"[:200], error_at=time.time())
    for i, cve in enumerate(todo):
        rec = {"fetched": time.time(), "epss": epss.get(cve)}
        for key, fn in (("nvd", _nvd), ("msrc", _msrc), ("vulnrichment", _vulnrichment)):
            try:
                rec[key] = fn(cve, http)
                _note(data_dir, key, checked=time.time())
            except Exception as exc:  # noqa: BLE001
                rec[key] = None
                _note(data_dir, key, error=f"{type(exc).__name__}: {exc}"[:200], error_at=time.time())
        cache[cve] = rec
        if i + 1 < len(todo):
            sleep(nvd_gap)
    if todo:
        _write(cache_p, cache)
    out = {}
    for cve in cves:
        rec = dict(cache.get(cve, {}))
        rec["kev"] = kev.get(cve)
        rec["exploitdb"] = edb.get(cve, [])
        rec["metasploit"] = msf.get(cve, [])
        out[cve] = rec
    return out


# --- what it means -------------------------------------------------------------------------------------

PRIORITY_ORDER = ["Act now", "Soon", "Routine"]


def assess(intel: dict, severity: str, tooling: bool = False) -> tuple[str, list[str]]:
    """(priority, plain-English reasons) from the facts each source gave."""
    urgent, soon = [], []
    kev = intel.get("kev")
    if kev:
        urgent.append(f"CISA lists it as exploited in the wild (added {kev['added']}"
                      + (", used in ransomware campaigns" if kev.get("ransomware") == "Known" else "") + ").")
    vr = intel.get("vulnrichment") or {}
    if vr.get("exploitation") == "active":
        urgent.append("CISA's assessment: exploitation is active.")
    elif vr.get("exploitation") == "poc":
        soon.append("CISA's assessment: a proof-of-concept exploit exists.")
    if vr.get("automatable") == "yes":
        soon.append("CISA's assessment: attacks can be automated.")
    ms = intel.get("msrc") or {}
    if str(ms.get("exploited", "")).lower().startswith("exploitation detected") or ms.get("exploited") == "Yes":
        urgent.append("Microsoft reports it exploited.")
    if intel.get("metasploit"):
        n = len(intel["metasploit"])
        urgent.append(f"Metasploit has {n} ready-made module{'s' if n != 1 else ''} for it.")
    if intel.get("exploitdb"):
        n = len(intel["exploitdb"])
        soon.append(f"Exploit-DB has {n} public exploit{'s' if n != 1 else ''} for it.")
    ep = intel.get("epss") or {}
    if ep.get("score") is not None and ep["score"] >= 0.1:
        soon.append(f"FIRST EPSS gives a {ep['score']:.1%} chance of exploitation in the next 30 days "
                    f"(higher than {ep['percentile']:.1%} of all CVEs).")
    if severity in ("Critical", "High") and not urgent:
        soon.append(f"Rated {severity.lower()} severity.")
    prio = "Act now" if urgent else "Soon" if soon else "Routine"
    reasons = urgent + soon
    if tooling and prio == "Act now":
        prio = "Soon"
        reasons.append("Lowered one step: the package is only used to install software, not by the running app.")
    if not reasons:
        reasons.append("No source reports exploitation or public exploit code.")
    return prio, reasons


def sources_used(intel: dict) -> list[dict]:
    """[{"label", "url"}] linking to each source's own page for this CVE (only those that said something)."""
    cve = intel.get("cve", "")
    links = []
    if intel.get("kev"):
        links.append({"label": "CISA Known Exploited Vulnerabilities catalog",
                      "url": "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"})
    if (intel.get("nvd") or {}).get("found"):
        links.append({"label": "NVD record", "url": f"https://nvd.nist.gov/vuln/detail/{cve}"})
    if (intel.get("msrc") or {}).get("tracked"):
        links.append({"label": "Microsoft Security Update Guide", "url": intel["msrc"]["url"]})
    if (intel.get("vulnrichment") or {}).get("covered"):
        _, y, n = cve.split("-")
        links.append({"label": "CISA Vulnrichment", "url":
                      f"https://github.com/cisagov/vulnrichment/blob/develop/{y}/{int(n) // 1000}xxx/{cve}.json"})
    if intel.get("epss"):
        links.append({"label": "FIRST EPSS", "url": f"https://api.first.org/data/v1/epss?cve={cve}"})
    for e in (intel.get("exploitdb") or [])[:3]:
        links.append({"label": f"Exploit-DB {e['id']}", "url": f"https://www.exploit-db.com/exploits/{e['id']}"})
    for m in (intel.get("metasploit") or [])[:3]:
        links.append({"label": f"Metasploit {m['module']}", "url":
                      f"https://github.com/rapid7/metasploit-framework/blob/master/modules/{m['module']}.rb"})
    return links
