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

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator

KIND_BY_NAME = {
    "jira": "issue_tracker",
    "linear": "issue_tracker",
    "github": "vcs",
    "gitlab": "vcs",
    "jenkins": "ci",
    "circleci": "ci",
    "slack": "notifications",
    "teams": "notifications",
}

# The order the UI lists them (issue tracker → VCS → CI → notifications).
_ORDER = ["jira", "linear", "github", "gitlab", "jenkins", "circleci", "slack", "teams"]


@dataclass
class IntegrationStatus:
    # One of the names in `_ORDER` below — that list is the source of truth.
    name: str
    kind: str            # "issue_tracker" | "vcs" | "ci" | "notifications"
    configured: bool
    healthy: bool | None  # None = never checked
    detail: str          # last check message, NEVER a secret
    # 'configured' (stored token/settings present) | 'ambient' (no stored
    # config, but the CLI the operator already uses — gh/git — is itself
    # authenticated, e.g. 36 PRs shipped via ambient `gh` auth with no
    # integration ever configured) | 'unconfigured'. Only github/gitlab are
    # ever 'ambient' — see `_AMBIENT_PROBES` below.
    status: str = "unconfigured"


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
    # intake/linear.py reads integrations.linear.* / LINEAR_API_KEY. The key
    # header is the RAW key (`Authorization: <key>`), not `Bearer <key>` —
    # see intake/linear.py's docstring.
    "linear": [
        FieldSpec("team_key", "Team key", False, config_path="integrations.linear.team_key"),
        FieldSpec("label", "Label filter", False, config_path="integrations.linear.label"),
        FieldSpec("api_key", "API key", True, env_var="LINEAR_API_KEY"),
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
    # notify/teams.py reads notifications.teams_webhook_url. Secret for the
    # same reason Slack's is: the Power Automate URL carries its own SAS
    # credential (`sp`/`sv`/`sig`) in the query string.
    "teams": [
        FieldSpec("webhook_url", "Webhook URL", True, config_path="notifications.teams_webhook_url"),
    ],
}


def _sect(config: dict, key: str) -> dict:
    """A config sub-section, tolerating a null (the deep-merge shadowing trap)."""
    return (config or {}).get(key) or {}


# --------------------------------------------------------------------------- #
# Pure status derivation (one function per integration)                        #
# --------------------------------------------------------------------------- #

def _status_str(configured: bool) -> str:
    return "configured" if configured else "unconfigured"


def _jira_status(config: dict) -> IntegrationStatus:
    j = _sect(config, "integrations").get("jira") or {}
    configured = bool(j.get("site") and j.get("project_key") and j.get("email"))
    detail = f"{j['site']} · {j['project_key']}" if configured else "not configured"
    return IntegrationStatus("jira", "issue_tracker", configured, None, detail,
                              status=_status_str(configured))


def _linear_status(config: dict) -> IntegrationStatus:
    lin = _sect(config, "integrations").get("linear") or {}
    team = lin.get("team_key")
    configured = bool(team)
    if configured:
        label = lin.get("label")
        detail = f"team {team}" + (f" · label {label}" if label else "")
    else:
        detail = "not configured"
    return IntegrationStatus("linear", "issue_tracker", configured, None, detail,
                              status=_status_str(configured))


def _teams_status(config: dict) -> IntegrationStatus:
    # The webhook is a secret — report only that one is set, never the URL.
    # A RETIRED Office 365 connector URL is reported as configured-but-broken
    # rather than as a working channel: Microsoft disabled those endpoints in
    # May 2026, so it can never deliver and saying "configured" would hide
    # that until an alert failed to arrive.
    from ..notify.teams import is_retired_connector_url

    url = _sect(config, "notifications").get("teams_webhook_url")
    configured = bool(url)
    if configured and is_retired_connector_url(url):
        return IntegrationStatus(
            "teams", "notifications", True, False,
            "retired Office 365 connector URL — replace with a Power Automate "
            "Workflows webhook", status="configured")
    detail = "webhook configured" if configured else "not configured"
    return IntegrationStatus("teams", "notifications", configured, None, detail,
                              status=_status_str(configured))


