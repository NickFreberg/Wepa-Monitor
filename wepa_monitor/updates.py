"""Versions, the change log, and software update packages.

* The running version is wepa_monitor.__version__. Each time the app starts on a different version it
  is recorded in records/versions.jsonl and announced in the activity feed ("Software updated").
* CHANGELOG.md is the plain-English history; the Change log pages render it.
* An update package (UPD reference number) lists the package upgrades that fix the open
  vulnerabilities. The administrator can start it from the Software page: the app asks GitHub to run
  the update workflow (.github/workflows/update.yml), which changes the versions, regenerates the
  hash-pinned lock file, adds a change-log entry, runs every test and security scan, and opens a pull
  request. The app never modifies or installs its own code; the reviewed pull request and the normal
  deploy do that.

Environment for starting updates from the app:
    WEPA_GITHUB_TOKEN   fine-grained token for this repository only, permission "Actions: read and write"
    WEPA_GITHUB_REPO    owner/name (default nickfreberg/wepa-monitor)
    WEPA_GITHUB_BRANCH  branch the workflow runs on (default master)
"""
from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - fixed git arguments only, never user input
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .refs import _FileLock, records_dir

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
WORKFLOW = "update.yml"
_RELEASE = re.compile(r"^## \[(?P<v>[^\]]+)\]\s*-\s*(?P<d>\d{4}-\d{2}-\d{2})\s*$")
_VERSION_OK = re.compile(r"^\d+\.\d+\.\d+([.-]?[A-Za-z0-9]+)?$")
_PKG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!_-]*$")


# --- change log ----------------------------------------------------------------------------------------

@dataclass
class Release:
    version: str
    date: str
    summary: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)

    @property
    def security_ids(self) -> list[str]:
        text = " ".join(self.sections.get("Security", []))
        return sorted(set(re.findall(r"\b(?:CVE-\d{4}-\d{4,}|GHSA(?:-[a-z0-9]{4}){3})\b", text)))


def changelog(path: Path | None = None) -> list[Release]:
    path = path or CHANGELOG
    if not path.exists():
        return []
    out: list[Release] = []
    cur, section = None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _RELEASE.match(line.strip())
        if m:
            cur = Release(m["v"], m["d"])
            out.append(cur)
            section = None
            continue
        if cur is None:
            continue
        if line.startswith("### "):
            section = line[4:].strip()
            cur.sections.setdefault(section, [])
        elif line.startswith("- ") and section:
            cur.sections[section].append(line[2:].strip())
        elif line.strip() and section is None:
            cur.summary = (cur.summary + " " + line.strip()).strip()
    return out


def release(version: str) -> Release | None:
    return next((r for r in changelog() if r.version == version), None)


# --- the running version -------------------------------------------------------------------------------

def build_info() -> dict:
    """Version, plus the git commit when known (deploys write build.json; a checkout asks git)."""
    info = {"version": __version__}
    p = ROOT / "build.json"
    try:
        info.update(json.loads(p.read_text()))
    except (OSError, ValueError):
        try:
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,  # nosec B603 B607
                                 text=True, timeout=5).stdout.strip()
            if sha:
                info["commit"] = sha
        except (OSError, subprocess.SubprocessError):
            pass
    info["version"] = __version__
    return info


def history(data_dir: Path) -> list[dict]:
    p = records_dir(data_dir) / "versions.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def record_running(data_dir: Path) -> dict | None:
    """Called at start-up: note a new version once, with an activity event linking to its change log."""
    from . import sysevents
    p = records_dir(data_dir) / "versions.jsonl"
    with _FileLock(p.with_suffix(".lock")):
        past = history(data_dir)
        last = past[-1]["version"] if past else None
        if last == __version__:
            return None
        info = build_info()
        row = {"ts": time.time(), "version": __version__, "previous": last, "commit": info.get("commit", "")}
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row) + "\n")
    rel = release(__version__)
    n_sec = len(rel.sections.get("Security", [])) if rel else 0
    detail = (rel.summary if rel and rel.summary else "") + (f" Includes {n_sec} security change"
                                                            f"{'s' if n_sec != 1 else ''}." if n_sec else "")
    title = (f"Software updated to version {__version__} (from {last})" if last
             else f"Version {__version__} is running (first recorded version)")
    sysevents.add("software_updated", title, detail.strip(), href=f"/changelog/{__version__}", data_dir=data_dir,
                  version=__version__, previous=last or "")
    return row


# --- update packages -----------------------------------------------------------------------------------

def _updates_path(data_dir: Path) -> Path:
    return records_dir(data_dir) / "updates.jsonl"


def packages(data_dir: Path) -> list[dict]:
    """Every update package, latest state of each, newest first."""
    p = _updates_path(data_dir)
    by_ref: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by_ref[r["ref"]] = {**by_ref.get(r["ref"], {}), **r}
    return sorted(by_ref.values(), key=lambda r: r.get("created", 0), reverse=True)


