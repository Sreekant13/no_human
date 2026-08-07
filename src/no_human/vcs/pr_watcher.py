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


def gitlab_project_path(project: str) -> str:
    """Percent-encode a GitLab project for the REST API — idempotently.

    GitLab addresses a project as ``group%2Frepo``; a RAW slash splits the URL
    path and 404s. Verified against the live API (2026-08-06)::

        projects/gitlab-org%2Fgitlab-foss  -> HTTP 200
        projects/gitlab-org/gitlab-foss    -> HTTP 404

    Every project has a namespace, so this is the normal case, not an edge one.
    A 404 here is silent — ``_run_cli`` returns None, the state reads "", and a
    ``pr_merged:`` blocker stays parked forever, which is the exact failure the
    GitLab lifecycle support was added to remove.

    Callers arrive in BOTH forms and neither can be made to go away: this
    module's own :func:`parse_pr_url` already encodes, while the ``project!iid``
    short ref — GitLab's own native notation, and the only form a human or an
    LLM writes by hand — carries a raw slash. Decoding before encoding makes the
    function idempotent, so both land on the same argv, and a MIXED ref such as
    ``grp%2Fsub/proj!7`` is normalized rather than half-encoded.

    The round trip cannot lose information: a GitLab path segment is
    ``[A-Za-z0-9_.-]`` joined by ``/``, so a literal ``%`` cannot occur in one
    and can only ever be an escape introducer. ``unquote`` leaves a malformed
    escape untouched, so a stray ``%`` re-encodes to ``%25`` rather than
    silently corrupting the path.
    """
    from urllib.parse import quote, unquote
    return quote(unquote(project), safe="")


async def fetch_gitlab_mr_comments(
    project_id: str, mr_iid: int, *, since: str | None = None,
    agent_username: str = "no-human-bot",
) -> list[PrComment]:
    """Fetch MR notes from GitLab via ``glab`` CLI.

    ``project_id`` may be given either encoded (``group%2Frepo``) or as a plain
    path (``group/repo``); :func:`gitlab_project_path` normalizes it.
    """
    if not shutil.which("glab"):
        return []

    project_id = gitlab_project_path(project_id)
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
    """Resolve a ``pr_merged:`` wake condition — gh for GitHub/GHE, glab for
    GitLab.

    ``ref`` may be a full PR/MR URL, ``owner/repo#num`` (GitHub) or
    ``project!iid`` (GitLab). Unknown/unsupported → False (never falsely report
    merged). Used by serve/wake/api watchers.

    Derived from :func:`default_pr_state` rather than running its own query:
    the two used to be separate `gh` calls that could in principle disagree,
    and every forge added to one had to be remembered in the other. A GitLab MR
    returned False here forever, with no error anywhere, so a task parked on
    ``pr_merged:<MR>`` never woke.
    """
    return (await default_pr_state(ref)) == "MERGED"


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
        project_id = gitlab_project_path(project_id)
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


def _find_marker_id(list_json: str | None, marker: str) -> str | None:
    """First comment/note id whose body contains *marker*, or None."""
    if not list_json:
        return None
    try:
        for c in json.loads(list_json):
            if marker in (c.get("body") or ""):
                return str(c.get("id"))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


async def upsert_agent_comment(pr_ref: str, message: str, key: str = "") -> bool:
    """Post OR update the ONE agent comment scoped by *key* on a PR/MR.

    Re-runs must UPDATE a single comment, not pile up: the CI_GATE gate posting a
    fresh comment on every attempt is how a PR ends up with a wall of
    near-identical comments. *key* (e.g. "ci_gate") scopes the comment so distinct
    report types each keep exactly one, updated in place. Falls back to a plain
    create if listing/patching isn't possible. Never mentions "no_human" in the
    visible body — the marker is an invisible HTML comment.
    """
    submarker = f"<!-- nh:{key} -->" if key else ""
    body = f"{AGENT_COMMENT_MARKER}{submarker}\n{message}"
    find = submarker or AGENT_COMMENT_MARKER

    if "!" in pr_ref:  # GitLab MR: "project!iid"
        project, _, iid = pr_ref.partition("!")
        base = f"projects/{gitlab_project_path(project)}/merge_requests/{iid}/notes"
        nid = _find_marker_id(await _run_cli(["glab", "api", f"{base}?per_page=100"]), find)
        if nid and await _run_cli(["glab", "api", "--method", "PUT",
                                   f"{base}/{nid}", "--field", f"body={body}"]) is not None:
            return True
        return await _run_cli(["glab", "api", "--method", "POST", base,
                               "--field", f"body={body}"]) is not None

    if "#" in pr_ref:  # GitHub/GHE: "host/owner/repo#num"
        repo, _, num = pr_ref.partition("#")
        host, _, slug = repo.partition("/")
        hostarg = ["--hostname", host] if host else []
        listing = await _run_cli(["gh", "api", *hostarg,
                                  f"repos/{slug}/issues/{num}/comments", "--paginate"])
        cid = _find_marker_id(listing, find)
        if cid and await _run_cli(["gh", "api", *hostarg, "-X", "PATCH",
                                   f"repos/{slug}/issues/comments/{cid}",
                                   "-f", f"body={body}"]) is not None:
            return True
        return await _run_cli(["gh", "api", *hostarg, "-X", "POST",
                               f"repos/{slug}/issues/{num}/comments",
                               "-f", f"body={body}"]) is not None

    return False