def _circleci_status(config: dict) -> IntegrationStatus:
    c = _sect(config, "integrations").get("circleci") or {}
    configured = bool(c.get("org_slug") and c.get("project"))
    detail = f"{c['org_slug']} · {c['project']}" if configured else "not configured"
    return IntegrationStatus("circleci", "ci", configured, None, detail,
                              status=_status_str(configured))


def _ci_view(config: dict, name: str, backend: str, kind: str,
             id_field: str, label: str) -> IntegrationStatus:
    """A status view over ``ci.*`` for a backend the CI layer already owns."""
    ci = _sect(config, "ci")
    configured = bool(ci.get("enabled") and ci.get("backend") == backend and ci.get(id_field))
    detail = f"{label} · {ci[id_field]}" if configured else "not configured"
    return IntegrationStatus(name, kind, configured, None, detail,
                              status=_status_str(configured))


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
    return IntegrationStatus("slack", "notifications", configured, None, detail,
                              status=_status_str(configured))


_STATUS = {
    "jira": _jira_status, "linear": _linear_status, "github": _github_status,
    "gitlab": _gitlab_status, "jenkins": _jenkins_status,
    "circleci": _circleci_status, "slack": _slack_status, "teams": _teams_status,
}


def list_integrations(config: dict) -> list[IntegrationStatus]:
    """Every integration's configured/kind status. Pure; ``healthy`` is None."""
    return [_STATUS[name](config) for name in _ORDER]


# --------------------------------------------------------------------------- #
# Ambient CLI-auth detection (SCRUM-81).                                       #
#                                                                               #
# Some providers work with no integration ever configured here because the    #
# operator's own CLI is already authenticated (e.g. `gh`) — this install has   #
# shipped merged GitHub PRs entirely via ambient `gh`/git auth while the panel #
# still said "Unconfigured". These probes are read-only: they never write a    #
# credential anywhere, and never surface a token/secret value.                 #
# --------------------------------------------------------------------------- #

