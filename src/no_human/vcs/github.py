"""Open a GitHub PR via the `gh` CLI. Never merges (§3.2)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def is_github_remote(url: str) -> bool:
    return "github.com" in url


def open_pr(
    repo_path: Path, branch: str, title: str, body: str, *, base: str = "main"
) -> str:
    """Create a draft PR and return its URL. Requires `gh` auth."""
    proc = subprocess.run(
        [
            "gh", "pr", "create",
            "--head", branch,
            "--base", base,
            "--title", title,
            "--body", body,
            "--draft",
        ],
        cwd=repo_path, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {proc.stderr.strip()}")
    return proc.stdout.strip()
