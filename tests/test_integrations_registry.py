"""Integrations registry: status list + health checks over the config.

The registry is a STATUS layer — jira/circleci are first-class config sections;
github/gitlab/jenkins are read-only views over ``ci.*`` and slack over
``notifications.*``. Secrets are never surfaced in a status detail.
"""

import pytest

from no_human import integrations as reg
from no_human.integrations import IntegrationStatus, list_integrations
# NB: the product fn is `integrations.test_integration`; referenced as
# `reg.test_integration` so pytest doesn't collect it as a test case.


def test_list_all_six_unconfigured():
    st = list_integrations({"integrations": {}, "ci": {}, "notifications": {}})
    assert [s.name for s in st] == ["jira", "github", "gitlab", "jenkins", "circleci", "slack"]
    assert all(isinstance(s, IntegrationStatus) for s in st)
    assert all(s.configured is False for s in st)
    assert all(s.healthy is None for s in st)          # None until test_integration runs
    kinds = {s.name: s.kind for s in st}
    assert kinds == {
        "jira": "issue_tracker", "github": "vcs", "gitlab": "vcs",
        "jenkins": "ci", "circleci": "ci", "slack": "notifications",
    }


def test_configured_detection():
    cfg = {
        "integrations": {
            "jira": {"site": "acme.atlassian.net", "project_key": "PROJ", "email": "me@x.com"},
            "circleci": {"org_slug": "gh/acme", "project": "svc"},
        },
        "ci": {"enabled": True, "backend": "github_actions", "project": "o/r", "job": ""},
        "notifications": {"slack_webhook_url": "https://hooks.slack.com/x"},
    }
    st = {s.name: s for s in list_integrations(cfg)}
    assert st["jira"].configured is True
    assert st["circleci"].configured is True
    assert st["github"].configured is True      # ci.backend == github_actions + project
    assert st["gitlab"].configured is False     # backend isn't gitlab
    assert st["jenkins"].configured is False    # backend isn't jenkins
    assert st["slack"].configured is True


def test_null_sections_are_safe():
    # Config deep-merge shadowing trap: a user setting `integrations:` (or ci /
    # notifications) to null must not crash the registry.
    st = list_integrations({"integrations": None, "ci": None, "notifications": None})
    assert len(st) == 6
    assert all(s.configured is False for s in st)


def test_detail_never_contains_a_secret():
    cfg = {
        "integrations": {"jira": {"site": "s", "project_key": "P", "email": "e@x.com"}},
        "ci": {}, "notifications": {"slack_webhook_url": "https://hooks.slack.com/T/SECRETPART"},
    }
    for s in list_integrations(cfg):
        assert "SECRETPART" not in s.detail


@pytest.mark.asyncio
async def test_test_integration_jira_health_ok(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok-should-not-leak")
    calls = {}

    async def fake_get(url, headers=None, auth=None, timeout=None):
        calls["url"] = url
        calls["auth"] = auth
        class _R:
            status_code = 200
            def json(self):
                return {"displayName": "Eyal"}
        return _R()

    monkeypatch.setattr(reg, "_http_get", fake_get)
    cfg = {"integrations": {"jira": {"site": "https://acme.atlassian.net",
                                     "project_key": "P", "email": "me@x.com"}}}
    s = await reg.test_integration("jira", cfg)
    assert s.name == "jira"
    assert s.healthy is True
    assert "acme.atlassian.net" in calls["url"]
    assert calls["auth"] == ("me@x.com", "tok-should-not-leak")   # Basic auth
    assert "tok-should-not-leak" not in s.detail                  # never echoed


@pytest.mark.asyncio
async def test_test_integration_jira_health_fails_loudly(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        class _R:
            status_code = 401
            def json(self):
                return {}
        return _R()

    monkeypatch.setattr(reg, "_http_get", fake_get)
    cfg = {"integrations": {"jira": {"site": "https://acme.atlassian.net",
                                     "project_key": "P", "email": "me@x.com"}}}
    s = await reg.test_integration("jira", cfg)
    assert s.healthy is False
    assert "401" in s.detail


@pytest.mark.asyncio
async def test_test_integration_unconfigured_jira_is_not_healthy(monkeypatch):
    s = await reg.test_integration("jira", {"integrations": {"jira": {}}})
    assert s.configured is False
    assert s.healthy is False
    assert "not configured" in s.detail.lower()


@pytest.mark.asyncio
async def test_test_integration_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown integration"):
        await reg.test_integration("mystery", {})
