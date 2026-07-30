"""`nh init` — guided first-run setup.

Walks a new user from zero to a working no_human installation:
  1. Check prerequisites (python, uv, git, claude CLI).
  2. Create ``~/.no_human/`` with correct permissions.
  3. Set up the subscription token (detect or guide).
  4. Generate default ``config.yaml`` with git identity pre-populated.
  5. Optionally onboard a first repo.
  6. Print a summary card with next steps.

Idempotent: running twice never destroys existing config, secrets, or data.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.panel import Panel

from . import print_path_error
from ..config import (
    API_KEY_VAR,
    CONFIG_PATH,
    DB_PATH,
    ENV_PATH,
    METERED_AUTH_VARS,
    NO_HUMAN_HOME,
    SUBSCRIPTION_TOKEN_VAR,
    credential_status,
    load_config,
)

console = Console()

# Required external tools. Each: (binary, version_flag, install_hint).
_PREREQUISITES = [
    ("python3", "--version", "https://python.org or `brew install python@3.12`"),
    ("git", "--version", "https://git-scm.com or `brew install git`"),
]

# Optional but recommended tools.
_OPTIONAL = [
    ("uv", "--version", "`curl -LsSf https://astral.sh/uv/install.sh | sh`"),
    ("claude", "--version", "`npm install -g @anthropic-ai/claude-code`"),
]


# --------------------------------------------------------------------------- #
# Prerequisite checks                                                         #
# --------------------------------------------------------------------------- #

def _check_tool(binary: str, flag: str) -> str | None:
    """Return the version string or None if the tool is not found."""
    path = shutil.which(binary)
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, flag], capture_output=True, text=True, timeout=10,
        )
        return (out.stdout.strip() or out.stderr.strip())[:120]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def check_prerequisites() -> tuple[list[str], list[str]]:
    """Check required and optional tools. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    for binary, flag, hint in _PREREQUISITES:
        ver = _check_tool(binary, flag)
        if ver:
            console.print(f"  [green]✓[/] {binary}: {ver}")
        else:
            errors.append(f"{binary} not found — install: {hint}")
            console.print(f"  [red]✗[/] {binary}: not found — {hint}")

    for binary, flag, hint in _OPTIONAL:
        ver = _check_tool(binary, flag)
        if ver:
            console.print(f"  [green]✓[/] {binary}: {ver}")
        else:
            warnings.append(f"{binary} not found (optional) — {hint}")
            console.print(f"  [yellow]~[/] {binary}: not found (optional) — {hint}")

    return errors, warnings


# --------------------------------------------------------------------------- #
# Directory + .env setup                                                      #
# --------------------------------------------------------------------------- #

def ensure_home_dir() -> bool:
    """Ensure ``~/.no_human/`` exists and grants no group or other access.

    Does NOT force 0700: owner bits are left as the operator set them (a 0500
    lockdown stays 0500). Raises if the result is unusable, rather than
    widening it. Returns True if the directory was created.
    """
    # Delegates to config.ensure_private_dir so ONE implementation governs
    # every writer. The old `!= 0o700` here re-OPENED a directory the operator
    # had locked down further (0500 -> 0700), contradicting the invariant the
    # shared helper documents; it only ever needs to clear group/other bits.
    from ..config import ensure_private_dir

    created = not NO_HUMAN_HOME.exists()
    ensure_private_dir(NO_HUMAN_HOME)
    # The shared helper deliberately does NOT restore owner bits — clearing
    # group/other is the security goal, and widening a directory the operator
    # locked down is not ours to do. But `nh init`'s whole contract is "make my
    # install work", and without owner-write the very next step died with a raw
    # PermissionError three calls downstream. Say so here instead.
    if not os.access(NO_HUMAN_HOME, os.W_OK | os.X_OK):
        mode = format(NO_HUMAN_HOME.stat().st_mode & 0o777, "04o")
        raise click.ClickException(
            f"{NO_HUMAN_HOME} is mode {mode}: no_human cannot write into it. "
            f"Run `chmod u+rwx {NO_HUMAN_HOME}` and re-run `nh init` (if it "
            f"sits on a read-only mount, move NO_HUMAN_HOME instead). "
            f"(Permissions are left as you set them — only group/other access "
            f"is stripped.)")
    return created


