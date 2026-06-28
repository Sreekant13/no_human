"""PR comment watcher — poll open agent PRs for new human comments.

Phase C (WS-C): the only permitted human touchpoints are (1) commenting
on the PR and (2) approving/merging.  This module detects new comments and
feeds them back into the task's ``send_back_feedback`` queue so the next
attempt addresses them.

Supports GitHub (``gh`` CLI), GitLab (``glab`` CLI), and a test-friendly
injectable callable for unit tests.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

log = logging.getLogger("no_human.pr_watcher")


@dataclass
class PrComment:
    """A single human comment on a PR."""
    author: str
    body: str
    path: str | None = None    # file path for inline/line comments
    line: int | None = None
    diff_hunk: str | None = None
    created_at: str = ""


@dataclass
class PrFeedback:
    """Aggregated feedback from all new comments on a PR."""
    pr_url: str
    comments: list[PrComment] = field(default_factory=list)

    def to_send_back_entries(self) -> list[dict[str, Any]]:
        """Convert to the ``send_back_feedback`` format consumed by the orchestrator."""
        entries: list[dict[str, Any]] = []
        for c in self.comments:
            msg = c.body
            if c.path:
                loc = f"{c.path}"
                if c.line:
                    loc += f":{c.line}"
                msg = f"[{loc}] {msg}"
            if c.diff_hunk:
                msg += f"\n\nContext:\n```\n{c.diff_hunk[:500]}\n```"
            entries.append({
                "at": c.created_at or datetime.now(timezone.utc).isoformat(),
                "message": msg,
                "author": c.author,
                "source": "pr_comment",
                "pr_url": self.pr_url,
            })
        return entries


# ---------------------------------------------------------------------------
# GitHub comment fetcher
# ---------------------------------------------------------------------------

async def _run_cli(cmd: list[str]) -> str | None:
    """Run a CLI command; return stdout or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            log.debug("cmd %s failed: %s", cmd[:3], err.decode()[:200])
            return None
        return out.decode()
    except FileNotFoundError:
        log.debug("CLI not found: %s", cmd[0])
        return None


async def fetch_github_pr_comments(
    repo: str, pr_number: int, *, since: str | None = None,
    agent_login: str = "no-human[bot]", host: str | None = None,
) -> list[PrComment]:
    """Fetch PR review comments + issue comments from GitHub via ``gh`` CLI.

    ``since`` is an ISO timestamp; only comments after it are returned.
    Comments authored by ``agent_login`` are excluded (avoid self-loops).
    ``host`` targets a GitHub Enterprise host (e.g. ``code.example.com``); when
    None, ``gh`` uses its default (github.com).
    """
    if not shutil.which("gh"):
        return []

    host_args = ["--hostname", host] if host else []
    comments: list[PrComment] = []

    # 1. Review comments (line-level)
    out = await _run_cli([
        "gh", "api", *host_args,
        f"repos/{repo}/pulls/{pr_number}/comments",
        "--paginate",
    ])
    if out:
        for c in json.loads(out):
            if c.get("user", {}).get("login") == agent_login:
                continue
            created = c.get("created_at", "")
            if since and created <= since:
                continue
            comments.append(PrComment(
                author=c.get("user", {}).get("login", "unknown"),
                body=c.get("body", ""),
                path=c.get("path"),
                line=c.get("original_line") or c.get("line"),
                diff_hunk=c.get("diff_hunk"),
                created_at=created,
            ))

    # 2. Issue comments (general PR comments)
    out = await _run_cli([
        "gh", "api", *host_args,
        f"repos/{repo}/issues/{pr_number}/comments",
        "--paginate",
    ])
    if out:
        for c in json.loads(out):
            if c.get("user", {}).get("login") == agent_login:
                continue
            created = c.get("created_at", "")
            if since and created <= since:
                continue
            comments.append(PrComment(
                author=c.get("user", {}).get("login", "unknown"),
                body=c.get("body", ""),
                created_at=created,
            ))

    return comments


async def fetch_gitlab_mr_comments(
    project_id: str, mr_iid: int, *, since: str | None = None,
    agent_username: str = "no-human-bot",
) -> list[PrComment]:
    """Fetch MR notes from GitLab via ``glab`` CLI.

    ``project_id`` is URL-encoded (e.g. ``group%2Frepo``).
    """
    if not shutil.which("glab"):
        return []

    comments: list[PrComment] = []
    out = await _run_cli([
        "glab", "api",
        f"projects/{project_id}/merge_requests/{mr_iid}/notes",
        "--paginate",
    ])
    if out:
        for c in json.loads(out):
            if c.get("author", {}).get("username") == agent_username:
                continue
            if c.get("system", False):
                continue
            created = c.get("created_at", "")
            if since and created <= since:
                continue
            # GitLab MR notes may have position data for inline comments.
            pos = c.get("position") or {}
            comments.append(PrComment(
                author=c.get("author", {}).get("username", "unknown"),
                body=c.get("body", ""),
                path=pos.get("new_path") or pos.get("old_path"),
                line=pos.get("new_line") or pos.get("old_line"),
                created_at=created,
            ))

    return comments


