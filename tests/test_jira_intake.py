"""Jira intake: JQL search → normalize → dedupe, plus opt-in write-back.

Mirrors the (removed) TRACKER poller's shape. All HTTP is mocked; the API token is
read from the env and must never appear in a log line.
"""

import logging

import pytest

from no_human.core.db import Store
from no_human.intake.jira import JiraAdapter, _adf_text
from no_human.intake.jira_poll import JiraPoller


def _cfg(**over):
    j = {"enabled": True, "site": "https://acme.atlassian.net",
         "project_key": "PROJ", "email": "me@x.com", "jql": ""}
    j.update(over)
    return {"integrations": {"jira": j}}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_adapter_configured(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    assert JiraAdapter(_cfg()).configured is True
    assert JiraAdapter(_cfg(site="")).configured is False
    monkeypatch.delenv("JIRA_API_TOKEN")
    assert JiraAdapter(_cfg()).configured is False   # no token → not configured


def test_search_uses_successor_endpoint_and_basic_auth(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params, auth=auth)
        return _Resp({"issues": [{"key": "PROJ-1", "fields": {"summary": "Do X"}}]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    issues = JiraAdapter(_cfg()).search()
    assert issues[0]["key"] == "PROJ-1"
    # r1: the deprecated /rest/api/3/search is replaced by /rest/api/3/search/jql
    assert "/rest/api/3/search/jql" in captured["url"]
    assert captured["auth"] == ("me@x.com", "SEKRET")     # Basic email:token


def test_jql_is_operator_authored_never_task_text(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    a = JiraAdapter(_cfg(jql="assignee = currentUser() AND statusCategory != Done"))
    assert a._search_jql() == "assignee = currentUser() AND statusCategory != Done"
    # default derives from the project key only — never from any task text
    assert 'project = "PROJ"' in JiraAdapter(_cfg(jql=""))._search_jql()


def test_normalize_maps_fields_and_criteria(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    issue = {"key": "PROJ-7", "fields": {
        "summary": "Add retry",
        "description": "Body\n- [ ] retries 3x\n- [x] logs\n",
        "status": {"name": "To Do"}, "labels": ["backend"],
        "issuetype": {"name": "Story"}}}
    task = JiraAdapter(_cfg()).normalize(issue)
    assert task.source == "jira"
    assert task.external_id == "PROJ-7"
    assert task.title == "Add retry"
    assert task.acceptance_criteria == ["retries 3x", "logs"]
    assert task.context["jira"]["url"].endswith("/browse/PROJ-7")
    assert task.context["jira"]["status"] == "To Do"
    assert task.context["jira"]["labels"] == ["backend"]


def test_normalize_handles_adf_description(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]}]}
    task = JiraAdapter(_cfg()).normalize({"key": "PROJ-8", "fields": {"summary": "S", "description": adf}})
    assert "hello world" in task.description
    assert _adf_text("plain string") == "plain string"


def test_issue_brief_truncates_but_issue_detail_carries_full_description(monkeypatch):
    """SCRUM-9: issue_brief (browse list) truncates to 2000 chars — that's
    correct for a small list payload. issue_detail (single picked issue) must
    carry the FULL text so the created task doesn't lose the tail."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    long_desc = "x" * 2000 + "TAIL-AFTER-TRUNCATION-POINT"
    issue = {"key": "PROJ-9", "fields": {"summary": "Fix", "description": long_desc}}
    a = JiraAdapter(_cfg())
    brief = a.issue_brief(issue)
    assert len(brief["description"]) == 2000
    assert "TAIL-AFTER-TRUNCATION-POINT" not in brief["description"]

    detail = a.issue_detail(issue)
    assert detail["description"] == long_desc
    assert detail["description"].endswith("TAIL-AFTER-TRUNCATION-POINT")
    # Same shape otherwise.
    assert detail["key"] == brief["key"] == "PROJ-9"
    assert detail["summary"] == brief["summary"]


def test_get_issue_fetches_by_key_with_basic_auth(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params, auth=auth)
        return _Resp({"key": "PROJ-9", "fields": {"summary": "Fix the thing"}})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    issue = JiraAdapter(_cfg()).get_issue("PROJ-9")
    assert issue["key"] == "PROJ-9"
    assert "/rest/api/3/issue/PROJ-9" in captured["url"]
    assert captured["auth"] == ("me@x.com", "SEKRET")


def test_auth_token_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("JIRA_API_TOKEN", "SUPERSECRET")
    monkeypatch.setattr("no_human.intake.jira.httpx.get",
                        lambda *a, **k: _Resp({"issues": []}))
    with caplog.at_level(logging.DEBUG):
        JiraAdapter(_cfg()).search()
    assert "SUPERSECRET" not in caplog.text


def test_comment_write_back_is_opt_in(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    posted = {}

    def fake_post(url, auth=None, json=None, timeout=None, headers=None):
        posted.update(url=url)
        return _Resp({})

    monkeypatch.setattr("no_human.intake.jira.httpx.post", fake_post)
    assert JiraAdapter(_cfg(write_back=False)).comment("PROJ-1", "hi") is False
    assert posted == {}                                    # never posts when off
    assert JiraAdapter(_cfg(write_back=True)).comment("PROJ-1", "hi") is True
    assert "/issue/PROJ-1/comment" in posted["url"]


@pytest.mark.asyncio
async def test_poller_creates_then_dedupes(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        issues = [{"key": "PROJ-1", "fields": {"summary": "A"}},
                  {"key": "PROJ-2", "fields": {"summary": "B"}}]
        a = JiraAdapter(_cfg())
        monkeypatch.setattr(a, "search", lambda: issues)
        poller = JiraPoller(a, store, config=_cfg(default_repo="/tmp/repo"))
        r1 = await poller.poll_once()
        assert (r1.created, r1.seen) == (2, 2)
        r2 = await poller.poll_once()          # same issues → all deduped
        assert (r2.created, r2.skipped) == (0, 2)
        tasks = await store.list_tasks()
        assert {t.external_id for t in tasks} == {"PROJ-1", "PROJ-2"}
        assert all(t.repo_path == "/tmp/repo" for t in tasks)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_survives_a_search_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        a = JiraAdapter(_cfg())

        def boom():
            raise RuntimeError("429 rate limited")

        monkeypatch.setattr(a, "search", boom)
        r = await JiraPoller(a, store, config=_cfg()).poll_once()
        assert r.created == 0                   # error logged, not raised
    finally:
        await store.close()
