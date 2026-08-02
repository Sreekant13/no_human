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
    # The SHA `repo.push()` actually sent, captured at push time — the receipt
    # check must compare the forge's PR head against THIS, never against a
    # HEAD re-resolved later (HEAD can drift while a long-running task waits
    # on CI/review). Empty when a caller fabricates a PrResult without a real
    # push (tests); receipts.py treats a falsy local_sha as "skip the check".
    pushed_sha: str = ""


def open_pr(
    repo: GitRepo,
    branch: str,
    title: str,
    body: str,
    *,
    base: str = "main",
    github_hosts: list[str] | None = None,
    labels: list[str] | None = None,
    update_existing_body: bool = False,
) -> PrResult:
    """Push the branch and open a PR/MR against the detected remote.

    ``github_hosts`` lists extra GitHub Enterprise hosts (from ``git.github_hosts``)
    so a GHE remote like code.example.com is recognized as GitHub.

    ``labels`` are attached when the PR/MR is created. Some CI jobs validate
    labels on PR-open (a repo can require a release-version label), so applying
    them after the fact would race the first CI run.

    For a local bare-repo remote (Phase 0 testing target) there is no PR API, so
    we push the branch and return a ``local`` marker — the push itself proves the
    branch/commit/PR-open code path without touching a real forge.
    """
    # Classify the remote BEFORE pushing. The old order pushed first and gave
    # any unrecognized https host a fake `local-pr://` marker afterwards — the
    # branch landed on a forge we couldn't even name, and the task reported
    # success with a PR URL that opens nothing.
    url = repo.remote_url() or ""
    is_github = github.is_github_remote(url, github_hosts or [])
    is_gitlab = gitlab.is_gitlab_remote(url)
    if not is_github and not is_gitlab and url.startswith(("http://", "https://", "git@")):
        raise RuntimeError(
            f"remote host not recognized as GitHub or GitLab: {url!r} — "
            "refusing to push. Add the host to git.github_hosts if it is a "
            "GitHub Enterprise instance."
        )

    pushed_sha = repo.push(branch)
    if is_github:
        return PrResult(github.open_pr(repo.path, branch, title, body, base=base,
                                       labels=labels,
                                       update_existing_body=update_existing_body),
                        "github", branch, pushed_sha=pushed_sha)
    if is_gitlab:
        return PrResult(gitlab.open_mr(repo.path, branch, title, body, base=base,
                                       labels=labels),
                        "gitlab", branch, pushed_sha=pushed_sha)

    # Local (file-path) remote — the Phase 0 testing target: no PR API, the
    # push itself proves the branch/commit/PR-open path.
    marker = f"local-pr://{Path(url).name or 'remote'}/{branch}"
    return PrResult(marker, "local", branch, pushed_sha=pushed_sha)