#: GitLab MR ``state`` → the GitHub vocabulary the rest of this module speaks.
#: Deliberately partial: GitLab also reports ``locked``, which is neither open
#: nor finished, and guessing it into CLOSED would escalate a live MR. Anything
#: not listed falls through to "" — unknown, which callers treat as no action.
_GITLAB_MR_STATE = {"merged": "MERGED", "closed": "CLOSED", "opened": "OPEN"}


async def _gitlab_mr_state(project: str, iid: str, *, host: str = "") -> str:
    """One GitLab MR's lifecycle state via ``glab``, in GitHub's vocabulary.

    ``project`` may be encoded (``group%2Frepo``, what :func:`parse_pr_url`
    produces) or a plain path (``group/repo``, what the ``project!iid`` short
    ref carries); :func:`gitlab_project_path` normalizes both. ``host`` is
    required for self-hosted GitLab: ``glab`` defaults to gitlab.com, which is
    the exact failure ``ci/gitlab.py`` records for ``glab ci run``.
    """
    if not shutil.which("glab"):
        return ""
    project = gitlab_project_path(project)
    hostarg = ["--hostname", host] if host else []
    out = await _run_cli(
        ["glab", "api", *hostarg, f"projects/{project}/merge_requests/{iid}"]
    )
    if not out:
        return ""
    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, dict):
        return ""
    return _GITLAB_MR_STATE.get(str(raw.get("state") or "").lower(), "")


async def default_pr_state(ref: str) -> str:
    """The PR/MR's lifecycle state: "MERGED" | "CLOSED" | "OPEN" | "".

    "" means unknown (no gh/glab, unparseable ref, network error, a state this
    module does not map) — callers must treat unknown as "no action", never as
    closed. An awaiting-approval task previously watched only comments, so a
    merged PR left it parked forever and a closed-unmerged PR was polled until
    the end of time.

    GitHub/GHE goes through ``gh``, GitLab through ``glab``. Before that split
    existed every GitLab ref returned "" here — silently, so an MR that had
    been merged for days looked exactly like a network blip.
    """
    if ref.startswith("http"):
        parsed = parse_pr_url(ref)
        if not parsed:
            return ""
        forge, host, slug, num = parsed
        if forge == "gitlab":
            return await _gitlab_mr_state(slug, str(num), host=host)
        repo_arg, num_str = f"{host}/{slug}", str(num)
    elif "!" in ref:                       # GitLab short ref: "project!iid"
        project, _, num_str = ref.partition("!")
        return await _gitlab_mr_state(project, num_str)
    elif "#" in ref:
        repo, _, num_str = ref.partition("#")
        repo_arg = repo
    else:
        return ""
    if not shutil.which("gh"):
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


async def default_pr_mergeable(ref: str) -> dict:
    """The PR head's mergeability via gh: {"mergeable": ..., "mergeStateStatus": ...}.

    ``mergeable`` is one of "MERGEABLE" | "CONFLICTING" | "UNKNOWN" | "" (gh
    missing / unparseable ref / network error — treated identically to
    "UNKNOWN" by callers: never act on it). GitHub computes ``mergeable``
    asynchronously after every push (including the rebase this rung itself
    asks for), so "UNKNOWN" is the normal state for a few seconds after a
    push, not a real signal — callers must never treat it as either resolved
    or still-conflicting.
    """
    if not shutil.which("gh"):
        return {"mergeable": "", "mergeStateStatus": ""}
    target = _gh_repo_and_number(ref)
    if not target:
        return {"mergeable": "", "mergeStateStatus": ""}
    repo_arg, num_str = target
    out = await _run_cli([
        "gh", "pr", "view", num_str, "--repo", repo_arg,
        "--json", "mergeable,mergeStateStatus",
    ])
    if not out:
        return {"mergeable": "", "mergeStateStatus": ""}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"mergeable": "", "mergeStateStatus": ""}
    return {
        "mergeable": str(data.get("mergeable") or "").upper(),
        "mergeStateStatus": str(data.get("mergeStateStatus") or "").upper(),
    }


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

    `<build>/consoleText` answers HTTP Basic with the credentials from
    ~/.no_human/.env even where the browser URL redirects to SSO — the API path
    and the human path authenticate differently, which is the whole reason this
    reaches for Basic. Where the TLS chain is an internal CA, verification is
    disabled for that host: the excerpt feeds a prompt, it is not an integrity
    boundary. Best-effort by
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


