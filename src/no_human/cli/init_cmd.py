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

from ..config import (
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
    """Create ``~/.no_human/`` with 700 permissions. Returns True if created."""
    created = False
    if not NO_HUMAN_HOME.exists():
        NO_HUMAN_HOME.mkdir(parents=True, mode=0o700)
        created = True
    else:
        # Fix permissions if too open (e.g. 755 from a careless mkdir).
        current = NO_HUMAN_HOME.stat().st_mode & 0o777
        if current != 0o700:
            NO_HUMAN_HOME.chmod(0o700)
    return created


def _env_has_key(key: str) -> bool:
    """Check if a key with a value exists in ``~/.no_human/.env``."""
    if not ENV_PATH.exists():
        return False
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key and v.strip().strip('"').strip("'"):
            return True
    return False


def _metered_key_in_env() -> str | None:
    """Return the name of the first metered-auth var found in the environment."""
    for var in METERED_AUTH_VARS:
        if os.environ.get(var):
            return var
    return None


def setup_token() -> bool:
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
            f"    no_human runs on subscription auth ONLY — metered keys cause\n"
            f"    silent API billing. Please unset it:\n"
            f"      [bold]unset {metered}[/]"
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
    """Append a key=value to ``~/.no_human/.env`` (create if absent, chmod 600)."""
    ensure_home_dir()
    # Read existing content to avoid duplicates.
    existing_keys: set[str] = set()
    lines: list[str] = []
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, _ = line.partition("=")
                existing_keys.add(k.strip())
            lines.append(raw)
    if key in existing_keys:
        # Replace the existing line.
        new_lines = []
        for raw in lines:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, _ = line.partition("=")
                if k.strip() == key:
                    new_lines.append(f"{key}={value}")
                    continue
            new_lines.append(raw)
        lines = new_lines
    else:
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)


# --------------------------------------------------------------------------- #
# Config generation                                                           #
# --------------------------------------------------------------------------- #

def ensure_config() -> bool:
    """Generate ``~/.no_human/config.yaml`` with git identity if absent.
    Returns True if file was created, False if it already existed."""
    if CONFIG_PATH.exists():
        console.print(f"  [green]✓[/] config.yaml already exists")
        return False

    # Pre-populate git identity from the user's git config.
    git_name = _git_config("user.name")
    git_email = _git_config("user.email")

    config = load_config(create_if_missing=True)
    if git_name or git_email:
        # Re-read to update git identity.
        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        git_section = data.setdefault("git", {})
        if git_name:
            git_section.setdefault("agent_identity_name", "no_human")
        if git_email:
            git_section.setdefault("agent_identity_email", "no-human@acme.com")
        # Also add a comment about the user's identity for reference.
        CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False))

    console.print(f"  [green]✓[/] created config.yaml")
    if git_name:
        console.print(f"    your git identity: {git_name} <{git_email or '?'}>")
        console.print(f"    agent identity:    no_human <no-human@acme.com>")
    return True


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
        console.print(f"  [red]not a directory:[/] {repo_path}")
        return None
    if not (Path(repo_path) / ".git").exists():
        console.print(f"  [red]not a git repo:[/] {repo_path}")
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
