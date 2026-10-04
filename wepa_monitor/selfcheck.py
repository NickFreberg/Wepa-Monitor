"""The app's check of itself: is every part working, current and configured safely?

Each check reports a status (ok, warn, fail, info), one plain sentence of fact, and what to do when it
isn't ok. The Software page lists them; the assistant's self_check tool reads the same list, so when it
speaks about the app it reports these facts rather than guessing.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__

ORDER = {"fail": 0, "warn": 1, "info": 2, "ok": 3}


@dataclass
class Check:
    area: str            # Availability, Integrity, Confidentiality, Currency
    name: str
    status: str
    detail: str
    fix: str = ""


def _ago(s: float) -> str:
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.1f} h"


def run(ds=None, data_dir: Path | None = None) -> list[Check]:
    data_dir = Path(data_dir or ds.data_dir)
    out: list[Check] = []
    add = lambda *a: out.append(Check(*a))  # noqa: E731
    demo = bool(ds is not None and ds.is_demo)

    # --- availability -----------------------------------------------------------------------------
    from . import peer
    last = peer.own_last_ok(data_dir)
    collecting = os.environ.get("WEPA_COLLECT", "1") == "1"
    if demo:
        add("Availability", "Collector", "info", "This copy shows demo data; nothing is being collected.")
    elif last is None:
        add("Availability", "Collector", "warn" if collecting else "info",
            "No reading recorded by this copy's own collector yet." if collecting else
            "This copy only serves the dashboard (WEPA_COLLECT=0).",
            "Start the app with the collector (python -m wepa_monitor start)." if collecting else "")
    else:
        age = time.time() - last
        add("Availability", "Collector", "ok" if age <= peer.LIVE_S else "fail",
            f"Last good reading of the Wepa page {_ago(age)} ago.",
            "" if age <= peer.LIVE_S else "Check the System page's live log; a backup collector covers gaps if one "
                                          "is set up.")
    peers = peer.peers(data_dir)
    if peer.key() is None:
        add("Availability", "Backup collectors", "info", "No backup collector is set up (WEPA_PEER_KEY isn't set).",
            "See docs/SECURITY.md, 'Backup collectors', to add one.")
    elif not peers:
        add("Availability", "Backup collectors", "warn", "The backup key is set but no backup has checked in.",
            "Start one: python -m wepa_monitor backup --primary <this app's address>.")
    else:
        lines, worst = [], "ok"
        for bid, p in sorted(peers.items()):
            age = time.time() - p.get("last_seen", 0)
            st = p.get("state", "standby")
            lines.append(f"'{bid}' {st}, last check-in {_ago(age)} ago (version {p.get('version') or '?'})")
            if age > 120:
                worst = "warn"
            elif st == "active" and worst == "ok":
                worst = "warn"
        add("Availability", "Backup collectors", worst, "; ".join(lines) + ".",
            "" if worst == "ok" else "A backup that hasn't checked in for 2 minutes may be off or offline.")
    try:
        du = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent)
        free_gb = du.free / 1e9
        add("Availability", "Disk space", "ok" if free_gb > 2 else "warn" if free_gb > 0.5 else "fail",
            f"{free_gb:,.1f} GB free where the data is kept.",
            "" if free_gb > 2 else "Raise the file share quota or archive old raw files.")
    except OSError:
        pass

    # --- integrity --------------------------------------------------------------------------------
    if ds is not None and not ds.empty:
        from . import metrics as M
        dq = M.data_quality(ds, *M.window(ds, 7))
        if dq.value is not None:
            add("Integrity", "Data quality (7 days)", "ok" if dq.value >= 95 else "warn" if dq.value >= 80 else "fail",
                f"Score {dq.value:.0f}/100: {dq.extra.get('completeness', 0):.1f}% of minutes captured.",
                "" if dq.value >= 95 else "Gaps are left out of every figure, never guessed; see the System page.")
    from . import investigations
    ok, msg = investigations.verify(data_dir)
    add("Integrity", "Investigation audit trail", "ok" if ok else "fail", msg,
        "" if ok else "Someone edited the records file by hand. Restore it from a backup of the data share.")
    lock = Path(__file__).resolve().parent.parent / "requirements.lock"
    add("Integrity", "Pinned dependencies", "ok" if lock.exists() else "warn",
        "Every package is installed at an exact version, checked against its published hash." if lock.exists()
        else "requirements.lock is missing; installs aren't hash-checked.",
        "" if lock.exists() else "Regenerate it (see the header of requirements.lock).")

    # --- confidentiality --------------------------------------------------------------------------
    from . import auth, security
    if auth.enabled():
        add("Confidentiality", "Sign-in", "ok", "Every page needs a signed-in account; sessions expire after "
            f"{auth.IDLE_S // 3600} h idle or {auth.ABSOLUTE_S // 3600} h in total.")
    else:
        add("Confidentiality", "Sign-in", "fail" if not demo else "info",
            "Sign-in is off: anyone with the address can see everything.",
            "Set WEPA_BASIC_AUTH (deploy.sh does this).")
    if os.environ.get("WEPA_PUBLIC_STATUS") == "1":
        add("Confidentiality", "Public status pages", "info",
            "Student status pages are public (WEPA_PUBLIC_STATUS=1). They show only current status, no history.")
    try:
        locks = security.locked_networks()
        fails = security.events_frame(("login_failed",))
        recent = fails[fails["ts"] >= fails["ts"].max() - __import__("pandas").Timedelta(days=1)] if len(fails) else fails
        add("Confidentiality", "Sign-in attempts", "warn" if locks or len(recent) >= 20 else "ok",
            f"{len(recent)} failed sign-ins in the last day" + (f"; {len(locks)} network(s) blocked now" if locks else "")
            + ".", "Look at the Security log on the System page." if locks or len(recent) >= 20 else "")
    except Exception:  # noqa: BLE001 - the security log is optional
        pass
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        add("Confidentiality", "Process privileges", "info",
            "The app runs as root inside its container (the stock Python image's default).",
            "Running as an unprivileged user would limit the damage of any future flaw; see docs/SECURITY.md.")

    # --- currency ---------------------------------------------------------------------------------
    from . import updates, vulns
    info = updates.build_info()
    rel = updates.release(__version__)
    add("Currency", "Version", "ok" if rel else "warn",
        f"Running version {__version__}" + (f" (commit {info['commit']})" if info.get("commit") else "")
        + (f", released {rel.date}." if rel else "; it has no change-log entry."),
        "" if rel else "Add the release to CHANGELOG.md.")
    c = vulns.load(data_dir)
    fs = vulns.findings(data_dir)
    runtime = [f for f in fs if not f.tooling]
    if not vulns.enabled():
        add("Currency", "Known vulnerabilities", "info", "The daily vulnerability check is turned off (WEPA_VULN_CHECK=0).")
    elif not c.get("checked_at"):
        add("Currency", "Known vulnerabilities", "info", "Not checked yet; the collector checks once a day.",
            "Use 'Check now' on the Software page.")
    else:
        serious = [f for f in runtime if f.severity in ("Critical", "High")]
        status = "fail" if serious else "warn" if runtime else "info" if fs else "ok"
        add("Currency", "Known vulnerabilities", status, vulns.sentence(data_dir),
            "Prepare an update package on the Software page." if any(f.fixed for f in fs) else
            ("No fixed version exists yet; the check repeats daily." if fs else ""))
        age = time.time() - c["checked_at"]
        if age > 2 * vulns.MAX_AGE_S:
            add("Currency", "Vulnerability check", "warn", f"The last successful check was {_ago(age)} ago.",
                c.get("error", "The collector runs it daily; check the live log."))
    pending = [p for p in updates.packages(data_dir) if p.get("state") in ("Prepared", "Requested")]
    if pending:
        p = pending[0]
        add("Currency", "Update package", "info", f"{p['ref']} is {p['state'].lower()}: {p['spec']}.")
    return sorted(out, key=lambda x: (ORDER[x.status], x.area))


def summary(checks: list[Check]) -> tuple[str, str]:
    """(tone, sentence) for a headline."""
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    if fails:
        return "critical", f"{len(fails)} problem{'s' if len(fails) != 1 else ''} need attention: " + \
            "; ".join(c.name.lower() for c in fails) + "."
    if warns:
        return "warning", f"Working, with {len(warns)} thing{'s' if len(warns) != 1 else ''} to look at: " + \
            "; ".join(c.name.lower() for c in warns) + "."
    return "good", f"Every check passed ({len(checks)} checked)."


def as_records(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
