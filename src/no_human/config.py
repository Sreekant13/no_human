"""Configuration loading and the subscription-auth safety boundary.

The single most important job in this module is preventing the daemon from
silently billing the metered Anthropic API. The Claude Agent SDK honours
``ANTHROPIC_API_KEY`` over ``CLAUDE_CODE_OAUTH_TOKEN`` when both are present, so
a stray key would quietly bill pay-per-token instead of the subscription. On
startup we scrub every metered-auth variable from the process environment and
assert that subscription mode is active before any task can run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Home for the user's private token + config. Never inside the repo.
NO_HUMAN_HOME = Path.home() / ".no_human"
ENV_PATH = NO_HUMAN_HOME / ".env"
CONFIG_PATH = NO_HUMAN_HOME / "config.yaml"
DB_PATH = NO_HUMAN_HOME / "no_human.db"

# The subscription token the SDK / `claude` CLI reads.
SUBSCRIPTION_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Variables that, if present, route to metered API / cloud billing instead of
# the subscription. ANTHROPIC_API_KEY is the dangerous one (wins precedence).
METERED_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_BEARER_TOKEN_BEDROCK",
)


class AuthError(RuntimeError):
    """Raised when the process is not provably in subscription-billing mode."""


@dataclass
class ScrubReport:
    """What the startup scrub found and removed."""

    removed: list[str] = field(default_factory=list)
    api_key_present: bool = False


def scrub_metered_auth(env: dict[str, str] | os._Environ | None = None) -> ScrubReport:
    """Remove every metered-auth variable from ``env`` (process env by default).

    Returns a report listing what was removed and whether the dangerous
    ``ANTHROPIC_API_KEY`` was among them. Callers decide whether its presence is
    fatal (see :func:`assert_subscription_mode`). Scrubbing is unconditional so
    that even a caller that swallows the error cannot fall through to metered
    billing.
    """
    target = os.environ if env is None else env
    report = ScrubReport()
    for var in METERED_AUTH_VARS:
        if var in target and target[var]:
            report.removed.append(var)
            if var == "ANTHROPIC_API_KEY":
                report.api_key_present = True
            del target[var]
    return report


def load_env_token(env_path: Path = ENV_PATH) -> str | None:
    """Load ``CLAUDE_CODE_OAUTH_TOKEN`` from ``~/.no_human/.env`` into the env.

    The .env is the source of truth (chmod 600, gitignored, never in the repo).
    A token already in the process environment is used as a fallback and is not
    overwritten. Returns the active token, or None if none is available.
    """
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # .env wins over an inherited token: it is the curated source.
            if key == SUBSCRIPTION_TOKEN_VAR and value:
                os.environ[SUBSCRIPTION_TOKEN_VAR] = value
    return os.environ.get(SUBSCRIPTION_TOKEN_VAR) or None


def assert_subscription_mode(
    env_path: Path = ENV_PATH,
    *,
    strict: bool = True,
) -> ScrubReport:
    """Enforce subscription billing before any task runs.

    1. Scrub all metered-auth variables from the process environment.
    2. If ``ANTHROPIC_API_KEY`` was present, refuse to start (``strict``) — the
       user must unset it; a silent scrub-and-continue would mask a real
       misconfiguration the operator should know about.
    3. Load and require the subscription token.

    Returns the :class:`ScrubReport` on success. Raises :class:`AuthError`
    otherwise.
    """
    report = scrub_metered_auth()

    if report.api_key_present and strict:
        raise AuthError(
            "ANTHROPIC_API_KEY is set in the environment. This would silently "
            "bill the metered API instead of your subscription. no_human runs "
            "on CLAUDE_CODE_OAUTH_TOKEN only.\n"
            "Unset it before starting:  unset ANTHROPIC_API_KEY\n"
            "(It has been scrubbed from this process, but startup is aborted so "
            "you can fix the source.)"
        )

    token = load_env_token(env_path)
    if not token:
        raise AuthError(
            f"No subscription token found. Expected {SUBSCRIPTION_TOKEN_VAR} in "
            f"{env_path} (chmod 600) or the process environment.\n"
            "Create one with:  claude setup-token"
        )
    return report


# --------------------------------------------------------------------------- #
# Config file                                                                  #
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8420},
    "llm": {
        # Subscription auth only. ANTHROPIC_API_KEY is intentionally absent.
        "auth_mode": "subscription",
        "primary_model": "claude-opus-4-8",
        "review_model": "claude-sonnet-4-6",
    },
    "database": {"path": str(DB_PATH)},
    "notifications": {
        # Write-only webhook, alert channel only. Read context uses separate
        # read-only tokens (Phase 1). None disables Slack (logs instead).
        "slack_webhook_url": None,
        "email_to": "dev@example.com",
    },
    "approval": {
        "require_before_merge": True,   # ALWAYS true — agent never merges
        "auto_merge_on_approval": False,  # there is no auto-merge
        "approval_timeout": "24h",
    },
    "git": {
        "branch_prefix": "no-human/",
        "commit_prefix": "",
        "never_push_to": ["main", "master", "release/*"],
        "agent_identity_name": "no_human",
        "agent_identity_email": "no-human@acme.com",
        "auto_pr": True,
    },
    "safety": {
        "max_files_changed": 20,
        "max_lines_changed": 500,
        "forbidden_paths": [".env", "secrets/", "*.key", "*.pem"],
        "block_test_weakening": True,
    },
    "bounds": {
        "max_attempts": 3,
        "max_turns_per_attempt": 40,
        "escalate_after": 3,
        "max_correction_rounds": 2,
    },
    "blockers": {
        # Part 22 blocker handling.
        "max_alternatives_before_escalate": 2,
        "max_park_duration": "48h",
        "wake_poll_interval": "10m",
        "transient_infra_retries": 2,
        "escalate_on_low_confidence_below": 0.6,
    },
    "ci": {
        # Opt-in per project. Set enabled=true and provide project path.
        "enabled": False,
        "backend": "gitlab",
        "project": "",
        "hostname": "gitlab.acme.net",
        "variables": {},          # extra KEY:VALUE pairs for glab ci run
        "timeout_minutes": 60,
        "max_infra_retries": 2,   # CLAUDE.md: retry after 2 min, max 2
        "poll_interval": 30,
        "result_parser": "pytest",  # or "surefire" for Maven projects
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base``."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class Config:
    """Resolved configuration: defaults overlaid with the user's config.yaml."""

    data: dict[str, Any]
    path: Path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def primary_model(self) -> str:
        return self.data["llm"]["primary_model"]

    @property
    def review_model(self) -> str:
        return self.data["llm"]["review_model"]

    @property
    def db_path(self) -> Path:
        return Path(self.data["database"]["path"]).expanduser()


def load_config(
    config_path: Path = CONFIG_PATH,
    *,
    create_if_missing: bool = True,
) -> Config:
    """Load ``~/.no_human/config.yaml``, generating a default if absent.

    Refuses to honour an ``ANTHROPIC_API_KEY`` smuggled into the config file —
    that variable must never appear in config (constraint §3.1).
    """
    if not config_path.exists() and create_if_missing:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))

    user_data: dict[str, Any] = {}
    if config_path.exists():
        user_data = yaml.safe_load(config_path.read_text()) or {}

    _reject_api_key_in_config(user_data)
    merged = _deep_merge(DEFAULT_CONFIG, user_data)
    return Config(data=merged, path=config_path)


def _reject_api_key_in_config(data: dict[str, Any]) -> None:
    """Fail loudly if a metered API key was placed in config (it never should)."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.upper() == "ANTHROPIC_API_KEY":
                    raise AuthError(
                        "ANTHROPIC_API_KEY must never appear in config.yaml. "
                        "no_human runs on subscription auth only."
                    )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