def _env_has_key(key: str) -> bool:
    """Check if a key with a value exists in ``~/.no_human/.env``.

    Uses config's parser so this reader cannot disagree with the writer.
    """
    from ..config import _read_env_file

    return bool(_read_env_file(ENV_PATH).get(key))


def _metered_key_in_env() -> str | None:
    """Return the name of the first metered-auth var found in the environment."""
    for var in METERED_AUTH_VARS:
        if os.environ.get(var):
            return var
    return None


def setup_token() -> tuple[bool, str]:
    """Guide the user through billing setup. Returns ``(ready, auth_mode)``.

    Two sanctioned paths: a Claude **subscription** (OAuth token — the default,
    CLAUDE.md #1) or the user's **own Anthropic API key** (BYO-API-key, metered
    and billed to them — for friends/commercial installs). The chosen mode is
    persisted as ``llm.auth_mode`` by :func:`ensure_config`.
    """
    # A re-run on a working install must not re-interrogate the operator: if a
    # mode is already configured and its credential is present, respect it.
    configured = _configured_auth_mode()
    if configured == "api_key" and (
        _env_has_key(API_KEY_VAR) or os.environ.get(API_KEY_VAR)
    ):
        console.print(f"  [green]✓[/] {API_KEY_VAR} found (auth_mode: api_key)")
        return True, "api_key"
    if configured == "subscription" and (
        _env_has_key(SUBSCRIPTION_TOKEN_VAR) or os.environ.get(SUBSCRIPTION_TOKEN_VAR)
    ):
        console.print(
            f"  [green]✓[/] {SUBSCRIPTION_TOKEN_VAR} found (auth_mode: subscription)"
        )
        return True, "subscription"

    console.print(
        "  How will this install pay for Claude?\n"
        "    [bold]1[/] Claude subscription  (OAuth token — recommended)\n"
        "    [bold]2[/] Your own Anthropic API key  (metered, billed to you)"
    )
    choice = click.prompt(
        "    Choice", type=click.Choice(["1", "2"]), default="1", show_default=True
    )
    if choice == "2":
        return _setup_api_key(), "api_key"
    return _setup_subscription_token(), "subscription"


def _configured_auth_mode() -> str:
    """Read ``llm.auth_mode`` from config.yaml if it exists, else "subscription".

    Best-effort — a malformed config never crashes setup; it just falls back to
    the default mode and re-prompts.
    """
    try:
        if CONFIG_PATH.exists():
            data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
            mode = (data.get("llm") or {}).get("auth_mode")
            if mode in ("subscription", "api_key"):
                return mode
    except Exception:  # noqa: BLE001
        pass
    return "subscription"


def _setup_api_key() -> bool:
    """BYO-API-key path: detect or prompt for ANTHROPIC_API_KEY. True if ready."""
    if _env_has_key(API_KEY_VAR):
        console.print(f"  [green]✓[/] {API_KEY_VAR} found in ~/.no_human/.env")
        return True
    if os.environ.get(API_KEY_VAR):
        console.print(f"  [green]✓[/] {API_KEY_VAR} found in environment")
        if click.confirm("    Persist it to ~/.no_human/.env?", default=True):
            _append_env(API_KEY_VAR, os.environ[API_KEY_VAR])
            console.print("    [green]saved[/] to ~/.no_human/.env")
        return True
    console.print(
        "\n  [yellow]✗ no Anthropic API key found.[/]\n"
        "    Create one at: [bold]https://console.anthropic.com/settings/keys[/]\n"
        "    This bills your own metered account. Paste it here, or press Enter "
        "to skip."
    )
    key = click.prompt(
        "    API key", default="", show_default=False, hide_input=True
    ).strip()
    if key:
        _append_env(API_KEY_VAR, key)
        os.environ[API_KEY_VAR] = key
        console.print("    [green]saved[/] to ~/.no_human/.env")
        return True
    console.print(
        "    [yellow]skipped[/] — add it later:\n"
        f"      echo '{API_KEY_VAR}=<your_key>' >> ~/.no_human/.env"
    )
    return False


