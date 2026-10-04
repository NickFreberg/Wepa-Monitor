"""Known vulnerabilities in the packages this copy of the app is actually running.

Once a day (and on demand from the Software page) every installed Python package and version is
looked up in OSV.dev, the open vulnerability database that aggregates the PyPI advisory database and
GitHub security advisories. Each finding is kept with its CVE/GHSA identifiers, severity, the first
version that fixes it, a plain-English summary and links to the original advisories. Results are
cached in records/vulns.json; new and fixed findings are written to the activity feed.

Only package names and version numbers are sent to OSV, never any data the app has collected.
Set WEPA_VULN_CHECK=0 to turn the lookup off (e.g. on a network without internet access).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path

from .refs import records_dir

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
MAX_AGE_S = 24 * 3600
MIN_MANUAL_GAP_S = 600
SEVERITY_ORDER = ["Critical", "High", "Moderate", "Low", "Not rated"]
_NAME = re.compile(r"[-_.]+")
TOOLING = {"pip", "setuptools", "wheel", "uv"}


def enabled() -> bool:
    return os.environ.get("WEPA_VULN_CHECK", "1") != "0"


def canonical(name: str) -> str:
    return _NAME.sub("-", name).lower()


@dataclass
class Finding:
    package: str
    installed: str
    ids: list[str]                      # every identifier, CVE first (CVE-…, GHSA-…, PYSEC-…)
    severity: str
    summary: str                        # plain English, one or two sentences
    fixed: str | None                   # first version that fixes it, if any
    published: str = ""
    direct: bool = False                # listed in requirements.txt (vs pulled in by another package)
    required_by: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)   # [{"label", "url"}]
    tooling: bool = False               # pip/setuptools/wheel: used to install packages, not by the running app

    @property
    def key(self) -> str:
        return f"{self.package}:{self.ids[0]}"

    @property
    def headline_id(self) -> str:
        return self.ids[0]


# --- what is installed ---------------------------------------------------------------------------------

def installed() -> dict[str, str]:
    out = {}
    for d in metadata.distributions():
        name = d.metadata["Name"] if d.metadata else None
        if name:
            out[canonical(name)] = d.version
    return out


def direct_requirements(root: Path | None = None) -> set[str]:
    path = (root or Path(__file__).resolve().parent.parent) / "requirements.txt"
    if not path.exists():
        return set()
    names = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(canonical(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0]))
    return names


def required_by() -> dict[str, list[str]]:
    """package -> the installed packages that depend on it (to explain why it is here)."""
    out: dict[str, set[str]] = {}
    for d in metadata.distributions():
        parent = canonical(d.metadata["Name"]) if d.metadata and d.metadata["Name"] else None
        for req in d.requires or []:
            if "extra ==" in req:
                continue
            child = canonical(re.split(r"[<>=!~;\[ (]", req.strip(), maxsplit=1)[0])
            if parent:
                out.setdefault(child, set()).add(parent)
    return {k: sorted(v) for k, v in out.items()}


# --- OSV -----------------------------------------------------------------------------------------------

def _version_key(v: str):
    try:
        from packaging.version import Version
        return (0, Version(v))
    except Exception:  # noqa: BLE001 - unparseable versions sort last
        return (1, v)


def _fixed_version(record: dict, package: str, current: str) -> str | None:
    fixes = []
    for aff in record.get("affected", []):
        pkg = aff.get("package", {})
        if pkg.get("ecosystem") != "PyPI" or canonical(pkg.get("name", "")) != package:
            continue
        for rng in aff.get("ranges", []):
            fixes += [e["fixed"] for e in rng.get("events", []) if "fixed" in e]
    newer = [f for f in fixes if _version_key(f) > _version_key(current)]
    return min(newer, key=_version_key) if newer else (max(fixes, key=_version_key) if fixes else None)


def _severity(records: list[dict]) -> str:
    for r in records:
        s = str((r.get("database_specific") or {}).get("severity") or "").title()
        if s == "Medium":
            s = "Moderate"
        if s in SEVERITY_ORDER:
            return s
    for r in records:
        for s in r.get("severity", []):
            score = _cvss_base(s.get("score", ""))
            if score is not None:
                return ("Critical" if score >= 9 else "High" if score >= 7 else "Moderate" if score >= 4 else "Low")
    return "Not rated"


def _cvss_base(vector: str) -> float | None:
    """CVSS v3 base score from its vector string (the published formula), so unrated records still get one."""
    if not vector.startswith("CVSS:3"):
        return None
    try:
        m = dict(p.split(":") for p in vector.split("/")[1:])
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[m["AV"]]
        ac = {"L": 0.77, "H": 0.44}[m["AC"]]
        changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[m["PR"]]
        ui = {"N": 0.85, "R": 0.62}[m["UI"]]
        cia = [{"H": 0.56, "L": 0.22, "N": 0.0}[m[k]] for k in ("C", "I", "A")]
    except (KeyError, ValueError):
        return None
    iss = 1 - (1 - cia[0]) * (1 - cia[1]) * (1 - cia[2])
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    expl = 8.22 * av * ac * pr * ui
    raw = min(1.08 * (impact + expl), 10) if changed else min(impact + expl, 10)
    return -(-round(raw * 100_000) // 10_000) / 10   # "round up" to one decimal, as the spec defines


def _plain(records: list[dict]) -> str:
    for r in records:
        s = (r.get("summary") or "").strip()
        if s:
            return s.rstrip(".") + "."
    for r in records:
        d = re.sub(r"\s+", " ", re.sub(r"[#*`>\[\]]", "", r.get("details") or "")).strip()
        if d:
            first = re.split(r"(?<=[.!?])\s", d, maxsplit=1)[0]
            return first[:300]
    return "No description was published."


def _links(ids: list[str], records: list[dict]) -> list[dict]:
    links, seen = [], set()

    def add(label, url):
        if url not in seen and url.startswith("https://"):
            seen.add(url)
            links.append({"label": label, "url": url})

    for i in ids:
        if i.startswith("CVE-"):
            add(f"{i} (NIST National Vulnerability Database)", f"https://nvd.nist.gov/vuln/detail/{i}")
        elif i.startswith("GHSA-"):
            add(f"{i} (GitHub advisory)", f"https://github.com/advisories/{i}")
        add(f"{i} (OSV.dev)", f"https://osv.dev/vulnerability/{i}")
    for r in records:
        for ref in r.get("references", [])[:6]:
            kind = ref.get("type", "WEB").replace("_", " ").title()
            label = {"Fix": "The fix (code change)", "Advisory": "Advisory", "Report": "Original report",
                     "Package": "Package page"}.get(kind, "More information")
            add(label, ref.get("url", ""))
    return links[:12]


def _group(vuln_records: list[dict]) -> list[list[dict]]:
    """PYSEC, GHSA and CVE records often describe the same flaw: group records that share an identifier."""
    groups: list[tuple[set, list]] = []
    for r in vuln_records:
        names = {r["id"], *r.get("aliases", [])}
        for g in groups:
            if g[0] & names:
                g[0].update(names)
                g[1].append(r)
                break
        else:
            groups.append((set(names), [r]))
    return [g[1] for g in groups]


def _ids(records: list[dict]) -> list[str]:
    ids = {x for r in records for x in (r["id"], *r.get("aliases", []))}
    rank = lambda i: (0 if i.startswith("CVE-") else 1 if i.startswith("GHSA-") else 2, i)  # noqa: E731
    return sorted(ids, key=rank)


def scan(packages: dict[str, str] | None = None, session=None, timeout: float = 20) -> list[Finding]:
    """Ask OSV about every package; raises on network errors (the caller keeps the last good result)."""
    import requests
    http = session or requests
    packages = packages if packages is not None else installed()
    names = sorted(packages)
    hits: dict[str, list[str]] = {}
    for i in range(0, len(names), 500):
        chunk = names[i:i + 500]
        body = {"queries": [{"package": {"name": n, "ecosystem": "PyPI"}, "version": packages[n]} for n in chunk]}
        r = http.post(OSV_BATCH, json=body, timeout=timeout)
        r.raise_for_status()
        for n, res in zip(chunk, r.json().get("results", [])):
            ids = [v["id"] for v in res.get("vulns", [])]
            if ids:
                hits[n] = ids
    direct, parents = direct_requirements(), required_by()
    out = []
    for pkg, ids in hits.items():
        records = []
        for vid in ids:
            r = http.get(OSV_VULN.format(vid), timeout=timeout)
            if r.ok:
                records.append(r.json())
        for group in _group(records):
            all_ids = _ids(group)
            out.append(Finding(
                package=pkg, installed=packages[pkg], ids=all_ids, severity=_severity(group),
                summary=_plain(group), fixed=_fixed_version(group[0], pkg, packages[pkg]),
                published=min((r.get("published") or "")[:10] for r in group),
                direct=pkg in direct, required_by=parents.get(pkg, []), links=_links(all_ids, group),
                tooling=pkg in TOOLING))
    return sorted(out, key=lambda f: (SEVERITY_ORDER.index(f.severity), f.package))


# --- cache, refresh and events ------------------------------------------------------------------------

def _cache_path(data_dir: Path) -> Path:
    return records_dir(data_dir) / "vulns.json"


def load(data_dir: Path) -> dict:
    p = _cache_path(data_dir)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def findings(data_dir: Path) -> list[Finding]:
    return [Finding(**f) for f in load(data_dir).get("findings", [])]


def refresh(data_dir: Path, force: bool = False, log=print, session=None) -> dict:
    """Re-check if the last check is a day old (or force=True, at most every 10 minutes). Never raises."""
    from . import sysevents
    prev = load(data_dir)
    age = time.time() - prev.get("checked_at", 0)
    if not enabled():
        return {**prev, "skipped": "turned off (WEPA_VULN_CHECK=0)"}
    if (not force and age < MAX_AGE_S) or (force and age < MIN_MANUAL_GAP_S and "error" not in prev):
        return prev
    packages = installed()
    try:
        found = scan(packages, session=session)
    except Exception as exc:  # noqa: BLE001 - keep the last good result
        out = {**prev, "error": f"{type(exc).__name__}: {exc}"[:300], "error_at": time.time()}
        _write(data_dir, out)
        log(f"vulnerability check failed: {out['error']}")
        return out
    out = {"checked_at": time.time(), "source": "OSV.dev (PyPI advisory database, GitHub advisories)",
           "packages": len(packages), "findings": [asdict(f) for f in found]}
    before = {f"{f['package']}:{f['ids'][0]}": f for f in prev.get("findings", [])}
    after = {f.key: f for f in found}
    if prev.get("checked_at"):
        for k in after.keys() - before.keys():
            f = after[k]
            sysevents.add("vuln_found", f"{f.headline_id} in {f.package} {f.installed}",
                          f"{f.severity}: {f.summary}" + (f" Fixed in {f.fixed}." if f.fixed else " No fix yet."),
                          href=f"/software#{f.headline_id}", data_dir=data_dir,
                          severity="critical" if f.severity in ("Critical", "High") else "warning")
        for k in before.keys() - after.keys():
            f = before[k]
            sysevents.add("vuln_resolved", f"{f['ids'][0]} no longer affects this app",
                          f"{f['package']} is no longer at an affected version.", href="/software",
                          data_dir=data_dir)
    _write(data_dir, out)
    log(f"vulnerability check: {len(packages)} packages, {len(found)} known "
        f"vulnerabilit{'y' if len(found) == 1 else 'ies'}")
    return out


def _write(data_dir: Path, obj: dict) -> None:
    p = _cache_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(p)


def counts(fs: list[Finding]) -> dict[str, int]:
    out = {s: 0 for s in SEVERITY_ORDER}
    for f in fs:
        out[f.severity] = out.get(f.severity, 0) + 1
    return out


def sentence(data_dir: Path) -> str:
    """One factual line for the self-check and the assistant."""
    c = load(data_dir)
    if not c.get("checked_at"):
        return "Dependencies haven't been checked for vulnerabilities yet."
    fs = [Finding(**f) for f in c.get("findings", [])]
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(c["checked_at"]))
    if not fs:
        return f"No known vulnerabilities in the {c.get('packages', 0)} installed packages (checked {when})."
    n = counts(fs)
    parts = ", ".join(f"{v} {k.lower()}" for k, v in n.items() if v)
    fixable = sum(1 for f in fs if f.fixed)
    return (f"{len(fs)} known vulnerabilit{'y' if len(fs) == 1 else 'ies'} in installed packages ({parts}); "
            f"{fixable} have a fixed version available (checked {when}).")