async def _git_rc(repo_path: str, *args: str) -> tuple[int, str]:
    """Run a local git command; return (returncode, stripped stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except OSError:
        # FileNotFoundError (git absent) and E2BIG (argv too long on a very
        # large touched set) both land here. The caller's contract is "never
        # a false shipped", so any failure must read as a non-zero rc.
        return 1, ""
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def refs_resolvable(repo_path: str, *refs: str) -> bool:
    """Whether every ``ref`` names something git can resolve in ``repo_path``.

    A PRECONDITION probe, not a fate probe. ``default_branch_shipped`` below
    deliberately collapses "the content is not on base" and "the check could
    not run" into a single ``False``, because its one production caller must
    never see a false "shipped". That is the right default for a caller that
    ACTS on the answer, and the wrong one for a caller that RECORDS it: a
    merged PR whose head branch was deleted afterwards — the common case, since
    a squash-merged branch has no further use — makes ``merge-base`` fail, and
    a recorder that trusted the resulting ``False`` would write "closed without
    merging" as a settled verdict about a PR that in fact landed.

    So telemetry asks this first and treats an unresolvable ref as "cannot
    tell" (``None``) rather than as evidence of absence. Uses ``rev-parse
    --verify`` with a trailing ``^{commit}`` so a ref that exists but does not
    name a commit is not mistaken for one.
    """
    if not repo_path:
        return False
    for ref in refs:
        if not ref:
            return False
        rc, _ = await _git_rc(repo_path, "rev-parse", "--verify", "--quiet",
                              f"{ref}^{{commit}}")
        if rc != 0:
            return False
    return True


async def default_branch_shipped(repo_path: str, branch: str, base: str = "main") -> bool:
    """Whether ``branch``'s changes are actually present in ``base``, checked
    by tree CONTENT rather than commit ancestry.

    A branch's changes routinely land on ``base`` as a SQUASH merge — one
    brand-new commit carrying the content but none of the branch's commits.
    When they do, `git merge-base --is-ancestor <branch> <base>` is FALSE even
    though the change is fully landed: ancestry tracks commit lineage, and a
    squash commit has no lineage back to the branch it came from. Ancestry is
    therefore not a valid "did this ship" test here.

    Instead: find the files ``branch`` touched relative to its merge-base with
    ``base``, then diff those same paths between ``branch`` and ``base``'s
    current tip. No remaining differences means the content already landed,
    regardless of how the commit graph got there.

    Returns False (never a false "shipped") on any git failure: missing repo,
    missing/deleted branch, unrelated histories, etc. -- callers must treat
    False as "can't tell", same as GitHub's PR state going unknown.
    """
    if not repo_path or not branch:
        return False
    rc, merge_base = await _git_rc(repo_path, "merge-base", branch, base)
    if rc != 0 or not merge_base:
        return False
    # --no-renames: rename detection reports a `git mv` as the DESTINATION
    # path only, so the source path never enters the touched set and its
    # deletion is never compared against base. A branch that moves a file
    # would then report "shipped" while the removal half had not landed, and
    # the task would be marked DONE with half its deliverable missing.
    # Trailing `--`: without it, a branch whose name is also a path in the
    # tree makes git bail with "ambiguous argument".
    # `-z`: git C-QUOTES any path containing a non-ASCII byte, a quote, a
    # backslash or a control character (core.quotePath, on by default), e.g.
    # `café.py` comes back as `"caf\303\251.py"`. Feeding that literal string
    # back as a pathspec matches NOTHING, so `--quiet` reports "no
    # differences" and a branch that never landed is reported as shipped.
    # `-z` emits raw NUL-separated names, so what we pass back is what git
    # gave us.
    rc, touched = await _git_rc(
        repo_path, "diff", "--name-only", "-z", "--no-renames",
        merge_base, branch, "--",
    )
    if rc != 0:
        return False
    files = [f for f in touched.split("\0") if f.strip()]
    if not files:
        return True  # branch never diverged in content from base — trivially shipped
    rc, _ = await _git_rc(repo_path, "diff", "--quiet", branch, base, "--", *files)
    if rc not in (0, 1):  # anything other than "clean"/"differs" is an error
        return False
    return rc == 0