def _setup_subscription_token() -> bool:
    """Guide the user through subscription token setup. Returns True if ready."""
    # Check .env file first.
    if _env_has_key(SUBSCRIPTION_TOKEN_VAR):
        console.print(f"  [green]✓[/] {SUBSCRIPTION_TOKEN_VAR} found in ~/.no_human/.env")
        return True

    # Check process environment.
    if os.environ.get(SUBSCRIPTION_TOKEN_VAR):
        console.print(f"  [green]✓[/] {SUBSCRIPTION_TOKEN_VAR} found in environment")
        if click.confirm("    Persist it to ~/.no_human/.env?", default=True):
            _append_env(SUBSCRIPTION_TOKEN_VAR, os.environ[SUBSCRIPTION_TOKEN_VAR])
            console.print("    [green]saved[/] to ~/.no_human/.env")
        return True

    # Check for the dangerous metered key.
    metered = _metered_key_in_env()
    if metered:
        console.print(
            f"\n  [bold red]⚠ {metered} is set in your environment.[/]\n"
            f"    In the default subscription mode, startup scrubs this variable\n"
            f"    so a run bills exactly one path. Please unset it:\n"
            f"      [bold]unset {metered}[/]\n"
            f"    (To bill your own Anthropic account with an API key, set\n"
            f"    llm.auth_mode: api_key in config.yaml.)"
        )

    # Guide through setup.
    console.print(
        f"\n  [yellow]✗ {SUBSCRIPTION_TOKEN_VAR} not found.[/]\n"
        f"    You need a Claude subscription token. To create one:\n"
        f"      [bold]claude setup-token[/]\n"
        f"    Then paste the token here, or press Enter to skip for now."
    )
    token = click.prompt("    Token", default="", show_default=False).strip()
    if token:
        _append_env(SUBSCRIPTION_TOKEN_VAR, token)
        os.environ[SUBSCRIPTION_TOKEN_VAR] = token
        console.print("    [green]saved[/] to ~/.no_human/.env")
        return True
    console.print(
        "    [yellow]skipped[/] — add it later:\n"
        f"      echo '{SUBSCRIPTION_TOKEN_VAR}=<your_token>' >> ~/.no_human/.env"
    )
    return False


def _append_env(key: str, value: str) -> None:
    """Upsert a key=value into ``~/.no_human/.env`` (create if absent, 0600).

    Delegates to ``config.upsert_env_var`` rather than doing its own
    read-modify-write. This used to be a THIRD independent writer: it parsed
    with ``splitlines()`` while validating nothing, so the writer and the
    reader disagreed about what a line is — the same defect that let a value
    forge an ``ANTHROPIC_API_KEY=`` entry. It also did ``write_text`` then
    ``chmod``, which is exactly the umask window ``atomic_write_0600`` exists
    to close.
    """
    from ..config import upsert_env_var

    ensure_home_dir()
    upsert_env_var(ENV_PATH, key, value)




# --------------------------------------------------------------------------- #
# Config generation                                                           #
# --------------------------------------------------------------------------- #

