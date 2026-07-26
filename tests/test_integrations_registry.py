"""Integrations registry: status list + health checks over the config.

The registry is a STATUS layer — jira/circleci are first-class config sections;
github/gitlab/jenkins are read-only views over ``ci.*`` and slack over
``notifications.*``. Secrets are never surfaced in a status detail.
"""

import subprocess

import pytest

from no_human import integrations as reg
from no_human.integrations import (
    IntegrationStatus,
    list_integrations,
    list_integrations_with_ambient,
)
# NB: the product fn is `integrations.test_integration`; referenced as
# `reg.test_integration` so pytest doesn't collect it as a test case.

_CONFIGURED_GITHUB_CFG = {
    "integrations": {}, "notifications": {},
    "ci": {"enabled": True, "backend": "github_actions", "project": "o/r", "job": ""},
}
_CONFIGURED_GITLAB_CFG = {
    "integrations": {}, "notifications": {},
    "ci": {"enabled": True, "backend": "gitlab", "project": "ns/r", "job": ""},
}
_UNCONFIGURED_CFG = {"integrations": {}, "ci": {}, "notifications": {}}


@pytest.mark.parametrize("name,configured_cfg", [
    ("github", _CONFIGURED_GITHUB_CFG),
    ("gitlab", _CONFIGURED_GITLAB_CFG),
])
def test_ambient_stays_configured_when_stored_config_present(mock_ambient_probes, name, configured_cfg):
    # Even if the CLI is also ambiently authenticated, a stored/configured
    # integration must never be downgraded/relabelled to "ambient".
    mock_ambient_probes._AMBIENT_PROBES[name] = lambda: True
    st = {s.name: s for s in list_integrations_with_ambient(configured_cfg, cache={})}
    assert st[name].configured is True
    assert st[name].status == "configured"


@pytest.mark.parametrize("name", ["github", "gitlab"])
def test_ambient_reported_when_unconfigured_and_cli_authenticated(mock_ambient_probes, name):
    mock_ambient_probes._AMBIENT_PROBES[name] = lambda: True
    st = {s.name: s for s in list_integrations_with_ambient(_UNCONFIGURED_CFG, cache={})}
    assert st[name].configured is False
    assert st[name].status == "ambient"


@pytest.mark.parametrize("name", ["github", "gitlab"])
def test_unconfigured_stays_unconfigured_when_cli_not_authenticated(mock_ambient_probes, name):
    mock_ambient_probes._AMBIENT_PROBES[name] = lambda: False
    st = {s.name: s for s in list_integrations_with_ambient(_UNCONFIGURED_CFG, cache={})}
    assert st[name].configured is False
    assert st[name].status == "unconfigured"


def test_ambient_available_caches_within_ttl_without_reprobing(monkeypatch):
    calls = []

    def fake_probe():
        calls.append(1)
        return True

    monkeypatch.setitem(reg._AMBIENT_PROBES, "github", fake_probe)
    cache = {}
    assert reg.ambient_available("github", cache=cache, now=1000.0) is True
    assert reg.ambient_available("github", cache=cache, now=1030.0) is True  # within 60s
    assert len(calls) == 1  # not re-probed
    assert reg.ambient_available("github", cache=cache, now=1070.0) is True  # past TTL (>60s)
    assert len(calls) == 2


def test_ambient_cache_never_stores_a_credential_only_bool_and_timestamp(monkeypatch):
    monkeypatch.setitem(reg._AMBIENT_PROBES, "github", lambda: True)
    cache = {}
    reg.ambient_available("github", cache=cache, now=5.0)
    ts, result = cache["github"]
    assert isinstance(ts, float)
    assert isinstance(result, bool)


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess — only the two fields the
    probes actually read."""
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_run_probe_returns_process_on_success(monkeypatch):
    monkeypatch.setattr(reg.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))
    proc = reg._run_probe(["true"])
    assert proc is not None
    assert proc.returncode == 0


def test_run_probe_returns_none_on_os_error(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("no such file or directory")
    monkeypatch.setattr(reg.subprocess, "run", _raise)
    assert reg._run_probe(["nonexistent-binary"]) is None


def test_run_probe_returns_none_on_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else k.get("cmd"), timeout=k.get("timeout", 2.0))
    monkeypatch.setattr(reg.subprocess, "run", _raise)
    assert reg._run_probe(["slow-binary"]) is None


def test_run_probe_passes_a_finite_timeout_to_subprocess_run(monkeypatch):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(reg.subprocess, "run", _fake_run)
    reg._run_probe(["gh", "auth", "status"])
    assert isinstance(captured.get("timeout"), (int, float))
    # Pinned to the shipped default: 5.0 was the pre-SCRUM-81 value, and a
    # loose bound here let a revert back to it pass unnoticed.
    assert 0 < captured["timeout"] <= 2.0


def test_probe_github_ambient_true_on_returncode_zero(monkeypatch):
    monkeypatch.setattr(reg.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))
    assert reg._probe_github_ambient() is True


def test_probe_github_ambient_false_on_nonzero_returncode(monkeypatch):
    # A runner that FAILED (e.g. `gh` installed but logged out) must never
    # read as ambient — the mutation that drops this check survived once.
    monkeypatch.setattr(reg.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1))
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_false_when_probe_errors(monkeypatch):
    monkeypatch.setattr(reg, "_run_probe", lambda *a, **k: None)
    assert reg._probe_github_ambient() is False


def test_probe_gitlab_ambient_true_when_password_line_present(monkeypatch):
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(
            returncode=0, stdout="protocol=https\nhost=gitlab.com\npassword=secret\n",
        ),
    )
    assert reg._probe_gitlab_ambient() is True


def test_probe_gitlab_ambient_false_on_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout="password=secret\n"),
    )
    assert reg._probe_gitlab_ambient() is False


def test_probe_gitlab_ambient_false_when_only_a_username_is_configured(monkeypatch):
    # The ticket's own bug, reversed: a bare username with no stored password
    # is a preference, not proof of an authenticated session.
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(
            returncode=0, stdout="protocol=https\nhost=gitlab.com\nusername=someone\n",
        ),
    )
    assert reg._probe_gitlab_ambient() is False


def test_probe_gitlab_ambient_false_when_password_line_is_empty(monkeypatch):
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="password=\n"),
    )
    assert reg._probe_gitlab_ambient() is False


def test_probe_gitlab_ambient_false_when_probe_errors(monkeypatch):
    monkeypatch.setattr(reg, "_run_probe", lambda *a, **k: None)
    assert reg._probe_gitlab_ambient() is False


def test_ambient_unknown_provider_is_never_ambient(mock_ambient_probes):
    # jira/circleci/jenkins/slack have no ambient concept.
    st = {s.name: s for s in list_integrations_with_ambient(_UNCONFIGURED_CFG, cache={})}
    for name in ("jira", "jenkins", "circleci", "slack"):
        assert st[name].status == "unconfigured"


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
