"""Open a GitLab MR via the `glab` CLI. Never merges (§3.2)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ._labels import is_label_error, label_args

log = logging.getLogger("no_human.vcs")


def is_gitlab_remote(url: str) -> bool:
    return "gitlab" in url


def open_mr(
    repo_path: Path, branch: str, title: str, body: str, *, base: str = "main",
    labels: list[str] | None = None,
) -> str:
    """Create an MR and return its URL. Requires `glab` auth.

    Note: explicitly no `--merge-when-pipeline-succeeds` — the agent never
    enables auto-merge. A configured label that doesn't exist on this project
    is retried without labels (logged) rather than stranding the task; a retry
    that still fails is surfaced.
    """
    def _create(with_labels: bool) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "glab", "mr", "create",
                "--source-branch", branch,
                "--target-branch", base,
                "--title", title,
                "--description", body,
                "--no-merge",
                "--yes",
                *(label_args(labels) if with_labels else []),
            ],
            cwd=repo_path, capture_output=True, text=True,
        )

    proc = _create(with_labels=True)
    if proc.returncode == 0:
        return proc.stdout.strip()
    stderr = proc.stderr.strip()
    if labels and is_label_error(stderr):
        log.warning("glab rejected label(s) %s for %s (%s); opening MR without them",
                    labels, repo_path, stderr)
        proc = _create(with_labels=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        stderr = proc.stderr.strip()
    raise RuntimeError(f"glab mr create failed: {stderr}")