def ensure_config(auth_mode: str = "subscription") -> bool:
    """Generate ``~/.no_human/config.yaml`` with git identity if absent, and
    persist the chosen ``llm.auth_mode`` (idempotent even when the file exists).
    Returns True if file was created, False if it already existed."""
    if CONFIG_PATH.exists():
        console.print(f"  [green]✓[/] config.yaml already exists")
        # Persist the chosen billing mode whenever it differs from what is
        # stored, in BOTH directions (api_key <-> subscription), so a re-run
        # of `nh init` that switches modes actually takes effect. Leave the
        # file untouched when the mode is unchanged.
        if auth_mode != _configured_auth_mode():
            _set_auth_mode(auth_mode)
            console.print(f"    set llm.auth_mode: {auth_mode}")
        return False

    # Pre-populate git identity from the user's git config.
    git_name = _git_config("user.name")
    git_email = _git_config("user.email")

    config = load_config(create_if_missing=True)
    # Re-read to update git identity and/or billing mode.
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    if git_name or git_email:
        git_section = data.setdefault("git", {})
        if git_name:
            git_section.setdefault("agent_identity_name", "no_human")
        if git_email:
            git_section.setdefault("agent_identity_email", "no-human@acme.com")
    if auth_mode != "subscription":
        data.setdefault("llm", {})["auth_mode"] = auth_mode
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False))

    console.print(f"  [green]✓[/] created config.yaml")
    if auth_mode != "subscription":
        console.print(f"    billing mode: {auth_mode}")
    if git_name:
        console.print(f"    your git identity: {git_name} <{git_email or '?'}>")
        console.print(f"    agent identity:    no_human <no-human@acme.com>")
    return True


def _set_auth_mode(auth_mode: str) -> None:
    """Upsert ``llm.auth_mode`` into an existing config.yaml, preserving the
    rest. Only the mode goes in config; the credential itself lives in .env."""
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    data.setdefault("llm", {})["auth_mode"] = auth_mode
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False))


def _git_config(key: str) -> str:
    """Read a git config value, or return empty string."""
    try:
        out = subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


# --------------------------------------------------------------------------- #
# Repo onboard offer                                                          #
# --------------------------------------------------------------------------- #

def offer_onboard() -> str | None:
    """Offer to onboard a repo. Returns the repo path or None if skipped."""
    console.print()
    if not click.confirm("  Onboard a repo now?", default=True):
        return None
    repo = click.prompt("  Repo path", default=".", show_default=True).strip()
    repo_path = str(Path(repo).expanduser().resolve())
    if not Path(repo_path).is_dir():
        print_path_error(console, "  [red]not a directory:[/]", repo_path)
        return None
    if not (Path(repo_path) / ".git").exists():
        print_path_error(console, "  [red]not a git repo:[/]", repo_path)
        return None
    return repo_path


# --------------------------------------------------------------------------- #
# Summary card                                                                #
# --------------------------------------------------------------------------- #

def print_summary(*, token_ready: bool, config_path: Path, repo_path: str | None):
    """Print the final summary card."""
    token_status = "[green]✓ ready[/]" if token_ready else "[yellow]✗ not set[/]"
    repo_line = f"  Repo:        {repo_path}" if repo_path else "  Repo:        [dim](none onboarded)[/]"

    card = (
        f"  Config:      {config_path}\n"
        f"  Secrets:     {ENV_PATH} [dim](chmod 600)[/]\n"
        f"  Database:    {DB_PATH}\n"
        f"  Auth:        {token_status}\n"
        f"{repo_line}\n"
        f"\n"
        f"  [bold]Quick start:[/]\n"
        f'    nh task add --title "Fix bug X" --repo ~/my-repo\n'
        f"    nh task add https://github.com/owner/repo/issues/42 --repo ~/my-repo\n"
        f"    nh blocked                  [dim]# check stuck tasks[/]\n"
        f"    nh dashboard                [dim]# open the web board[/]\n"
    )
    if not token_ready:
        card += (
            f"\n  [yellow]⚠ Token not set. Before running tasks:[/]\n"
            f"    [bold]claude setup-token[/]\n"
            f"    [bold]echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> ~/.no_human/.env[/]"
        )

    console.print(Panel(card, title="[bold green]no_human initialized[/]", border_style="green"))
