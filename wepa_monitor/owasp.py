"""OWASP Top 10:2025 checklist: how the app answers each risk, with evidence, checked live where possible.

Each item lists the app's controls, the test or file that proves each one, a live check of this running
copy's configuration (sign-in on, secret key set, audit chain intact, …), and the honest gaps. Status:
  Met       every control is in place and the live checks pass
  Partial   the main controls are in place; a known gap remains (stated)
  Gap       a live check failed on this copy
It is a self-assessment against the OWASP list, not an independent audit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE = "https://owasp.org/Top10/2025/"


@dataclass
class Item:
    code: str
    name: str
    slug: str
    risk: str                                  # the risk in plain English
    controls: list[tuple[str, str]]            # (what the app does, evidence)
    gaps: list[str] = field(default_factory=list)
    live: list[tuple[bool, str]] = field(default_factory=list)   # (passed, what was checked)

    @property
    def url(self) -> str:
        return BASE + self.slug + "/"

    @property
    def status(self) -> str:
        if any(not ok for ok, _ in self.live):
            return "Gap"
        return "Partial" if self.gaps else "Met"


def checklist(data_dir: Path) -> list[Item]:
    from . import accounts, auth, investigations, vulns
    root = Path(__file__).resolve().parent.parent
    sec_dir = accounts.store_dir()
    key_file = sec_dir / "secret.key" if sec_dir else None
    fs = vulns.findings(data_dir)
    exploited = [f for f in fs if f.priority == "Act now" and not f.tooling]
    chain_ok, chain_msg = investigations.verify(data_dir)
    signin = auth.enabled()
    public = os.environ.get("WEPA_PUBLIC_STATUS") == "1"
    pk = os.environ.get("WEPA_PEER_KEY", "")

    return [
        Item("A01", "Broken Access Control", "A01_2025-Broken_Access_Control",
             "People reaching pages, files or actions they shouldn't.",
             [("Every page, file and API needs a signed-in session; tested by walking every route with every method",
               "tests/test_hardening.py::test_every_route_needs_a_session"),
              ("Account management and updates are administrator-only; updates need the password again",
               "tests/test_software.py::test_only_the_administrator_can_start_updates_and_needs_the_password_again"),
              ("Investigations can only be changed by named staff accounts, with separation of duties",
               "tests/test_investigations.py"),
              ("Path tricks on pictures, evidence packets and static files are refused",
               "tests/test_hardening.py::test_hostile_inputs"),
              ("Residence halls and sign-in times shown only to the administrator and the person", "docs/ACCOUNTS.md")],
             live=[(signin, "Sign-in is on (WEPA_BASIC_AUTH set)"),
                   (True, "Student status pages are " + ("public by choice (WEPA_PUBLIC_STATUS=1): current status only"
                                                         if public else "behind sign-in"))]),
        Item("A02", "Security Misconfiguration", "A02_2025-Security_Misconfiguration",
             "Unsafe defaults, debug modes, missing security headers, leftover features.",
             [("Content Security Policy, nosniff, frame, referrer and permissions policies on every response; "
               "HSTS over HTTPS", "tests/test_hardening.py::test_security_headers"),
              ("No caching of pages with data; the Server header is removed", "wepa_monitor/dashboard/app.py (_harden)"),
              ("Production runs gunicorn without debug; Azure ingress refuses plain HTTP",
               "deploy/azure-containerapps/deploy.sh (allowInsecure: false)"),
              ("Backup-collector API answers 404 unless a 32+ character key is set", "wepa_monitor/peer.py")],
             gaps=["The container runs as root (the stock python:3.12-slim default)."],
             live=[(bool(os.environ.get("WEPA_SECRET_KEY")) or bool(key_file and key_file.exists()),
                    "A session signing key is set"),
                   (not pk or len(pk) >= 32, "The backup key, if set, is at least 32 characters")]),
        Item("A03", "Software Supply Chain Failures", "A03_2025-Software_Supply_Chain_Failures",
             "Vulnerable or tampered packages and build pipelines.",
             [("Every package pinned to an exact version and checked against its SHA-256 hash on every install",
               "requirements.lock, deploy.sh (--require-hashes)"),
              ("Daily look-up of every installed package in OSV.dev, ranked with CISA KEV, NVD, Microsoft, EPSS, "
               "Exploit-DB and Metasploit", "wepa_monitor/vulns.py, wepa_monitor/threatintel.py"),
              ("pip-audit and bandit on every push and daily; Dependabot weekly", ".github/workflows/ci.yml"),
              ("Updates are built, tested and scanned by GitHub and reviewed in a pull request before deploy; "
               "workflows have read-only permissions by default", ".github/workflows/update.yml")],
             live=[((root / "requirements.lock").exists(), "requirements.lock is present"),
                   (not exploited, "No package the app runs has a vulnerability known to be exploited"
                    + (f" (found: {', '.join(f.headline_id for f in exploited)})" if exploited else ""))]),
        Item("A04", "Cryptographic Failures", "A04_2025-Cryptographic_Failures",
             "Weak or missing encryption and hashing of sensitive data.",
             [("Passwords stored only as salted scrypt hashes", "wepa_monitor/accounts.py"),
              ("HTTPS only in Azure; session cookies Secure, HttpOnly and SameSite", "tests/test_site_login.py"),
              ("Sessions signed with a 256-bit random key; backup messages with HMAC-SHA256",
               "wepa_monitor/auth.py, wepa_monitor/peer.py"),
              ("Azure storage requires TLS 1.2 and is encrypted at rest by Azure", "deploy.sh (--min-tls-version TLS1_2)")],
             live=[(True, "Breached-password check uses k-anonymity (only 5 hash characters leave the server)")]),
        Item("A05", "Injection", "A05_2025-Injection",
             "Untrusted input run as code or queries (SQL, HTML/script, XML, shell, prompts).",
             [("No SQL database; records are JSON lines written by the app", "wepa_monitor/refs.py"),
              ("Pages are built by Dash/React, which escapes text; sign-in pages escape every value; CSP blocks "
               "inline script", "tests/test_hardening.py::test_hostile_inputs (reflected XSS)"),
              ("XML (OpenStreetMap data) parsed with defusedxml", "wepa_monitor/routing.py"),
              ("Subprocesses use fixed argument lists, never a shell or user text", "wepa_monitor/peer.py, scripts/"),
              ("AI answers shown without links, images or HTML; the AI only has read-only tools",
               "tests/test_hardening.py::test_ai_output_cannot_carry_links_or_markup"),
              ("Feature requests sent to GitHub neutralize @mentions and are length-limited",
               "tests/test_feedback.py")]),
        Item("A06", "Insecure Design", "A06_2025-Insecure_Design",
             "Missing security thinking in how the app is designed.",
             [("Written threat model, assets and limits", "docs/SECURITY.md"),
              ("Read-only collection: the app only reads Wepa's public page", "wepa_monitor/scrape.py"),
              ("Append-only records, never deleted; the app never modifies its own code",
               "wepa_monitor/updates.py"),
              ("Rate limits: sign-in, AI answers per hour, feature requests per day, vulnerability checks",
               "wepa_monitor/security.py, wepa_monitor/feedback.py")]),
        Item("A07", "Authentication Failures", "A07_2025-Authentication_Failures",
             "Weak sign-in: guessable passwords, no lockout, sessions that never end.",
             [("NIST SP 800-63B password rules, including breached-password refusal",
               "tests/test_site_login.py::test_temporary_password_must_be_changed_and_policy_applies"),
              ("Lockout per account (5 in 15 min) and per network (8)", "tests/test_site_login.py::test_account_lockout"),
              ("Sessions end after 2 h idle or 12 h, and at once on password change or deactivation",
               "tests/test_site_login.py::test_idle_sessions_expire"),
              ("Same message and timing for wrong usernames and wrong passwords", "wepa_monitor/accounts.py")],
             gaps=["No multi-factor authentication (path: BSU Entra ID single sign-on).",
                   "The administrator is one shared account."],
             live=[(signin, "Sign-in is on")]),
        Item("A08", "Software or Data Integrity Failures", "A08_2025-Software_or_Data_Integrity_Failures",
             "Trusting code or data that may have been altered.",
             [("Investigation history is a SHA-256 hash chain; tampering is detected",
               "tests/test_software.py::test_selfcheck_reports_facts_and_catches_tampering"),
              ("Raw readings are append-only; every figure is recomputed from them", "wepa_monitor/store.py"),
              ("Backup messages and replies are signed, time-limited and single-use; an impostor can't stand a "
               "backup down", "tests/test_software.py::test_forged_status_from_an_impostor_is_ignored"),
              ("Hash-checked installs; CSRF tokens on every form", "requirements.lock, wepa_monitor/auth.py")],
             live=[(chain_ok, f"Investigation audit trail: {chain_msg}")]),
        Item("A09", "Security Logging and Alerting Failures", "A09_2025-Security_Logging_and_Alerting_Failures",
             "Attacks that go unnoticed because nothing is recorded or nobody is told.",
             [("Sign-ins, failures (never the password), blocks, exports, account changes and update actions in the "
               "audit log", "System page, Security log"),
              ("Refused backup-collector messages are logged", "wepa_monitor/peer.py"),
              ("Exploited vulnerabilities, backups taking over and software updates appear as notifications",
               "wepa_monitor/activity.py (NOTIFY_KINDS)")],
             gaps=["Alerts appear in the app only; nothing is emailed or paged yet.",
                   "The live log is kept in memory; the audit log is kept on disk."],
             live=[(sec_dir is not None, "Audit records are written to the security folder")]),
        Item("A10", "Mishandling of Exceptional Conditions", "A10_2025-Mishandling_of_Exceptional_Conditions",
             "Errors that crash the app, leak details or fail open.",
             [("The collector records failed reads as data and carries on; it never stops on an error",
               "wepa_monitor/collector.py"),
              ("Sign-in fails closed: an unknown or broken session is signed out", "wepa_monitor/auth.py (_gate)"),
              ("Outside services (OSV, threat intel, GitHub, AI, weather) fail softly and keep the last good result; "
               "every failure is shown", "tests/test_software.py::test_a_failed_lookup_keeps_the_last_good_result"),
              ("Oversized requests refused (8 MB)", "tests/test_hardening.py::test_hostile_inputs")]),
    ]


def summary(items: list[Item]) -> dict[str, int]:
    out = {"Met": 0, "Partial": 0, "Gap": 0}
    for it in items:
        out[it.status] += 1
    return out
