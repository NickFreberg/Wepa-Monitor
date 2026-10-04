"""Build a software update package: upgrade packages, re-pin the lock file, version and change log.

    python scripts/prepare_update.py jinja2==3.1.6 requests==2.32.4 [--ticket UPD000000001] [--by name]

Run by .github/workflows/update.yml (started from the app's Software page), or by hand. It:
  1. raises each package's minimum version in requirements.txt (adding indirect ones with a comment);
  2. regenerates requirements.lock with uv (exact versions and hashes), upgrading only those packages;
  3. looks up in OSV.dev which vulnerabilities the upgrade fixes;
  4. bumps the patch version and adds a plain-English change-log entry with links to every CVE/GHSA;
  5. writes the pull request description to update-pr.md.
It never installs or deploys anything; tests, scans and review happen next.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess  # nosec B404 - fixed command, validated package names
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wepa_monitor import vulns  # noqa: E402

SPEC = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)$")
LOCK_CMD = ["uv", "pip", "compile", "requirements.txt", "--python-version", "3.12", "--python-platform", "linux",
            "--generate-hashes", "-o", "requirements.lock"]


def locked_versions(text: str) -> dict[str, str]:
    return {vulns.canonical(m[1]): m[2] for m in re.finditer(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", text, re.M)}


def raise_minimum(req_text: str, name: str, version: str, why: str) -> str:
    """Set name's lower bound to version, keeping any upper bound; add the line if name isn't listed."""
    lines = req_text.splitlines()
    for i, line in enumerate(lines):
        body = line.split("#", 1)[0].strip()
        listed = re.split(r"[<>=!~;\[ ]", body, maxsplit=1)[0] if body else ""
        if listed and vulns.canonical(listed) == name:
            bounds = [b.strip() for b in body[len(listed):].split(",")
                      if b.strip() and not b.strip().startswith((">=", ">", "==", "~="))]
            lines[i] = listed + ",".join([f">={version}", *bounds])
            return "\n".join(lines) + "\n"
    return req_text.rstrip("\n") + f"\n{name}>={version}  # security: {why}\n"


def bump_patch(init_text: str) -> tuple[str, str, str]:
    m = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', init_text)
    old = f"{m[1]}.{m[2]}.{m[3]}"
    new = f"{m[1]}.{m[2]}.{int(m[3]) + 1}"
    return init_text.replace(f'__version__ = "{old}"', f'__version__ = "{new}"'), old, new


def entry(version: str, date: str, changes: list[dict], ticket: str, by: str) -> str:
    lines = [f"## [{version}] - {date}",
             "Security update: " + ", ".join(f"{c['package']} {c['old']} → {c['new']}" for c in changes) + ".", "",
             "### Security"]
    for c in changes:
        if not c["fixes"]:
            lines.append(f"- Updated {c['package']} from {c['old']} to {c['new']}.")
        for f in c["fixes"]:
            refs = []
            for i in f.ids:
                if i.startswith("CVE-"):
                    refs.append(f"[{i}](https://nvd.nist.gov/vuln/detail/{i})")
                elif i.startswith("GHSA-"):
                    refs.append(f"[{i}](https://github.com/advisories/{i})")
            refs.append(f"[OSV](https://osv.dev/vulnerability/{f.ids[0]})")
            lines.append(f"- Updated {c['package']} from {c['old']} to {c['new']} to fix a {f.severity.lower()}-"
                         f"severity issue: {f.summary} ({', '.join(refs)})")
    lines += ["", "### Sources", f"- Update package {ticket or '(prepared by hand)'}"
              + (f", requested by {by}" if by else ""), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("packages", nargs="+", help="name==version")
    ap.add_argument("--ticket", default="")
    ap.add_argument("--by", default="")
    ap.add_argument("--no-lock", action="store_true", help="skip uv (tests)")
    ap.add_argument("--root", type=Path, default=ROOT)
    a = ap.parse_args(argv)
    if a.ticket and not re.fullmatch(r"UPD\d{9}", a.ticket):
        sys.exit("ticket must look like UPD000000001")
    wanted = []
    for s in a.packages:
        m = SPEC.match(s)
        if not m:
            sys.exit(f"not name==version: {s!r}")
        wanted.append((vulns.canonical(m[1]), m[2]))

    req, lock, init, log = (a.root / "requirements.txt", a.root / "requirements.lock",
                            a.root / "wepa_monitor" / "__init__.py", a.root / "CHANGELOG.md")
    old_locked = locked_versions(lock.read_text()) if lock.exists() else {}
    for name, ver in wanted:
        if name in old_locked and vulns._version_key(ver) < vulns._version_key(old_locked[name]):
            sys.exit(f"{name} {ver} would be a downgrade from {old_locked[name]}; refusing")
    try:
        found = vulns.scan({n: old_locked.get(n, "0") for n, _ in wanted})
    except Exception as exc:  # noqa: BLE001 - the change log then lists upgrades without CVE detail
        print(f"OSV lookup failed ({exc}); continuing without vulnerability details", file=sys.stderr)
        found = []

    text = req.read_text()
    changes = []
    for name, ver in wanted:
        fixes = [f for f in found if f.package == name and f.fixed
                 and vulns._version_key(f.fixed) <= vulns._version_key(ver)]
        why = ", ".join(f.ids[0] for f in fixes) or "update package"
        text = raise_minimum(text, name, ver, why)
        changes.append({"package": name, "old": old_locked.get(name, "not pinned"), "new": ver, "fixes": fixes})
    req.write_text(text)

    if not a.no_lock:
        cmd = LOCK_CMD + [x for n, _ in wanted for x in ("--upgrade-package", n)]
        subprocess.run(cmd, cwd=a.root, check=True)  # nosec B603 - fixed command, validated names
        new_locked = locked_versions(lock.read_text())
        for c in changes:
            c["new"] = new_locked.get(c["package"], c["new"])

    init_text, old_v, new_v = bump_patch(init.read_text())
    init.write_text(init_text)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    body = entry(new_v, today, changes, a.ticket, a.by)
    cl = log.read_text()
    at = cl.find("\n## [")
    log.write_text(cl[:at + 1] + body + "\n" + cl[at + 1:] if at >= 0 else cl.rstrip() + "\n\n" + body)

    pr = [f"Security update {old_v} → {new_v}" + (f" ({a.ticket})" if a.ticket else ""), "",
          "Prepared automatically from the app's Software page. Every test and security scan ran on this branch.",
          "", body]
    (a.root / "update-pr.md").write_text("\n".join(pr))
    print(f"version {old_v} -> {new_v}")
    for c in changes:
        print(f"  {c['package']} {c['old']} -> {c['new']}: fixes {', '.join(f.ids[0] for f in c['fixes']) or 'nothing known'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