def _append(data_dir: Path, row: dict) -> None:
    p = _updates_path(data_dir)
    with _FileLock(p.with_suffix(".lock")):
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")


def plan(data_dir: Path) -> dict:
    """Which upgrades fix the open vulnerabilities: per package, the lowest version that fixes all of them."""
    from . import vulns
    from .vulns import _version_key
    by_pkg: dict[str, dict] = {}
    for f in vulns.findings(data_dir):
        e = by_pkg.setdefault(f.package, {"package": f.package, "installed": f.installed, "target": None,
                                          "fixes": [], "unfixed": [], "tooling": f.tooling, "direct": f.direct})
        if f.fixed:
            e["fixes"].append(f.headline_id)
            if e["target"] is None or _version_key(f.fixed) > _version_key(e["target"]):
                e["target"] = f.fixed
        else:
            e["unfixed"].append(f.headline_id)
    items = sorted(by_pkg.values(), key=lambda e: (e["tooling"], e["package"]))
    upgrades = [e for e in items if e["target"] and not e["tooling"]]
    return {"items": items, "upgrades": upgrades,
            "spec": " ".join(f"{e['package']}=={e['target']}" for e in upgrades),
            "tooling": [e for e in items if e["tooling"]]}


def prepare(data_dir: Path, by: str) -> dict:
    """Freeze the current plan as an update package with its own reference number."""
    from . import refs, sysevents
    p = plan(data_dir)
    if not p["upgrades"]:
        raise ValueError("Nothing to update: no open vulnerability in the app's packages has a fixed version.")
    record = {"kind": "update", "spec": p["spec"]}
    ref = refs.next_number(data_dir, "UPD", record)
    row = {"ref": ref, "created": time.time(), "by": by, "state": "Prepared", "spec": p["spec"],
           "upgrades": p["upgrades"], "from_version": __version__}
    _append(data_dir, row)
    fixes = sorted({i for e in p["upgrades"] for i in e["fixes"]})
    sysevents.add("update_prepared", f"{ref}: update package prepared",
                  f"{len(p['upgrades'])} package upgrade{'s' if len(p['upgrades']) != 1 else ''} fixing "
                  f"{', '.join(fixes)}", href=f"/software#{ref}", data_dir=data_dir, ref=ref)
    return row


def can_start() -> bool:
    return bool(os.environ.get("WEPA_GITHUB_TOKEN"))


def repo() -> str:
    return os.environ.get("WEPA_GITHUB_REPO", "nickfreberg/wepa-monitor")


def start(data_dir: Path, ref: str, by: str, session=None) -> dict:
    """Ask GitHub to build the update (workflow_dispatch). Raises ValueError with a plain message."""
    from . import sysevents
    pkg = next((p for p in packages(data_dir) if p["ref"] == ref), None)
    if pkg is None:
        raise ValueError(f"No update package {ref}.")
    if pkg["state"] != "Prepared":
        raise ValueError(f"{ref} is already {pkg['state'].lower()}.")
    spec = pkg["spec"].split()
    if not spec or not all(_PKG_OK.match(s) for s in spec):
        raise ValueError("The update package doesn't look right; prepare a new one.")
    if not can_start():
        raise ValueError("Starting updates from the app needs WEPA_GITHUB_TOKEN (see docs/SECURITY.md). "
                         "You can run the update workflow from GitHub's Actions tab with the same packages.")
    import requests
    http = session or requests
    branch = os.environ.get("WEPA_GITHUB_BRANCH", "master")
    r = http.post(f"https://api.github.com/repos/{repo()}/actions/workflows/{WORKFLOW}/dispatches",
                  headers={"Authorization": f"Bearer {os.environ['WEPA_GITHUB_TOKEN']}",
                           "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                  json={"ref": branch, "inputs": {"packages": pkg["spec"], "ticket": ref, "requested_by": by}},
                  timeout=20)
    if r.status_code != 204:
        raise ValueError(f"GitHub refused the request ({r.status_code}). Check the token's permissions.")
    row = {"ref": ref, "state": "Requested", "requested": time.time(), "requested_by": by,
           "runs": f"https://github.com/{repo()}/actions/workflows/{WORKFLOW}"}
    _append(data_dir, row)
    sysevents.add("update_requested", f"{ref}: update started by {by}",
                  f"GitHub is testing {pkg['spec']}; a pull request follows if every check passes.",
                  href=row["runs"], data_dir=data_dir, ref=ref)
    return {**pkg, **row}


def manual_steps(spec: str) -> list[str]:
    """What the workflow does, for running it by hand."""
    return [f"python scripts/prepare_update.py {spec}",
            "pytest -q && pip-audit --disable-pip --require-hashes -r requirements.lock",
            "git commit -am 'Security update' && open a pull request"]


def version_key(v: str):
    from .vulns import _version_key
    return _version_key(v)


def valid_version(v: str) -> bool:
    return bool(_VERSION_OK.match(v or ""))
