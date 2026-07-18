"""Integrations registry — a status layer over the config.

Two integrations are first-class config sections (``integrations.jira`` and
``integrations.circleci``); the rest are read-only STATUS VIEWS over existing
config: github / gitlab / jenkins over ``ci.*`` and slack over
``notifications.*``. There is exactly one source of truth per setting — no
duplicate keys.

``list_integrations`` is pure and synchronous (never a secret in a detail
string, ``healthy`` always None until checked). ``test_integration`` runs a
live health check for one integration and returns the same shape with
``healthy`` set. Tokens are read from the process env (loaded from
``~/.no_human/.env`` at the CLI/API boundary) — never from config, never
echoed back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

KIND_BY_NAME = {
    "jira": "issue_tracker",
    "github": "vcs",
    "gitlab": "vcs",
    "jenkins": "ci",
    "circleci": "ci",
    "slack": "notifications",
}

# The order the UI lists them (issue tracker → VCS → CI → notifications).
_ORDER = ["jira", "github", "gitlab", "jenkins", "circleci", "slack"]


@dataclass
class IntegrationStatus:
    name: str            # "jira" | "github" | "gitlab" | "jenkins" | "circleci" | "slack"
    kind: str            # "issue_tracker" | "vcs" | "ci" | "notifications"
    configured: bool
    healthy: bool | None  # None = never checked
    detail: str          # last check message, NEVER a secret


def _sect(config: dict, key: str) -> dict:
    """A config sub-section, tolerating a null (the deep-merge shadowing trap)."""
    return (config or {}).get(key) or {}


# --------------------------------------------------------------------------- #
# Pure status derivation (one function per integration)                        #
# --------------------------------------------------------------------------- #

def _jira_status(config: dict) -> IntegrationStatus:
    j = _sect(config, "integrations").get("jira") or {}
    configured = bool(j.get("site") and j.get("project_key") and j.get("email"))
    detail = f"{j['site']} · {j['project_key']}" if configured else "not configured"
    return IntegrationStatus("jira", "issue_tracker", configured, None, detail)


def _circleci_status(config: dict) -> IntegrationStatus:
    c = _sect(config, "integrations").get("circleci") or {}
    configured = bool(c.get("org_slug") and c.get("project"))
    detail = f"{c['org_slug']} · {c['project']}" if configured else "not configured"
    return IntegrationStatus("circleci", "ci", configured, None, detail)


def _ci_view(config: dict, name: str, backend: str, kind: str,
             id_field: str, label: str) -> IntegrationStatus:
    """A status view over ``ci.*`` for a backend the CI layer already owns."""
    ci = _sect(config, "ci")
    configured = bool(ci.get("enabled") and ci.get("backend") == backend and ci.get(id_field))
    detail = f"{label} · {ci[id_field]}" if configured else "not configured"
    return IntegrationStatus(name, kind, configured, None, detail)


def _github_status(config: dict) -> IntegrationStatus:
    return _ci_view(config, "github", "github_actions", "vcs", "project", "GitHub Actions")


def _gitlab_status(config: dict) -> IntegrationStatus:
    return _ci_view(config, "gitlab", "gitlab", "vcs", "project", "GitLab CI")


def _jenkins_status(config: dict) -> IntegrationStatus:
    return _ci_view(config, "jenkins", "jenkins", "ci", "job", "Jenkins")


def _slack_status(config: dict) -> IntegrationStatus:
    # The webhook is a secret — report only that one is set, never the URL.
    configured = bool(_sect(config, "notifications").get("slack_webhook_url"))
    detail = "webhook configured" if configured else "not configured"
    return IntegrationStatus("slack", "notifications", configured, None, detail)


_STATUS = {
    "jira": _jira_status, "github": _github_status, "gitlab": _gitlab_status,
    "jenkins": _jenkins_status, "circleci": _circleci_status, "slack": _slack_status,
}


def list_integrations(config: dict) -> list[IntegrationStatus]:
    """Every integration's configured/kind status. Pure; ``healthy`` is None."""
    return [_STATUS[name](config) for name in _ORDER]


# --------------------------------------------------------------------------- #
# Live health checks                                                           #
# --------------------------------------------------------------------------- #

async def _http_get(url, headers=None, auth=None, timeout=10.0):
    """Thin async GET seam (monkeypatched in tests; real impl uses httpx)."""
    import httpx
    async with httpx.AsyncClient() as client:
        return await client.get(url, headers=headers, auth=auth, timeout=timeout)


async def _check_jira(config: dict) -> IntegrationStatus:
    base = _jira_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    token = os.environ.get("JIRA_API_TOKEN")
    if not token:
        return replace(base, healthy=False,
                       detail="JIRA_API_TOKEN not set in ~/.no_human/.env")
    j = _sect(config, "integrations").get("jira") or {}
    url = f"{j['site'].rstrip('/')}/rest/api/3/myself"
    try:
        r = await _http_get(url, auth=(j["email"], token), timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — a health check never raises
        return replace(base, healthy=False, detail=f"connection failed: {type(exc).__name__}")
    if r.status_code == 200:
        who = ""
        try:
            who = (r.json() or {}).get("displayName", "")
        except Exception:  # noqa: BLE001
            pass
        return replace(base, healthy=True,
                       detail=f"authenticated as {who}" if who else "authenticated")
    return replace(base, healthy=False, detail=f"HTTP {r.status_code}")


async def _check_circleci(config: dict) -> IntegrationStatus:
    base = _circleci_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    token = os.environ.get("CIRCLECI_TOKEN")
    if not token:
        return replace(base, healthy=False,
                       detail="CIRCLECI_TOKEN not set in ~/.no_human/.env")
    try:
        r = await _http_get("https://circleci.com/api/v2/me",
                            headers={"Circle-Token": token}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return replace(base, healthy=False, detail=f"connection failed: {type(exc).__name__}")
    if r.status_code == 200:
        who = ""
        try:
            who = (r.json() or {}).get("login", "")
        except Exception:  # noqa: BLE001
            pass
        return replace(base, healthy=True,
                       detail=f"authenticated as {who}" if who else "authenticated")
    return replace(base, healthy=False, detail=f"HTTP {r.status_code}")


async def _check_view(status_fn, config: dict) -> IntegrationStatus:
    """github/gitlab/jenkins/slack are status views — 'healthy' mirrors
    'configured'; the live connection is exercised by the CI backend / webhook
    at run time, so a separate ping here would be a second, weaker truth."""
    base = status_fn(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    return replace(base, healthy=True,
                   detail=f"{base.detail} — verified by the backend at run time")


_CHECKERS = {
    "jira": _check_jira,
    "circleci": _check_circleci,
    "github": lambda c: _check_view(_github_status, c),
    "gitlab": lambda c: _check_view(_gitlab_status, c),
    "jenkins": lambda c: _check_view(_jenkins_status, c),
    "slack": lambda c: _check_view(_slack_status, c),
}


async def test_integration(name: str, config: dict) -> IntegrationStatus:
    """Run a live health check for one integration; return its status with
    ``healthy`` set. Never raises on a network error (captured into ``detail``)."""
    checker = _CHECKERS.get(name)
    if checker is None:
        raise ValueError(f"unknown integration: {name!r}")
    return await checker(config)


__all__ = ["IntegrationStatus", "KIND_BY_NAME", "list_integrations", "test_integration"]
