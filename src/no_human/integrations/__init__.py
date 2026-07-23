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
from pathlib import Path
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


@dataclass
class FieldSpec:
    """One configurable field of an integration, for the settings UI's forms.

    Exactly one of ``env_var`` / ``config_path`` is set: secrets (API tokens)
    live in ``~/.no_human/.env``; everything else is a dotted path into the
    user's ``config.yaml``. Names/paths here are the ones the corresponding
    integration module ALREADY reads (see the modules cited per field below) —
    nothing here is invented.
    """
    name: str
    label: str
    secret: bool
    env_var: str | None = None
    config_path: str | None = None


# github/gitlab/jenkins are STATUS VIEWS over the single shared `ci.*` section
# (see module docstring — one CI backend active at a time). Saving a field for
# one of them is how the UI selects it as that backend, so a successful save
# also pins `ci.backend` (+ `ci.enabled`) alongside whatever field was given.
_CI_BACKEND_BY_NAME = {"github": "github_actions", "gitlab": "gitlab", "jenkins": "jenkins"}

FIELD_SPECS: dict[str, list[FieldSpec]] = {
    # integrations/jira.py + intake/jira.py read integrations.jira.* / JIRA_API_TOKEN.
    "jira": [
        FieldSpec("site", "Site URL", False, config_path="integrations.jira.site"),
        FieldSpec("project_key", "Project key", False, config_path="integrations.jira.project_key"),
        FieldSpec("email", "Email", False, config_path="integrations.jira.email"),
        FieldSpec("jql", "JQL filter", False, config_path="integrations.jira.jql"),
        FieldSpec("api_token", "API token", True, env_var="JIRA_API_TOKEN"),
    ],
    # ci/circleci.py reads CIRCLECI_TOKEN; integrations.circleci.* is the
    # first-class config section (see _circleci_status above).
    "circleci": [
        FieldSpec("org_slug", "Org slug", False, config_path="integrations.circleci.org_slug"),
        FieldSpec("project", "Project", False, config_path="integrations.circleci.project"),
        FieldSpec("api_token", "API token", True, env_var="CIRCLECI_TOKEN"),
    ],
    # ci/github_actions.py + _ci_view read ci.project as the id_field for this backend.
    "github": [
        FieldSpec("project", "Project (owner/repo)", False, config_path="ci.project"),
    ],
    # ci/gitlab.py + _ci_view read the SAME ci.project key — one source of
    # truth, shared with github (only one backend is active at a time).
    "gitlab": [
        FieldSpec("project", "Project (namespace/repo)", False, config_path="ci.project"),
    ],
    # ci/jenkins.py reads ci.job (id_field) + JENKINS_USER / JENKINS_API_TOKEN.
    "jenkins": [
        FieldSpec("job", "Job path", False, config_path="ci.job"),
        FieldSpec("user", "Jenkins user", True, env_var="JENKINS_USER"),
        FieldSpec("api_token", "API token", True, env_var="JENKINS_API_TOKEN"),
    ],
    # cli/commands.py + api/app.py read notifications.slack_webhook_url. It is
    # a secret (never echoed back) even though it lives in config.yaml, not
    # .env — that is the location the existing code already reads it from.
    "slack": [
        FieldSpec("webhook_url", "Webhook URL", True, config_path="notifications.slack_webhook_url"),
    ],
}


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
# Write path (settings UI): FIELD_SPECS-validated save + field/set reporting.  #
#                                                                               #
# Paths are always resolved from the config module's ENV_PATH/CONFIG_PATH      #
# ATTRIBUTES (looked up fresh on every call, never captured as a default       #
# parameter) so that tests can monkeypatch them onto tmp_path and this code    #
# picks it up — the same discipline api/app.py's _persist_onboarding uses.     #
# --------------------------------------------------------------------------- #

