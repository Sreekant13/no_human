"""VCS facade: branch/commit/push + open-PR dispatch. The agent never merges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import github, gitlab
from .git import CommitResult, GitError, GitRepo, ProtectedBranch

__all__ = [
    "GitRepo",
    "GitError",
    "ProtectedBranch",
    "CommitResult",
    "PrResult",
    "open_pr",
]


@dataclass
class PrResult:
    url: str
    kind: str  # github | gitlab | local
    branch: str


def open_pr(
    repo: GitRepo,
    branch: str,
    title: str,
    body: str,
    *,
    base: str = "main",
    github_hosts: list[str] | None = None,
    labels: list[str] | None = None,
) -> PrResult:
    """Push the branch and open a PR/MR against the detected remote.

    ``github_hosts`` lists extra GitHub Enterprise hosts (from ``git.github_hosts``)
    so a GHE remote like code.example.com is recognized as GitHub.

    ``labels`` are attached when the PR/MR is created. Some CI jobs validate
    labels on PR-open (e.g. metrics-core requires a ``V*`` version label), so applying
    them after the fact would race the first CI run.

    For a local bare-repo remote (Phase 0 testing target) there is no PR API, so
    we push the branch and return a ``local`` marker — the push itself proves the
    branch/commit/PR-open code path without touching a real forge.
    """
    repo.push(branch)
    url = repo.remote_url() or ""

    if github.is_github_remote(url, github_hosts or []):
        return PrResult(github.open_pr(repo.path, branch, title, body, base=base,
                                       labels=labels),
                        "github", branch)
    if gitlab.is_gitlab_remote(url):
        return PrResult(gitlab.open_mr(repo.path, branch, title, body, base=base,
                                       labels=labels),
                        "gitlab", branch)

    # Local / unknown remote: branch is pushed, no forge PR to open.
    marker = f"local-pr://{Path(url).name or 'remote'}/{branch}"
    return PrResult(marker, "local", branch)
