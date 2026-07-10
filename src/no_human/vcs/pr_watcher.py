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
import os
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

# Invisible HTML comment stamped on every PR comment no_human posts. The
# forge renders it as nothing, and copy-pasting the rendered comment does not
# carry it — so a human quoting agent output is never misclassified. Load-
# bearing: comments are posted under the operator's own gh login, so author
# identity CANNOT distinguish the product's comments from the human's — the
# 2026-07-10 incident (the CI_GATE results comment resumed its own task into
# the budget gate) is exactly this gap.
AGENT_COMMENT_MARKER = "<!-- no_human-agent-comment -->"


def is_agent_comment(body: str | None) -> bool:
    """True if a PR comment body was authored by no_human itself."""
    return bool(body) and AGENT_COMMENT_MARKER in body


async def post_reply_comment(pr_ref: str, message: str) -> bool:
    """Post a reply comment on a PR/MR after addressing feedback.

    Every body is stamped with AGENT_COMMENT_MARKER so the wake watcher can
    recognize the product's own comments (see marker docstring).
    Returns True on success.
    """
    message = f"{AGENT_COMMENT_MARKER}\n{message}"
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


async def default_pr_state(ref: str) -> str:
    """The PR's lifecycle state via gh: "MERGED" | "CLOSED" | "OPEN" | "".

    "" means unknown (no gh, unparseable ref, network error) — callers must
    treat unknown as "no action", never as closed. An awaiting-approval task
    previously watched only comments, so a merged PR left it parked forever
    and a closed-unmerged PR was polled until the end of time.
    """
    if not shutil.which("gh"):
        return ""
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed or parsed[0] != "github":
            return ""
        _, host, slug, num = parsed
        repo_arg, num_str = f"{host}/{slug}", str(num)
    elif "#" in ref:
        repo, _, num_str = ref.partition("#")
        repo_arg = repo
    else:
        return ""
    out = await _run_cli(
        ["gh", "pr", "view", num_str, "--repo", repo_arg, "--json", "state"]
    )
    if not out:
        return ""
    try:
        return str(json.loads(out).get("state") or "").upper()
    except json.JSONDecodeError:
        return ""


async def default_pr_checks(ref: str) -> list[dict]:
    """The PR head's CI checks, normalized: [{name, status, link}].

    status ∈ "fail" | "pass" | "pending". Sources both GitHub check-runs
    (conclusion) and commit statuses (state) from statusCheckRollup — the
    Jenkins integration on code.example.com reports plain commit statuses
    (e.g. continuous-integration/jenkins/pr-head), which `gh pr checks`
    renders but scripts often miss. Empty list = unknown/no checks.
    """
    if not shutil.which("gh"):
        return []
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed or parsed[0] != "github":
            return []
        _, host, slug, num = parsed
        repo_arg, num_str = f"{host}/{slug}", str(num)
    elif "#" in ref:
        repo, _, num_str = ref.partition("#")
        repo_arg = repo
    else:
        return []
    out = await _run_cli([
        "gh", "pr", "view", num_str, "--repo", repo_arg,
        "--json", "statusCheckRollup",
    ])
    if not out:
        return []
    try:
        rollup = json.loads(out).get("statusCheckRollup") or []
    except json.JSONDecodeError:
        return []
    checks: list[dict] = []
    for c in rollup:
        name = c.get("name") or c.get("context") or "unnamed check"
        raw = (c.get("conclusion") or c.get("state") or "").upper()
        if raw in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            status = "fail"
        elif raw in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            status = "pass"
        else:  # PENDING, EXPECTED, IN_PROGRESS, QUEUED, "" (still running)
            status = "pending"
        checks.append({
            "name": name, "status": status,
            "link": c.get("targetUrl") or c.get("detailsUrl") or "",
        })
    return checks


def _gh_repo_and_number(ref: str) -> tuple[str, str] | None:
    """Normalize a PR ref (URL or ``owner/repo#n``) to gh's (repo_arg, number).

    GitHub/GHE only — returns None for GitLab or unparseable refs.
    """
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed or parsed[0] != "github":
            return None
        _, host, slug, num = parsed
        return f"{host}/{slug}", str(num)
    if "#" in ref:
        repo, _, num_str = ref.partition("#")
        return repo, num_str
    return None


async def default_pr_head(ref: str) -> str:
    """The PR's current head commit SHA, or "" (unknown must never look like
    a real head — the CI_GATE gate keys its once-per-head guard on this)."""
    if not shutil.which("gh"):
        return ""
    target = _gh_repo_and_number(ref)
    if not target:
        return ""
    repo_arg, num_str = target
    out = await _run_cli(
        ["gh", "pr", "view", num_str, "--repo", repo_arg, "--json", "headRefOid"]
    )
    if not out:
        return ""
    try:
        return str(json.loads(out).get("headRefOid") or "")
    except json.JSONDecodeError:
        return ""


async def default_pr_files(ref: str) -> list[str]:
    """Changed file paths of the PR, or [] (unknown). The CI_GATE gate treats
    an empty list as unclassifiable and refuses latest_dev images for it only
    when runtime code might be touched — callers decide."""
    if not shutil.which("gh"):
        return []
    target = _gh_repo_and_number(ref)
    if not target:
        return []
    repo_arg, num_str = target
    out = await _run_cli(
        ["gh", "pr", "view", num_str, "--repo", repo_arg, "--json", "files"]
    )
    if not out:
        return []
    try:
        files = json.loads(out).get("files") or []
        return [f.get("path", "") for f in files if f.get("path")]
    except json.JSONDecodeError:
        return []


async def default_ci_log_excerpt(link: str) -> str:
    """A short excerpt of a failing Jenkins build's console log, or "".

    Proven against build.example.com: `<build>/consoleText` answers HTTP Basic
    with the SSO credentials from ~/.no_human/.env (the browser URL the human
    sees is an SSO redirect, but the API path takes Basic auth). The corporate
    chain is self-signed, so verification is disabled for this host — the
    excerpt feeds a prompt, it is not an integrity boundary. Best-effort by
    design: "" simply means the feedback carries only the check name + link.
    """
    if "/display/redirect" in link:
        link = link.split("/display/redirect")[0]
    if not link.startswith("http"):
        return ""
    # The credentials live in ~/.no_human/.env; the server process does not
    # export them, so reading os.environ alone would always come up empty.
    from ..config import load_env_var

    user = load_env_var("SSO_USERNAME")
    password = load_env_var("SSO_PASSWORD")
    if not (user and password):
        return ""
    import httpx
    try:
        async with httpx.AsyncClient(verify=False, timeout=25, auth=(user, password)) as client:
            resp = await client.get(link.rstrip("/") + "/consoleText")
            if resp.status_code != 200:
                return ""
            text = resp.text
    except Exception as exc:  # noqa: BLE001 — a log fetch must never break the watcher
        log.warning("CI log fetch failed for %s: %s", link[:80], exc)
        return ""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "error" in low or "exception" in low or "failure" in low:
            return "\n".join(lines[max(0, i - 2): i + 18])[:2000]
    return "\n".join(lines[-15:])[:2000]
