"""Open a GitLab MR via the `glab` CLI. Never merges (§3.2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ._labels import label_args


def is_gitlab_remote(url: str) -> bool:
    return "gitlab" in url


def open_mr(
    repo_path: Path, branch: str, title: str, body: str, *, base: str = "main",
    labels: list[str] | None = None,
) -> str:
    """Create an MR and return its URL. Requires `glab` auth.

    Note: explicitly no `--merge-when-pipeline-succeeds` — the agent never
    enables auto-merge.
    """
    proc = subprocess.run(
        [
            "glab", "mr", "create",
            "--source-branch", branch,
            "--target-branch", base,
            "--title", title,
            "--description", body,
            "--no-merge",
            "--yes",
            *label_args(labels),
        ],
        cwd=repo_path, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"glab mr create failed: {proc.stderr.strip()}")
    return proc.stdout.strip()
