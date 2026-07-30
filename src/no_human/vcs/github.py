"""Open a GitHub PR via the `gh` CLI. Never merges (§3.2)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ._labels import is_label_error, label_args

log = logging.getLogger("no_human.vcs")


def is_github_remote(url: str, extra_hosts: tuple[str, ...] | list[str] = ()) -> bool:
    """True for github.com or any configured GitHub Enterprise host.

    GHE hosts (e.g. ``code.example.com``) don't contain "github.com", so they
    must be listed explicitly via ``git.github_hosts`` in config. ``gh`` infers
    the host from the repo remote, so the same `gh pr create` path works.
    """
    if "github.com" in url:
        return True
    return any(h and h in url for h in extra_hosts)


def open_pr(
    repo_path: Path, branch: str, title: str, body: str, *, base: str = "main",
    labels: list[str] | None = None, update_existing_body: bool = False,
) -> str:
    """Create a draft PR and return its URL. Requires `gh` auth.

    Idempotent: if a PR already exists for ``branch`` (e.g. when revising after a
    human PR comment, which pushes onto the same branch), return the existing PR's
    URL instead of failing — the push has already updated it. Never merges.

    ``labels`` are applied at creation time, not after, so a CI job that validates
    labels on PR-open never observes an unlabelled PR. A label that doesn't exist
    on THIS repo (e.g. metrics-core's ``V17`` applied to a repo that never defined it)
    must not block the PR: `gh` fails the whole create, so we retry once without
    labels and open the PR unlabelled (logged). A retry that still fails is a
    real error and is surfaced.
    """
    def _create(with_labels: bool) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "gh", "pr", "create",
                "--head", branch,
                "--base", base,
                "--title", title,
                "--body", body,
                "--draft",
                *(label_args(labels) if with_labels else []),
            ],
            cwd=repo_path, capture_output=True, text=True,
        )

    proc = _create(with_labels=True)
    if proc.returncode == 0:
        return proc.stdout.strip()

    stderr = proc.stderr.strip()
    # A nonexistent label shouldn't strand the task — open the PR without it.
    if labels and is_label_error(stderr):
        log.warning("gh rejected label(s) %s for %s (%s); opening PR without them",
                    labels, repo_path, stderr)
        proc = _create(with_labels=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        stderr = proc.stderr.strip()
    if "already exists" in stderr.lower():
        existing = _existing_pr_url(repo_path, branch)
        if existing:
            # 🔴 APPLY THE NEW BODY. This path used to return the URL and silently drop
            # it, which was harmless while the only caller was `_finalize` — but 0a opens
            # a DRAFT before the review gate, so the first body is the pre-review one
            # (no test evidence, no review evidence) and `_finalize`'s richer body was
            # being discarded. A review caught it: the human-visible PR permanently lost
            # its "## Review evidence" and "## Test evidence" sections, which regresses
            # W1.6/M-B and defeats 0a's own purpose. The comment claiming "_finalize's
            # later call UPDATES the same PR" was simply false — nothing in src/ ran
            # `gh pr edit` at all.
            #
            # 🔴 OPT-IN ONLY. My first version edited unconditionally, which also hit the
            # REVISION flow (a task resuming onto an existing PR branch) and would have
            # OVERWRITTEN a description a human had edited — behaviour main never had. A
            # review caught it. Only the run that opened the draft itself may rewrite the
            # body, so `_finalize` passes True and nothing else does.
            #
            # Best-effort: a failed body update must not fail a PR that exists. The URL is
            # the delivery; the body is evidence, and losing it loudly beats escalating a
            # delivered change.
            if not update_existing_body:
                return existing
            edit = subprocess.run(
                ["gh", "pr", "edit", existing, "--body", body],
                cwd=repo_path, capture_output=True, text=True,
            )
            if edit.returncode != 0:
                log.warning("gh pr edit failed for %s (%s); PR keeps its earlier body",
                            existing, edit.stderr.strip())
            return existing
    raise RuntimeError(f"gh pr create failed: {stderr}")


def _existing_pr_url(repo_path: Path, branch: str) -> str | None:
    """Return the URL of the open PR for ``branch``, or None."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "url", "--jq", ".[0].url"],
        cwd=repo_path, capture_output=True, text=True,
    )
    url = proc.stdout.strip()
    return url or None
