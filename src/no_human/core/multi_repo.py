"""Multi-repo task orchestration (Phase D — WS-E).

When a task declares ``linked_repos``, the implementation spans multiple
repositories.  Each repo gets its own branch/worktree and PR.  The task
reaches ``AWAITING_APPROVAL`` only when ALL repos have PRs open.

The orchestrator runs the agent once per repo (in declared order) with cross-
repo context injected into the prompt so it can see files from all repos.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .task import Task, TaskStatus

log = logging.getLogger("no_human.multi_repo")


@dataclass
class RepoResult:
    """Outcome of implementing in a single repo."""
    repo_path: str
    branch: str = ""
    pr_url: str = ""
    status: str = "pending"     # pending | succeeded | failed
    detail: str = ""
    files_changed: int = 0


@dataclass
class MultiRepoOutcome:
    """Aggregated outcome for a multi-repo task."""
    task: Task
    results: list[RepoResult] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return all(r.status == "succeeded" for r in self.results)

    @property
    def any_failed(self) -> bool:
        return any(r.status == "failed" for r in self.results)

    @property
    def pr_urls(self) -> list[str]:
        return [r.pr_url for r in self.results if r.pr_url]

    def summary(self) -> str:
        lines = [f"Multi-repo task: {self.task.title}"]
        for r in self.results:
            icon = "✅" if r.status == "succeeded" else "❌" if r.status == "failed" else "⏳"
            lines.append(f"  {icon} {r.repo_path}: {r.status}")
            if r.pr_url:
                lines.append(f"      PR: {r.pr_url}")
        return "\n".join(lines)


def all_repo_paths(task: Task) -> list[str]:
    """Return all repo paths for a task (primary + linked), in order."""
    paths: list[str] = []
    if task.repo_path:
        paths.append(task.repo_path)
    for p in task.linked_repos:
        if p and p not in paths:
            paths.append(p)
    return paths


def cross_repo_context(task: Task, current_repo: str) -> str:
    """Build a context section describing all repos in a multi-repo task.

    Injected into the agent prompt so it knows about the other repos.
    """
    repos = all_repo_paths(task)
    if len(repos) <= 1:
        return ""

    lines = [
        "This is a MULTI-REPO task spanning these repositories:",
    ]
    for i, r in enumerate(repos, 1):
        marker = " ← (current)" if r == current_repo else ""
        lines.append(f"  {i}. {r}{marker}")
    lines.append("")
    lines.append(
        "Changes may need coordination across repos. The primary repo "
        "is listed first; linked repos follow in declared order."
    )
    if current_repo != repos[0]:
        lines.append(
            f"\nYou are currently working in a LINKED repo ({current_repo}). "
            f"The primary repo is {repos[0]}. Check its recent changes for context."
        )
    return "\n".join(lines)


def linked_repos_block(task: Task) -> str:
    """Path map of the task's linked repos, for the read-only planner (D19).

    The planner explores with ``cwd`` set to the primary repo and was never told
    the linked repos exist, so it planned around them — assuming files it could
    have read were "not on disk".  Returns "" for single-repo tasks, leaving
    their prompt byte-identical.
    """
    repos = all_repo_paths(task)
    linked = repos[1:] if task.repo_path else repos
    if not linked:
        return ""
    lines = ["LINKED REPOSITORIES (checked out on this machine — read them):"]
    lines += [f"  - {p}" for p in linked]
    lines.append("")
    lines.append(
        "They are part of this task's context. Read them with the same tools you "
        "use on the primary repo, by absolute path. Never assume a linked repo is "
        "absent or unreadable — open it and look. If the change spans one, name "
        "its files by absolute path in FILES TO CHANGE/CREATE."
    )
    return "\n".join(lines) + "\n\n"


def validate_linked_repos(task: Task) -> list[str]:
    """Validate that all linked repos exist. Returns list of errors."""
    errors: list[str] = []
    for repo in all_repo_paths(task):
        p = Path(repo)
        if not p.exists():
            errors.append(f"repo not found: {repo}")
        elif not (p / ".git").exists() and not p.is_file():
            errors.append(f"not a git repo: {repo}")
    return errors


def store_multi_repo_outcome(task: Task, outcome: MultiRepoOutcome) -> None:
    """Store the multi-repo outcome in the task's context for later display."""
    ctx = task.context or {}
    ctx["multi_repo_results"] = [
        {
            "repo_path": r.repo_path,
            "branch": r.branch,
            "pr_url": r.pr_url,
            "status": r.status,
            "detail": r.detail,
            "files_changed": r.files_changed,
        }
        for r in outcome.results
    ]
    ctx["multi_repo_pr_urls"] = outcome.pr_urls
    task.context = ctx
