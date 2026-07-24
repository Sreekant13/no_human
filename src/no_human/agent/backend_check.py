"""Is the coding backend actually usable? A pre-flight readiness check.

The board can render green while every task silently fails. ``nh start`` only
requires that an OAuth *token* is on file (``assert_subscription_mode``), and
``nh doctor`` is a HISTORICAL liveness check over past events — neither verifies
that the ``claude`` CLI the Claude Agent SDK shells out to actually exists on
this machine. A fresh install with a valid token but no CLI installed passes
both checks, then every task dies at the first SDK call with
``CLINotFoundError`` — an absence, not a crash, exactly the failure mode this
project keeps getting bitten by (TESTING dead for the system's whole life, the
watcher that persisted nothing). This makes that absence loud and early.

The CLI search MIRRORS the SDK's own resolution
(``claude_agent_sdk._internal.transport.subprocess_cli._find_cli``): the bundled
binary first, then ``PATH``, then the known npm/local install locations. Kept in
lockstep so this never reports "missing" for a CLI the SDK would have found, nor
"present" for one it would not.

Read-only and side-effect-free: it resolves paths and reads config, it never
spawns the CLI (a live auth probe would spend quota) and never mutates the env.
"""

from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Mirror of the SDK's fallback search list (subprocess_cli._find_cli). Order is
# not load-bearing for presence, but kept identical so a divergence is obvious.
_CLI_FALLBACK_LOCATIONS = (
    ".npm-global/bin/claude",
    ".local/bin/claude",
    "node_modules/.bin/claude",
    ".yarn/bin/claude",
    ".claude/local/claude",
)


def find_claude_cli() -> str | None:
    """Resolve the ``claude`` CLI exactly as the Claude Agent SDK does, or None.

    Bundled (inside the installed SDK package) → ``shutil.which`` → known install
    locations. Returns the resolved path string, or ``None`` when nothing the SDK
    would accept exists.
    """
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"

    # 1. Bundled CLI shipped inside the SDK package. The SDK computes this as
    #    <package_root>/_bundled/<cli_name>; package_root is the dir holding
    #    claude_agent_sdk/__init__.py.
    try:
        import claude_agent_sdk

        bundled = (
            Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / cli_name
        )
        if bundled.is_file():
            return str(bundled)
    except Exception:  # noqa: BLE001 — SDK absent/unimportable ⇒ no bundled CLI
        pass

    # 2. System-wide search on PATH.
    if cli := shutil.which("claude"):
        return cli

    # 3. Known install locations (absolute + $HOME-relative), as the SDK checks.
    home = Path.home()
    candidates = [Path("/usr/local/bin/claude")]
    candidates += [home / rel for rel in _CLI_FALLBACK_LOCATIONS]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _token_on_file(profile: str | None = None) -> bool:
    """Whether an OAuth token for *profile* resolves from env or ~/.no_human/.env.

    Never returns or logs the token value. A best-effort read: any failure to
    resolve the profile name is treated as "no token" rather than raising, so a
    readiness probe never crashes on a malformed config.
    """
    try:
        from ..config import load_env_var, profile_token_var

        return bool(load_env_var(profile_token_var(profile or "default")))
    except Exception:  # noqa: BLE001
        return False


@dataclass
class BackendStatus:
    """The result of a coding-backend readiness probe."""

    cli_path: str | None
    token_present: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.cli_path is not None and self.token_present


def check_backend(*, token_present: bool | None = None,
                  profile: str | None = None) -> BackendStatus:
    """Probe whether the coding backend can actually run a task.

    ``token_present`` may be supplied by a caller that already knows it (the
    board's auth payload, the startup auth assertion); when ``None`` it is read
    from config. The check is CLI-presence + token-presence — deliberately not a
    live auth call, which would cost quota and could not run inside ``nh doctor``.
    """
    cli = find_claude_cli()
    if token_present is None:
        token_present = _token_on_file(profile)
    reasons: list[str] = []
    if cli is None:
        reasons.append(
            "the `claude` CLI is not installed or not on PATH — the Claude Agent "
            "SDK shells out to it for every task, so every task would fail at "
            "launch. Install it with: npm install -g @anthropic-ai/claude-code"
        )
    if not token_present:
        reasons.append(
            "no OAuth token on file — expected CLAUDE_CODE_OAUTH_TOKEN in "
            "~/.no_human/.env or the environment. Create one with: "
            "claude setup-token"
        )
    return BackendStatus(cli_path=cli, token_present=bool(token_present),
                         reasons=reasons)