# ---------------------------------------------------------------------------
# Generic PR comment checker (for WakeWatcher integration)
# ---------------------------------------------------------------------------

# Type alias: takes "owner/repo#123" and returns list of new comments.
PrCommentChecker = Callable[[str], Awaitable[list[PrComment]]]


def parse_pr_url(url: str) -> tuple[str, str, str, int] | None:
    """Parse a PR/MR URL into ``(forge, host, slug, number)``.

    Handles GitHub/GHE (``https://<host>/<owner>/<repo>/pull/<n>``) and GitLab
    (``https://<host>/<group>/<repo>/-/merge_requests/<n>``). Returns None if it
    can't be parsed. The ``host`` lets us target GitHub Enterprise (e.g.
    ``code.example.com``) rather than defaulting to github.com.
    """
    import re
    from urllib.parse import quote
    gh = re.match(r"https?://([^/]+)/(.+?)/pull/(\d+)", url)
    if gh:
        return ("github", gh.group(1), gh.group(2), int(gh.group(3)))
    gl = re.match(r"https?://([^/]+)/(.+?)(?:/-)?/merge_requests/(\d+)", url)
    if gl:
        return ("gitlab", gl.group(1), quote(gl.group(2), safe=""), int(gl.group(3)))
    return None


async def default_pr_merged(ref: str) -> bool:
    """Resolve a ``pr_merged:`` wake condition via gh.

    ``ref`` may be a full PR URL or ``owner/repo#num``. Unknown/unsupported →
    False (never falsely report merged). Used by serve/wake/api watchers.
    """
    if not shutil.which("gh"):
        return False
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed or parsed[0] != "github":
            return False
        _, host, slug, num = parsed
        repo_arg, num_str = f"{host}/{slug}", str(num)  # gh --repo takes [HOST/]OWNER/REPO
    elif "#" in ref:
        repo, _, num_str = ref.partition("#")
        repo_arg = repo
    else:
        return False
    out = await _run_cli(
        ["gh", "pr", "view", num_str, "--repo", repo_arg, "--json", "state"]
    )
    return bool(out) and '"MERGED"' in out


async def check_pr_comments(
    pr_ref: str,
    *,
    since: str | None = None,
) -> list[PrComment]:
    """Check for new comments on a PR/MR.

    ``pr_ref`` may be:
    - a full PR/MR URL (preferred — carries the host for GitHub Enterprise)
    - GitHub short ref ``owner/repo#123``
    - GitLab short ref ``project_id!123`` (using ``!`` for MR)

    Returns new comments since ``since`` (ISO timestamp), or all if None.
    """
    if pr_ref.startswith("http"):
        parsed = parse_pr_url(pr_ref)
        if not parsed:
            log.warning("could not parse PR URL: %s", pr_ref)
            return []
        forge, host, slug, num = parsed
        if forge == "github":
            return await fetch_github_pr_comments(slug, num, since=since, host=host)
        return await fetch_gitlab_mr_comments(slug, num, since=since)

    if "!" in pr_ref:
        # GitLab MR
        project_id, _, iid_str = pr_ref.partition("!")
        try:
            iid = int(iid_str)
        except ValueError:
            log.warning("invalid GitLab MR ref: %s", pr_ref)
            return []
        return await fetch_gitlab_mr_comments(project_id, iid, since=since)

    if "#" in pr_ref:
        # GitHub PR
        repo, _, num_str = pr_ref.partition("#")
        try:
            num = int(num_str)
        except ValueError:
            log.warning("invalid GitHub PR ref: %s", pr_ref)
            return []
        return await fetch_github_pr_comments(repo, num, since=since)

    log.warning("unrecognized PR ref format: %s", pr_ref)
    return []


# ---------------------------------------------------------------------------
# Post reply comment (after agent addresses feedback)
# ---------------------------------------------------------------------------

async def post_reply_comment(pr_ref: str, message: str) -> bool:
    """Post a reply comment on a PR/MR after addressing feedback.

    Returns True on success.
    """
    if "!" in pr_ref:
        project_id, _, iid_str = pr_ref.partition("!")
        out = await _run_cli([
            "glab", "api", "--method", "POST",
            f"projects/{project_id}/merge_requests/{iid_str}/notes",
            "--field", f"body={message}",
        ])
        return out is not None

    if "#" in pr_ref:
        repo, _, num_str = pr_ref.partition("#")
        out = await _run_cli([
            "gh", "pr", "comment", num_str,
            "--repo", repo,
            "--body", message,
        ])
        return out is not None

    return False