def _get_dotted(config: dict, dotted: str) -> Any:
    node: Any = config or {}
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _set_dotted(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


# Single implementation lives in config, which owns ENV_PATH and every other
# .env read; aliased here so existing call sites are untouched.
from ..config import atomic_write_0600 as _atomic_write_0600  # noqa: E402
from ..config import upsert_env_var as _upsert_env_var  # noqa: E402


def _write_config_values(config_path: Path, updates: dict[str, Any]) -> None:
    """Read-modify-write config.yaml: read the RAW user file (never the
    defaults-merged view — the deep-merge shadowing trap), set the dotted
    path(s), and write back preserving every other key untouched."""
    import yaml

    from .. import config as _config_mod

    try:
        on_disk = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    except (yaml.YAMLError, OSError):
        on_disk = {}
    on_disk = on_disk or {}
    for dotted, value in updates.items():
        _set_dotted(on_disk, dotted, value)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _config_mod._atomic_write_text(config_path, yaml.safe_dump(on_disk, sort_keys=False))


def _field_is_set(spec: FieldSpec, config: dict) -> bool:
    if spec.env_var:
        from .. import config as _config_mod
        status = _config_mod.credential_status([spec.env_var], _config_mod.ENV_PATH)
        return bool(status.get(spec.env_var, False))
    return bool(_get_dotted(config, spec.config_path))


def integration_fields(name: str, config: dict) -> list[dict[str, Any]]:
    """The field descriptors for one integration's settings form — never a
    secret VALUE, only whether each field currently ``set``."""
    return [
        {"name": s.name, "label": s.label, "secret": s.secret, "set": _field_is_set(s, config)}
        for s in FIELD_SPECS.get(name, [])
    ]


def save_integration_config(name: str, fields: dict[str, str]) -> IntegrationStatus:
    """Validate ``fields`` against ``FIELD_SPECS[name]`` and persist them:
    secrets to ``~/.no_human/.env``, everything else to ``config.yaml``.
    Returns the refreshed :class:`IntegrationStatus`. Raises ``ValueError`` for
    an unknown integration or an unknown field name; never logs or returns a
    secret value."""
    specs = FIELD_SPECS.get(name)
    if specs is None:
        raise ValueError(f"unknown integration: {name!r}")

    by_field = {s.name: s for s in specs}
    unknown = sorted(set(fields) - set(by_field))
    if unknown:
        raise ValueError(f"unknown field(s) for integration {name!r}: {', '.join(unknown)}")

    # A value that is not exactly ONE .env line could inject arbitrary extra
    # entries (e.g. a forged CLAUDE_CODE_OAUTH_TOKEN= or ANTHROPIC_API_KEY=
    # line) — refuse before any write is dispatched. Never echo the offending
    # value back.
    #
    # This checked only \n and \r while the writer's own guard rejects every
    # separator `splitlines()` honours. The eight it missed therefore reached
    # the write loop below, which writes ONE KEY AT A TIME: the first key
    # landed on disk before a later one was refused, leaving .env half-updated
    # and the caller with a 500. Sharing the writer's guard is what makes the
    # loop effectively all-or-nothing.
    from ..config import AuthError, assert_single_env_line

    bad = []
    for f, v in sorted(fields.items()):
        if not isinstance(v, str):
            continue
        try:
            assert_single_env_line(v)
        except AuthError:
            bad.append(f)
    if bad:
        raise ValueError(
            f"field value(s) for integration {name!r} must be a single line: "
            f"{', '.join(bad)}"
        )

    from .. import config as _config_mod

    env_updates: dict[str, str] = {}
    config_updates: dict[str, Any] = {}
    for field_name, value in fields.items():
        spec = by_field[field_name]
        if spec.env_var:
            env_updates[spec.env_var] = value
        else:
            config_updates[spec.config_path] = value

    if fields and name in _CI_BACKEND_BY_NAME:
        config_updates.setdefault("ci.backend", _CI_BACKEND_BY_NAME[name])
        config_updates.setdefault("ci.enabled", True)

    for key, value in env_updates.items():
        _upsert_env_var(_config_mod.ENV_PATH, key, value)
    if config_updates:
        _write_config_values(_config_mod.CONFIG_PATH, config_updates)

    refreshed = _config_mod.load_config(_config_mod.CONFIG_PATH)
    return _STATUS[name](refreshed.data)


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


__all__ = [
    "IntegrationStatus", "KIND_BY_NAME", "list_integrations", "test_integration",
    "FieldSpec", "FIELD_SPECS", "integration_fields", "save_integration_config",
]
