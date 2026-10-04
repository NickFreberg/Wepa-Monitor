"""Feature requests filed as GitHub issues."""
from __future__ import annotations

import pytest

from wepa_monitor import feedback, sysevents

USER = {"username": "jsmith", "name": "Jordan Smith", "role": "staff"}


class GH:
    def __init__(self, status=201):
        self.status, self.posts = status, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json})

        class R:
            status_code = self.status

            @staticmethod
            def json():
                return {"number": 42, "html_url": "https://github.com/o/r/issues/42"}
        return R()


def test_request_becomes_an_issue_without_names_or_pings(tmp_path, monkeypatch):
    monkeypatch.setenv("WEPA_GITHUB_ISSUES_TOKEN", "tok")
    monkeypatch.delenv("WEPA_FEEDBACK_NAMES", raising=False)
    gh = GH()
    r = feedback.submit(tmp_path, USER, "feature", "Toner on status pages",
                        "Please show toner.\n@octocat look <script>x</script>", page="/stations", session=gh)
    assert r["ref"] == "REQ000000001" and r["issue"] == 42 and r["state"] == "Sent"
    sent = gh.posts[0]
    assert sent["url"].endswith("/repos/nickfreberg/wepa-monitor/issues")
    assert sent["json"]["title"] == "[Feature request] Toner on status pages"
    assert sent["json"]["labels"] == ["enhancement", "from the app"]
    body = sent["json"]["body"]
    assert "@​octo" in body and "@octocat" not in body                 # no GitHub ping
    assert "<script>" not in body and "Jordan" not in body and "a staff user" in body
    assert "REQ000000001" in body and "page /stations" in body
    assert feedback.mine(tmp_path, "jsmith")[0]["url"] == "https://github.com/o/r/issues/42"
    assert sysevents.load(tmp_path)[-1]["kind"] == "feedback"


def test_names_only_when_turned_on(tmp_path, monkeypatch):
    monkeypatch.setenv("WEPA_FEEDBACK_NAMES", "1")
    assert "by Jordan Smith" in feedback.issue_body({**USER, "ref": "REQ1", "kind": "problem", "body": "x"})


def test_queued_without_github_then_sent_later(tmp_path, monkeypatch):
    monkeypatch.delenv("WEPA_GITHUB_ISSUES_TOKEN", raising=False)
    monkeypatch.delenv("WEPA_GITHUB_TOKEN", raising=False)
    r = feedback.submit(tmp_path, USER, "problem", "Map is blank", "The map shows nothing on Safari.")
    assert r["state"] == "Queued" and not r.get("issue")
    monkeypatch.setenv("WEPA_GITHUB_ISSUES_TOKEN", "tok")
    assert feedback.send_queued(tmp_path, session=GH(), log=lambda m: None) == 1
    assert feedback.all_requests(tmp_path)[0]["state"] == "Sent"
    assert feedback.send_queued(tmp_path, session=GH(), log=lambda m: None) == 0   # never twice


def test_github_refusal_keeps_it_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("WEPA_GITHUB_ISSUES_TOKEN", "tok")
    r = feedback.submit(tmp_path, USER, "feature", "Dark map", "A dark basemap please.", session=GH(status=403))
    assert r["state"] == "Queued" and "403" in r["error"]


def test_validation_and_daily_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("WEPA_GITHUB_ISSUES_TOKEN", raising=False)
    monkeypatch.delenv("WEPA_GITHUB_TOKEN", raising=False)
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(tmp_path, USER, "feature", "Hi", "Long enough body text.")
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(tmp_path, USER, "feature", "A fine title", "short")
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(tmp_path, USER, "hack", "A fine title", "Long enough body text.")
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(tmp_path, USER, "feature", "A fine title", "x" * (feedback.BODY_MAX + 5))
    for i in range(feedback.PER_DAY):
        feedback.submit(tmp_path, USER, "feature", f"Idea number {i}", "Long enough body text.")
    with pytest.raises(feedback.FeedbackError, match="today"):
        feedback.submit(tmp_path, USER, "feature", "One more idea", "Long enough body text.")
    r = feedback.submit(tmp_path, {**USER, "username": "alee"}, "other", "Different person", "Long enough body.",
                        page="javascript:alert(1)")
    assert r["page"] == ""                                                  # only plain app paths kept
