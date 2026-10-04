"""Feature requests and problem reports from the app's users, filed as GitHub issues.

Anyone signed in can send one from "Suggest a feature". It gets a permanent REQ number, is kept in
records/feedback.jsonl, and is opened as an issue in the app's GitHub repository with labels. If GitHub
can't be reached (or no token is set) it waits as "Queued" and is sent later (on the next request, or by
the collector once a day). People can see their own requests and whether the issue is open or closed.

Privacy: the issue carries the request, its REQ number, the page it came from, the app version and the
person's role. Names are added only with WEPA_FEEDBACK_NAMES=1, because issues are visible to anyone who
can see the repository. @mentions are neutralized so a request can't ping people on GitHub.

Environment:
    WEPA_GITHUB_ISSUES_TOKEN   fine-grained token for this repository only, "Issues: read and write"
                               (falls back to WEPA_GITHUB_TOKEN if that one also has Issues permission)
    WEPA_GITHUB_REPO           owner/name (default nickfreberg/wepa-monitor)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .refs import _FileLock, records_dir

KINDS = {"feature": ("Feature request", "enhancement"), "problem": ("Problem report", "bug"),
         "other": ("Other feedback", "question")}
TITLE_MIN, TITLE_MAX = 5, 120
BODY_MIN, BODY_MAX = 10, 4000
PER_DAY = 5
STATUS_TTL = 900
_status_cache: dict[str, tuple[float, dict]] = {}


class FeedbackError(ValueError):
    pass


def token() -> str:
    return os.environ.get("WEPA_GITHUB_ISSUES_TOKEN") or os.environ.get("WEPA_GITHUB_TOKEN", "")


def repo() -> str:
    return os.environ.get("WEPA_GITHUB_REPO", "nickfreberg/wepa-monitor")


def _path(data_dir: Path) -> Path:
    return records_dir(data_dir) / "feedback.jsonl"


def all_requests(data_dir: Path) -> list[dict]:
    """Every request, latest state of each, newest first."""
    p = _path(data_dir)
    by_ref: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by_ref[r["ref"]] = {**by_ref.get(r["ref"], {}), **r}
    return sorted(by_ref.values(), key=lambda r: r.get("created", 0), reverse=True)


def mine(data_dir: Path, username: str) -> list[dict]:
    return [r for r in all_requests(data_dir) if r.get("username") == username]


def _append(data_dir: Path, row: dict) -> None:
    p = _path(data_dir)
    with _FileLock(p.with_suffix(".lock")):
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row) + "\n")


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text or "").replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]


def _neutralize(text: str) -> str:
    """No @mentions (no pings), no HTML comments or tags hiding text from readers."""
    text = re.sub(r"@(?=[A-Za-z0-9-])", "@​", text)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def issue_body(row: dict) -> str:
    from . import __version__
    who = row.get("name") if os.environ.get("WEPA_FEEDBACK_NAMES") == "1" and row.get("name") else \
        f"a {row.get('role', 'staff')} user"
    quoted = "\n".join("> " + line if line else ">" for line in _neutralize(row["body"]).split("\n"))
    return (f"{quoted}\n\n---\n"
            f"**{KINDS[row['kind']][0]}** {row['ref']} · sent from ResNet Print Ops {__version__}"
            f"{' · page ' + _neutralize(row['page']) if row.get('page') else ''} · by {_neutralize(who)}\n\n"
            "_Filed automatically from the app's “Suggest a feature” form._")


def submit(data_dir: Path, user: dict, kind: str, title: str, body: str, page: str = "", session=None) -> dict:
    from . import refs, security, sysevents
    if kind not in KINDS:
        raise FeedbackError("Choose what kind of feedback this is.")
    title, body = _clean(title, TITLE_MAX + 1), _clean(body, BODY_MAX + 1)
    if len(title) < TITLE_MIN:
        raise FeedbackError(f"Give it a short title ({TITLE_MIN} characters or more).")
    if len(title) > TITLE_MAX:
        raise FeedbackError(f"Keep the title under {TITLE_MAX} characters.")
    if len(body) < BODY_MIN:
        raise FeedbackError("Say a little more about what you'd like and why.")
    if len(body) > BODY_MAX:
        raise FeedbackError(f"Keep it under {BODY_MAX:,} characters.")
    username = user.get("username") or "admin"
    day_ago = time.time() - 86400
    if sum(1 for r in mine(data_dir, username) if r.get("created", 0) > day_ago) >= PER_DAY:
        raise FeedbackError(f"You've sent {PER_DAY} requests today; please wait until tomorrow.")
    page = page if re.fullmatch(r"/[A-Za-z0-9/_-]{0,80}", page or "") else ""
    ref = refs.next_number(data_dir, "REQ", {"kind": "feedback"})
    row = {"ref": ref, "created": time.time(), "username": username, "name": user.get("name", username),
           "role": user.get("role", "staff"), "kind": kind, "title": title, "body": body, "page": page,
           "state": "Queued"}
    _append(data_dir, row)
    security.audit("feature request", f"{ref}: {title}")
    sent = push(data_dir, row, session=session)
    sysevents.add("feedback", f"{ref}: {KINDS[kind][0].lower()} — {title}",
                  f"Opened as GitHub issue #{sent['issue']}." if sent.get("issue") else
                  "Saved; it will be sent to GitHub when it can be.",
                  href=sent.get("url", ""), data_dir=data_dir)
    return {**row, **sent}


def push(data_dir: Path, row: dict, session=None) -> dict:
    """Open the GitHub issue. Returns the fields that changed (state, issue, url, or error)."""
    if not token():
        return {}
    import requests
    http = session or requests
    label, gh_label = KINDS[row["kind"]]
    try:
        r = http.post(f"https://api.github.com/repos/{repo()}/issues",
                      headers={"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"},
                      json={"title": f"[{label}] {row['title']}", "body": issue_body(row),
                            "labels": [gh_label, "from the app"]}, timeout=20)
    except Exception as exc:  # noqa: BLE001 - stays queued
        upd = {"ref": row["ref"], "error": f"{type(exc).__name__}"}
        _append(data_dir, upd)
        return upd
    if r.status_code != 201:
        upd = {"ref": row["ref"], "error": f"GitHub answered {r.status_code}"}
        _append(data_dir, upd)
        return upd
    d = r.json()
    upd = {"ref": row["ref"], "state": "Sent", "issue": d["number"], "url": d["html_url"], "sent": time.time(),
           "error": ""}
    _append(data_dir, upd)
    return upd


def send_queued(data_dir: Path, session=None, log=print) -> int:
    n = 0
    for r in all_requests(data_dir):
        if r.get("state") == "Queued" and push(data_dir, r, session=session).get("state") == "Sent":
            n += 1
    if n:
        log(f"sent {n} queued feature request(s) to GitHub")
    return n


def issue_status(row: dict, session=None) -> dict:
    """Open/closed and when, for a sent request (cached 15 minutes; empty when unknown)."""
    if not row.get("issue") or not token():
        return {}
    hit = _status_cache.get(row["ref"])
    if hit and time.time() - hit[0] < STATUS_TTL:
        return hit[1]
    import requests
    http = session or requests
    try:
        r = http.get(f"https://api.github.com/repos/{repo()}/issues/{int(row['issue'])}",
                     headers={"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json"},
                     timeout=10)
        d = r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        d = {}
    st = {"state": d.get("state", ""), "reason": d.get("state_reason") or "", "comments": d.get("comments", 0)} if d else {}
    _status_cache[row["ref"]] = (time.time(), st)
    return st