def _run_probe(
    cmd: list[str], *, timeout: float = 2.0, input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, input=input_text, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_credential_present(host: str) -> bool:
    """Ask git's OWN credential subsystem whether it can produce a credential
    for ``host``, without prompting and without a network round-trip:
    `git credential fill` consults whatever helper is configured (netrc,
    credential store, osxkeychain, manager, `gh auth git-credential`, ...) and
    returns immediately if none has anything — GIT_TERMINAL_PROMPT=0 + a no-op
    GIT_ASKPASS guarantee it never blocks waiting on input. Only WHETHER a
    non-empty `password=` line came back is inspected — never its value — so
    this can never leak a secret. A username alone (e.g. a bare
    `credential.<host>.username` config entry with no stored password) is a
    preference, not proof of an authenticated session, and must not read as
    ambient. `fill` only reads; storing is `approve`/`reject`, never issued
    here.

    The credential's lifetime is bounded to this frame on purpose: `stdout`
    carries `password=<TOKEN>`, and a function's locals stay reachable from a
    traceback for as long as the frame does, so the reply is cleared before
    returning either way. Nothing in this package renders frame locals and
    nothing after the read can raise, so that was never exploitable — it is
    done anyway, because the alternative is a containment claim that promises
    more than its mechanism delivers."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/true"}
    proc = _run_probe(
        ["git", "credential", "fill"],
        input_text=f"protocol=https\nhost={host}\n\n", env=env,
    )
    if proc is None or proc.returncode != 0:
        return False
    try:
        return any(
            line.startswith("password=") and line != "password="
            for line in proc.stdout.splitlines()
        )
    finally:
        proc.stdout = ""
        del proc


#: Token variables `gh` itself prefers over any stored credential. Presence of
#: one is checked, never its value.
_GH_TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


def _gh_hosts_path() -> Path:
    """`gh`'s `hosts.yml`, resolved the way gh resolves it: `GH_CONFIG_DIR`,
    else `XDG_CONFIG_HOME/gh`, else `~/.config/gh`."""
    base = os.environ.get("GH_CONFIG_DIR") or ""
    if not base:
        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        base = str(Path(xdg) / "gh") if xdg else str(Path.home() / ".config" / "gh")
    return Path(base) / "hosts.yml"


def _gh_hosts_block_lines(text: str, host: str) -> Iterator[str]:
    """Yield only the lines of a `hosts.yml` that sit under the top-level
    ``host:`` key. `hosts.yml` is a map of HOST -> settings, so a token in it
    belongs to whichever host block encloses it; a scan that ignores the
    enclosing block reports a credential for an enterprise host as if it were
    a github.com one. (`gh auth status` had exactly that any-host semantics —
    "exits 0 iff at least one host is logged in" — so scoping this is a
    tightening rather than a regression, but it has to agree with
    `_git_credential_present`, which asks about one host by name.)

    A top-level key is a line starting in column 0; everything indented after
    it belongs to that block, which is all the structure this needs and is why
    it does not pull in a YAML parser (one would bind the whole parsed tree,
    tokens included, to a local)."""
    in_block = False
    for line in text.splitlines():
        if line[:1] not in (" ", "\t", ""):
            in_block = line.split(":", 1)[0].strip().strip("\"'") == host
        elif in_block:
            yield line


def _is_gh_oauth_token_line(line: str) -> bool:
    """True iff *line* is a `hosts.yml` `oauth_token:` entry with something
    after the colon. Takes a line and returns a bool: the value is touched only
    inside this frame, is never returned, stored, logged or compared against
    anything, and nothing here can raise (so it can never reach a traceback
    either). Empty and `""`/`''` placeholders are not a credential."""
    stripped = line.strip()
    if not stripped.startswith("oauth_token:"):
        return False
    return bool(stripped[len("oauth_token:"):].strip().strip("\"'"))


def _probe_github_ambient() -> bool:
    """Is a GitHub credential PRESENT on this machine? Presence only — never
    validity — and with no network round-trip.

    WHY THIS IS NOT `gh auth status`, which it was until 2026-08-02: that
    command is not a local check. It validates the stored token against the
    GitHub API — measured on a dev machine at 1700 ms and 2036 ms, against
    85-93 ms for the local credential read below and ~10 ms for a file read.
    So the old probe TRANSMITTED THE OPERATOR'S GITHUB TOKEN, undisclosed, and
    it did so precisely when GitHub was UNCONFIGURED, since that is the only
    time an ambient probe runs — inverting the one guarantee the reader who
    configured nothing is entitled to. Deciding whether an integration is worth
    OFFERING needs presence, not validity; an expired token fails later,
    visibly, at the point of use, which is a better place to learn it.

    Three local sources, each covering a case the others cannot see, checked
    cheapest-first and short-circuiting:

    1. `GH_TOKEN` / `GITHUB_TOKEN` in the environment — what gh itself prefers
       over stored credentials, and invisible to (2).
    2. git's credential subsystem for github.com, via the same
       `_git_credential_present` helper `_probe_gitlab_ambient` uses. This is
       the case that matters most in practice: `gh auth login` stores the token
       in the OS keyring (not in a file) and registers
       `gh auth git-credential` as github.com's helper, so this is the only
       local way to see a keyring-stored login.
    3. a non-empty `oauth_token:` line **inside the `github.com:` block** of
       gh's `hosts.yml` — a gh login on a machine with no keyring, or where
       `gh auth setup-git` never ran so (2) has no helper to ask. Host-scoped,
       because a token under `ghe.corp.example:` is a credential for that host
       and reporting github.com as ambient on the strength of it would be the
       false "yes" this docstring calls a lie. The cost is that a GHE-only
       login no longer reads as ambient here; that is the fail-closed side.

    Only WHETHER a non-empty token exists is ever inspected. No value is
    returned, logged, stored, or put in an exception message, and none survives
    the frame that inspects it — see the lifetime note in
    `_git_credential_present`. `gh auth token`, which prints the token in the
    clear, is deliberately not used. What this does NOT claim is that no value
    is ever bound at all: `read_text` necessarily holds the file, and a
    credential helper's reply necessarily exists for the length of the check.

    Fails CLOSED — if it cannot tell, it reports "not present" and the panel
    says "Unconfigured". A false "no" costs a suggestion; a false "yes" is a
    lie, and the only ways to shrink that gap further would be to prompt the
    operator for keychain access or to validate on the wire, which is the
    defect this replaces."""
    if any(os.environ.get(v, "").strip() for v in _GH_TOKEN_ENV_VARS):
        return True
    if _git_credential_present("github.com"):
        return True
    try:
        # errors="replace" so an undecodable byte fails closed to False rather
        # than raising a ValueError past the OSError guard.
        return any(
            _is_gh_oauth_token_line(line)
            for line in _gh_hosts_block_lines(
                _gh_hosts_path().read_text(errors="replace"), "github.com")
        )
    except OSError:
        return False


def _probe_gitlab_ambient() -> bool:
    """Is a GitLab credential present? Same local, presence-only mechanism as
    the GitHub probe — see `_git_credential_present`."""
    return _git_credential_present("gitlab.com")


# Only github/gitlab have an ambient path — jira/circleci/jenkins/slack have no
# equivalent "already authenticated CLI" concept.
_AMBIENT_PROBES: dict[str, Callable[[], bool]] = {
    "github": _probe_github_ambient,
    "gitlab": _probe_gitlab_ambient,
}

_AMBIENT_TTL_SECONDS = 60.0

# Process-lifetime cache, keyed by provider name → (checked_at, result). This
# app has no multi-user/session concept (single-operator local tool — see
# `_require_local_origin`), so the running server process IS the "session";
# the cache never stores a credential, only a bool + timestamp, and evaporates
# on restart. Tests inject their own `cache=` dict to isolate state.
_AMBIENT_CACHE: dict[str, tuple[float, bool]] = {}


def ambient_available(
    name: str, *, cache: dict[str, tuple[float, bool]] | None = None, now: float | None = None,
) -> bool:
    """Is ``name`` reachable via ambient CLI auth right now? Cached for
    ``_AMBIENT_TTL_SECONDS`` so a burst of requests within the window doesn't
    repeatedly shell out to `gh`/`git`."""
    probe = _AMBIENT_PROBES.get(name)
    if probe is None:
        return False
    if cache is None:
        cache = _AMBIENT_CACHE
    ts = time.monotonic() if now is None else now
    cached = cache.get(name)
    if cached is not None and (ts - cached[0]) < _AMBIENT_TTL_SECONDS:
        return cached[1]
    result = probe()
    cache[name] = (ts, result)
    return result


_AMBIENT_DETAIL = "available via ambient CLI auth"


def list_integrations_with_ambient(
    config: dict, *, cache: dict[str, tuple[float, bool]] | None = None, now: float | None = None,
) -> list[IntegrationStatus]:
    """``list_integrations`` plus the ambient-auth overlay: an unconfigured
    github/gitlab whose CLI is already authenticated is reported as
    ``status="ambient"`` instead of ``"unconfigured"`` (``configured`` stays
    False — no stored settings exist)."""
    out = []
    for s in list_integrations(config):
        if not s.configured and ambient_available(s.name, cache=cache, now=now):
            s = replace(s, status="ambient", detail=_AMBIENT_DETAIL)
        out.append(s)
    return out


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
    status = _STATUS[name](refreshed.data)
    # Saving (or clearing) a field must not make an ambiently-authenticated
    # github/gitlab look worse than the list endpoint already reports it —
    # overlay the same ambient check here (see list_integrations_with_ambient).
    if not status.configured and ambient_available(name):
        status = replace(status, status="ambient", detail=_AMBIENT_DETAIL)
    return status


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


async def _http_post(url, headers=None, json=None, timeout=10.0):
    """Thin async POST seam (monkeypatched in tests; real impl uses httpx).

    Linear's API is GraphQL-only, so its health check cannot reuse
    ``_http_get``.
    """
    import httpx
    async with httpx.AsyncClient() as client:
        return await client.post(url, headers=headers, json=json, timeout=timeout)


async def _check_linear(config: dict) -> IntegrationStatus:
    base = _linear_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        return replace(base, healthy=False,
                       detail="LINEAR_API_KEY not set in ~/.no_human/.env")
    from ..intake.linear import API_URL
    try:
        r = await _http_post(
            API_URL,
            # RAW key, not Bearer — Linear's documented personal-key header.
            headers={"Authorization": key, "Content-Type": "application/json"},
            json={"query": "{ viewer { id name } }"}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — a health check never raises
        return replace(base, healthy=False, detail=f"connection failed: {type(exc).__name__}")
    # Linear returns field errors at 200, auth failure at 401 and rate limiting
    # at 400 — every one of them carries an `errors` array, so a 200 alone does
    # not mean success.
    body: Any = {}
    try:
        body = r.json() or {}
    except Exception:  # noqa: BLE001
        body = {}
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        code = ((first.get("extensions") or {}).get("code") or "") if isinstance(first, dict) else ""
        if code == "RATELIMITED":
            return replace(base, healthy=False,
                           detail="rate limited (Linear reports this as HTTP 400)")
        return replace(base, healthy=False, detail=f"API error: {code or 'unknown'}")
    if r.status_code == 200:
        who = ""
        if isinstance(body, dict):
            who = ((body.get("data") or {}).get("viewer") or {}).get("name", "")
        return replace(base, healthy=True,
                       detail=f"authenticated as {who}" if who else "authenticated")
    return replace(base, healthy=False, detail=f"HTTP {r.status_code}")


async def _check_teams(config: dict) -> IntegrationStatus:
    """Teams is a status view, not a ping.

    There is deliberately no live probe: the only way to exercise a Workflows
    webhook is to POST a message, and Microsoft's Graph/Teams terms state it is
    a violation "to use Microsoft Teams as a log file — only send messages that
    people will read". A health check must not put noise in a human's channel.
    What IS checked is the one failure we can see without sending: a retired
    Office 365 connector URL, which can never deliver.
    """
    base = _teams_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    if base.healthy is False:      # retired connector URL, detail already set
        return base
    return replace(base, healthy=True,
                   detail=f"{base.detail} — verified by the webhook at run time")


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


async def _check_view(status_fn, name: str, config: dict) -> IntegrationStatus:
    """github/gitlab/jenkins/slack are status views — 'healthy' mirrors
    'configured'; the live connection is exercised by the CI backend / webhook
    at run time, so a separate ping here would be a second, weaker truth. An
    unconfigured github/gitlab that is nonetheless ambiently authenticated
    (SCRUM-81) reports ``status="ambient"``/``healthy=None`` instead of a flat
    'not configured' — this endpoint must agree with `list_integrations_with_
    ambient`, not contradict it."""
    base = status_fn(config)
    if not base.configured:
        # ambient_available() can shell out (subprocess.run) — never block
        # this coroutine's event loop; offload it exactly like every other
        # blocking call in this codebase.
        if await asyncio.to_thread(ambient_available, name):
            return replace(base, healthy=None, detail=_AMBIENT_DETAIL, status="ambient")
        return replace(base, healthy=False, detail="not configured")
    return replace(base, healthy=True,
                   detail=f"{base.detail} — verified by the backend at run time")


_CHECKERS = {
    "jira": _check_jira,
    "linear": _check_linear,
    "teams": _check_teams,
    "circleci": _check_circleci,
    "github": lambda c: _check_view(_github_status, "github", c),
    "gitlab": lambda c: _check_view(_gitlab_status, "gitlab", c),
    "jenkins": lambda c: _check_view(_jenkins_status, "jenkins", c),
    "slack": lambda c: _check_view(_slack_status, "slack", c),
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
    "ambient_available", "list_integrations_with_ambient",
]
