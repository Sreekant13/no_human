"""`nh approve` merges the PR: local squash as the operator identity.

Operator directive 2026-08-12 (refile of 74bf7dec): approving a completed
task must MERGE its PR, not just record approval. This is the one sanctioned
place the product performs a real merge — done under the OPERATOR's git
identity (``git.approve_identity``), never the agent's, and only in response
to an explicit human `nh approve` / API call. The agent itself still never
merges anything (constraint #2 is unchanged).

The eight-step procedure, proven by hand before this module existed:

  1. preconditions   — config enabled, a PR exists, `gh` is on PATH, the
                        branch resolves. (Review-PASS-for-head-sha is a
                        CALLER precondition — see `cli/commands.py`'s
                        `approve` — because it needs `Orchestrator.
                        _rounds_for_head`, which this lower-level vcs module
                        must not import.)
  2. fetch + worktree — fetch the remote, resolve the CURRENT default-branch
                        tip, and create a detached temp worktree there.
  3. squash           — `git merge --squash <branch>` into the worktree.
  4. manifest         — the merge-result ledger rule: reset ONLY
                        RELEASE_MANIFEST.txt to the tip's version (the
                        branch's EXPORT_CLASSIFICATION.txt, with its own
                        count bumps, is left exactly as the squash produced
                        it), then `export_guard.py approve` the branch's
                        changed ship-classified files so the manifest is
                        re-derived from tip + those pins, and stage it.
  5. commit           — one commit, `-c user.name=<operator> -c
                        user.email=<operator>`, message = task title + task
                        id + review-evidence line.
  6. verify + tests   — `export_guard.py verify`, then the task's
                        change-scoped tests, both run IN the worktree.
  7. push             — re-check the tip has not moved, then push the landed
                        commit straight onto the remote's default branch ref
                        (a non-force push, so a raced tip is refused by git
                        itself as well as by the re-check); verify the
                        remote ref actually advanced.
  8. close_pr         — close the PR without a comment (a comment re-wakes
                        the watcher — ticket b1fd13ca); idempotent on an
                        already-closed/merged PR; a failure here is a
                        non-fatal warning — the code is already on the
                        default branch.

Every step failure removes the temp worktree and returns a
:class:`LandResult` naming the failing `step` and the captured `stderr` —
the caller leaves the task `awaiting_approval` and surfaces both verbatim.
Nothing here touches the task store; that is the caller's job (mirrors
`manifest_repair.py`'s shape).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .git import GitError, GitRepo, ProtectedBranch
from .pr_watcher import parse_pr_url

STEPS = (
    "preconditions", "fetch", "worktree", "squash", "manifest",
    "commit", "verify", "tests", "push", "close_pr",
)

_STDERR_CAP = 4000
_DEFAULT_TEST_TIMEOUT_S = 1800
_APPROVE_TIMEOUT_S = 120
_VERIFY_TIMEOUT_S = 120
_GH_TIMEOUT_S = 30

_DEFAULT_OPERATOR_NAME = "eyalgolan"
_DEFAULT_OPERATOR_EMAIL = "5146175+eyalgolan@users.noreply.github.com"


@dataclass(frozen=True)
class LandResult:
    """The outcome of one `land_task` run.

    ``step`` is always one of :data:`STEPS` — the step that failed (``ok``
    is False), or the last step that ran (``ok`` is True; normally
    ``"close_pr"``). ``skipped`` marks the today's-behaviour record-only
    path (``approve_merge.enabled`` false, no PR, or no `gh`) — that is
    still ``ok=True``, never a failure.
    """

    ok: bool
    step: str = ""
    landed_sha: str = ""
    pr_url: str = ""
    branch: str = ""
    message: str = ""
    stderr: str = ""
    skipped: bool = False


def _cap(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _STDERR_CAP:
        return text
    return text[:_STDERR_CAP] + "\n…(truncated)"


def _sh(args: list[str], *, cwd: Path | str, timeout: float | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env=env,
    )


def _load_export_builder(root: Path):
    """Load `scripts/build_public_export.py` by path, same as `export_guard.py`
    does — best-effort: returns None on any error, since this is only used to
    narrow the file list handed to `approve` (never a decision the gate
    itself is trusted to make)."""
    path = root / "scripts" / "build_public_export.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            "_nh_approve_merge_build_public_export", path)
        module = importlib.util.module_from_spec(spec)
        # `build_public_export.py` uses `@dataclass`, and dataclasses resolves
        # forward-ref types via `sys.modules[cls.__module__]` — the module MUST
        # be registered before `exec_module` runs the class body, or that
        # lookup finds nothing and raises (matches `export_guard.py`'s own
        # `_load_builder`, which registers for the same reason).
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — advisory narrowing only
        return None


def _ship_classified_paths(root: Path, paths: list[str]) -> list[str]:
    """*paths* filtered to the ones `export_guard.py` classifies ship.

    A drop-classified changed path (a test file, a doc) handed to `approve`
    would be REFUSED as "not ship-classified" and wrongly abort the land —
    this is what keeps the manifest step scoped to files the guard will
    actually accept."""
    if not paths:
        return []
    builder = _load_export_builder(root)
    if builder is None:
        return []
    try:
        rules = builder.parse_classification(
            (root / builder.CLASSIFICATION_NAME).read_text(encoding="utf-8"))
        tracked_out = _sh(["git", "ls-files", "-z"], cwd=root).stdout
        tracked = [p for p in tracked_out.split("\0") if p]
        cls = builder.classify(rules, tracked)
    except Exception:  # noqa: BLE001 — advisory narrowing only
        return []
    shipped = set(cls.shipped)
    manifest_name = builder.RELEASE_MANIFEST_NAME
    return [p for p in paths if p in shipped and p != manifest_name]


def _map_change_scoped_tests(root: Path, changed_files: list[str]) -> list[str]:
    """`src/no_human/**/<stem>.py` -> `tests/test_<stem>.py`, plus any changed
    path already under `tests/`. Missing mappings are silently dropped — the
    caller logs "no change-scoped tests matched" rather than claim a pass."""
    tests: list[str] = []
    for f in changed_files:
        p = Path(f)
        if p.suffix != ".py":
            continue
        if f.startswith("tests/"):
            if (root / f).exists():
                tests.append(f)
            continue
        if f.startswith("src/"):
            candidate = f"tests/test_{p.stem}.py"
            if (root / candidate).exists():
                tests.append(candidate)
    return sorted(dict.fromkeys(tests))


def _close_pr(pr_url: str) -> str:
    """Close *pr_url* without a comment. Idempotent, best-effort: any problem
    (gh/glab missing, network, already handled) returns a note but never
    raises — the code is already on the default branch by the time this
    runs, so failing the task here would strand a landed change."""
    parsed = parse_pr_url(pr_url)
    if not parsed:
        return "pr close skipped: could not parse PR URL"
    forge, host, slug, number = parsed

    if forge == "gitlab":
        if not shutil.which("glab"):
            return "mr close skipped: glab not found"
        try:
            state_proc = _sh(["glab", "mr", "view", str(number), "-R", slug],
                              cwd=Path.cwd(), timeout=_GH_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"mr close skipped: could not read state ({exc})"
        out = (state_proc.stdout or "").lower()
        if "state:\tmerged" in out or "state:\tclosed" in out:
            return ""
        try:
            close_proc = _sh(["glab", "mr", "close", str(number), "-R", slug],
                              cwd=Path.cwd(), timeout=_GH_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"mr close failed (non-fatal): {exc}"
        if close_proc.returncode != 0:
            return f"mr close failed (non-fatal): {close_proc.stderr.strip()[:300]}"
        return ""

    if not shutil.which("gh"):
        return "pr close skipped: gh not found"
    repo_arg = f"{host}/{slug}"
    state = ""
    try:
        state_proc = _sh(
            ["gh", "pr", "view", str(number), "--repo", repo_arg, "--json", "state"],
            cwd=Path.cwd(), timeout=_GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"pr close skipped: could not read state ({exc})"
    if state_proc.returncode == 0:
        try:
            state = str(json.loads(state_proc.stdout).get("state") or "").upper()
        except json.JSONDecodeError:
            state = ""
    if state in ("MERGED", "CLOSED"):
        return ""
    try:
        close_proc = _sh(["gh", "pr", "close", str(number), "--repo", repo_arg],
                          cwd=Path.cwd(), timeout=_GH_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"pr close failed (non-fatal): {exc}"
    if close_proc.returncode != 0:
        return f"pr close failed (non-fatal): {close_proc.stderr.strip()[:300]}"
    return ""


def land_task(
    *,
    repo_path: Path | str,
    branch: str,
    pr_url: str,
    task_id: str,
    task_title: str,
    review_evidence: str,
    config: dict,
    changed_test_paths: list[str] | None = None,
    remote: str = "origin",
    _before_push: Callable[[], None] | None = None,
) -> LandResult:
    """Run the whole land procedure. Never raises — every failure path
    returns a :class:`LandResult` with ``ok=False`` and the failing step.

    ``config`` is a plain nested dict (``Config.data`` in production) —
    reads ``config["git"]`` (``agent_identity_name``/``_email``,
    ``never_push_to``, ``approve_identity``) and ``config["approve_merge"]``
    (``enabled``, ``test_timeout_seconds``).

    ``_before_push`` is a test-only seam: a callable invoked immediately
    before the push-time tip re-check, so a test can simulate a concurrent
    push landing on the remote's default branch mid-run — otherwise
    unreachable from a synchronous, single-threaded call.
    """
    approve_cfg = config.get("approve_merge") or {}
    if not approve_cfg.get("enabled", True):
        return LandResult(ok=True, step="preconditions", skipped=True, branch=branch,
                           pr_url=pr_url, message="approve_merge disabled — "
                           "approval recorded only, merge the PR yourself")
    if not (pr_url or "").strip():
        return LandResult(ok=True, step="preconditions", skipped=True, branch=branch,
                           message="no PR to merge")
    if not shutil.which("gh"):
        return LandResult(ok=True, step="preconditions", skipped=True, branch=branch,
                           pr_url=pr_url, message="gh CLI not found — cannot merge "
                           "automatically; merge the PR yourself")

    git_cfg = config.get("git") or {}
    try:
        repo = GitRepo(
            Path(repo_path),
            identity_name=git_cfg.get("agent_identity_name", "no_human"),
            identity_email=git_cfg.get("agent_identity_email", "no-human@acme.com"),
            never_push_to=git_cfg.get("never_push_to")
            or ["main", "master", "release/*"],
        )
    except GitError as exc:
        return LandResult(ok=False, step="preconditions", branch=branch, pr_url=pr_url,
                           stderr=_cap(str(exc)))

    resolved_branch = repo.resolve_commitish(branch)
    if not resolved_branch:
        return LandResult(ok=False, step="preconditions", branch=branch, pr_url=pr_url,
                           stderr=f"branch {branch!r} does not resolve to a commit")

    ident = git_cfg.get("approve_identity") or {}
    op_name = ident.get("name") or _DEFAULT_OPERATOR_NAME
    op_email = ident.get("email") or _DEFAULT_OPERATOR_EMAIL
    test_timeout = approve_cfg.get("test_timeout_seconds", _DEFAULT_TEST_TIMEOUT_S)

    # -- step 2: fetch + resolve the CURRENT default-branch tip ------------ #
    repo.fetch(remote)
    resolved_branch = repo.resolve_commitish(branch) or resolved_branch
    default = repo.default_branch()
    if not default:
        return LandResult(ok=False, step="fetch", branch=branch, pr_url=pr_url,
                           stderr="cannot resolve the remote's default branch")
    # Always the REMOTE-TRACKING ref, never `resolve_commitish(default)`: a
    # task's repo_path clone routinely carries a local branch of the same
    # name as the default branch (e.g. left over from clone time) that
    # `git fetch` never fast-forwards — `resolve_commitish` prefers exactly
    # that stale local branch over `origin/<default>`, which silently pins
    # the land to a base that predates this run's own fetch.
    tip_ref = f"{remote}/{default}"
    tip_proc = _sh(["git", "rev-parse", "--verify", "--quiet", tip_ref], cwd=repo.path)
    if tip_proc.returncode != 0 or not tip_proc.stdout.strip():
        return LandResult(ok=False, step="fetch", branch=branch, pr_url=pr_url,
                           stderr=f"cannot resolve {tip_ref!r} to a commit")
    if tip_proc.returncode != 0:
        return LandResult(ok=False, step="fetch", branch=branch, pr_url=pr_url,
                           stderr=_cap(tip_proc.stderr))
    tip_sha = tip_proc.stdout.strip()

    # -- worktree, at the tip, detached -------------------------------------#
    tmp_dir = Path(tempfile.mkdtemp(prefix="nh-land-"))
    shutil.rmtree(tmp_dir, ignore_errors=True)  # free the name for `worktree add`
    try:
        repo.add_worktree(tmp_dir, base=tip_sha, detach=True)
    except (GitError, ProtectedBranch) as exc:
        return LandResult(ok=False, step="worktree", branch=branch, pr_url=pr_url,
                           stderr=_cap(str(exc)))

    try:
        return _land_in_worktree(
            repo=repo, worktree_path=tmp_dir, remote=remote, branch=branch,
            resolved_branch=resolved_branch, default=default, tip_sha=tip_sha,
            task_id=task_id, task_title=task_title, review_evidence=review_evidence,
            op_name=op_name, op_email=op_email, test_timeout=test_timeout,
            pr_url=pr_url, changed_test_paths=changed_test_paths,
            _before_push=_before_push,
        )
    finally:
        repo.remove_worktree(tmp_dir, force=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _land_in_worktree(
    *, repo: GitRepo, worktree_path: Path, remote: str, branch: str,
    resolved_branch: str, default: str, tip_sha: str, task_id: str,
    task_title: str, review_evidence: str, op_name: str, op_email: str,
    test_timeout: float, pr_url: str, changed_test_paths: list[str] | None,
    _before_push: Callable[[], None] | None,
) -> LandResult:
    # -- step 3: squash --------------------------------------------------- #
    merge_proc = _sh(["git", "merge", "--squash", resolved_branch], cwd=worktree_path)
    if merge_proc.returncode != 0:
        _sh(["git", "merge", "--abort"], cwd=worktree_path)
        return LandResult(ok=False, step="squash", branch=branch, pr_url=pr_url,
                           stderr=_cap(merge_proc.stdout + "\n" + merge_proc.stderr))

    # -- step 4: manifest merge-result ledger rule ------------------------ #
    guard = worktree_path / "scripts" / "export_guard.py"
    manifest = worktree_path / "RELEASE_MANIFEST.txt"
    if guard.exists() and manifest.exists():
        co = _sh(["git", "checkout", tip_sha, "--", "RELEASE_MANIFEST.txt"],
                  cwd=worktree_path)
        if co.returncode != 0:
            return LandResult(ok=False, step="manifest", branch=branch, pr_url=pr_url,
                               stderr=_cap(co.stderr))

        diff_proc = _sh(
            ["git", "diff", "--name-only", "--diff-filter=d",
             f"{tip_sha}..{resolved_branch}"],
            cwd=worktree_path,
        )
        changed = [p.strip() for p in diff_proc.stdout.splitlines() if p.strip()]
        shipped_changed = _ship_classified_paths(worktree_path, changed)

        if shipped_changed:
            _sh(["git", "add", "-A", "--", *shipped_changed], cwd=worktree_path)
            try:
                approve_proc = _sh(
                    [sys.executable, "scripts/export_guard.py", "approve",
                     *shipped_changed],
                    cwd=worktree_path, timeout=_APPROVE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return LandResult(
                    ok=False, step="manifest", branch=branch, pr_url=pr_url,
                    stderr=f"export_guard approve timed out after {_APPROVE_TIMEOUT_S}s")
            if approve_proc.returncode != 0:
                why = ("scan-hit refusal" if approve_proc.returncode == 1
                       else "refused before writing pins")
                return LandResult(
                    ok=False, step="manifest", branch=branch, pr_url=pr_url,
                    stderr=_cap(f"export_guard approve {why} "
                                f"({approve_proc.returncode}):\n"
                                + approve_proc.stdout + approve_proc.stderr))

        add_manifest = _sh(["git", "add", "--", "RELEASE_MANIFEST.txt"], cwd=worktree_path)
        if add_manifest.returncode != 0:
            return LandResult(ok=False, step="manifest", branch=branch, pr_url=pr_url,
                               stderr=_cap(add_manifest.stderr))

    # -- step 5: operator-identity commit ---------------------------------- #
    # `GIT_AUTHOR_NAME`/`_EMAIL`/`GIT_COMMITTER_NAME`/`_EMAIL` env vars, when
    # set, OUTRANK `-c user.name=`/`user.email=` on the command line — and
    # `nh approve` can itself run inside a coder/agent process whose sandbox
    # sets exactly those four to the AGENT identity (observed in practice).
    # Scrubbed here so the operator identity always wins regardless of the
    # ambient environment this runs in.
    message = f"{task_title}\n\n{task_id}\n{review_evidence}"
    commit_env = dict(os.environ)
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        commit_env.pop(var, None)
    commit_proc = _sh(
        ["git", "-c", f"user.name={op_name}", "-c", f"user.email={op_email}",
         "commit", "-m", message],
        cwd=worktree_path, env=commit_env,
    )
    if commit_proc.returncode != 0:
        return LandResult(ok=False, step="commit", branch=branch, pr_url=pr_url,
                           stderr=_cap(commit_proc.stdout + "\n" + commit_proc.stderr))
    landed_sha = _sh(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    # -- step 6a: export_guard verify -------------------------------------- #
    if guard.exists():
        try:
            verify_proc = _sh([sys.executable, "scripts/export_guard.py", "verify"],
                               cwd=worktree_path, timeout=_VERIFY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return LandResult(ok=False, step="verify", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=f"export_guard verify timed out after "
                                      f"{_VERIFY_TIMEOUT_S}s")
        if verify_proc.returncode != 0:
            return LandResult(ok=False, step="verify", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=_cap(verify_proc.stdout + "\n" + verify_proc.stderr))

    # -- step 6b: change-scoped tests --------------------------------------#
    test_paths = changed_test_paths
    if test_paths is None:
        squash_diff = _sh(["git", "diff", "--name-only", f"{tip_sha}..HEAD"],
                           cwd=worktree_path)
        changed_files = [p.strip() for p in squash_diff.stdout.splitlines() if p.strip()]
        test_paths = _map_change_scoped_tests(worktree_path, changed_files)
    if test_paths:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(worktree_path / "src")
        try:
            test_proc = _sh([sys.executable, "-m", "pytest", "-q", *test_paths],
                             cwd=worktree_path, timeout=test_timeout, env=env)
        except subprocess.TimeoutExpired:
            return LandResult(ok=False, step="tests", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=f"change-scoped tests timed out after "
                                      f"{test_timeout}s")
        if test_proc.returncode != 0:
            return LandResult(ok=False, step="tests", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=_cap(test_proc.stdout + "\n" + test_proc.stderr))

    # -- step 7: ff-merge + push, remote-ref verified ---------------------- #
    if _before_push is not None:
        _before_push()
    repo.fetch(remote)
    check_proc = _sh(
        ["git", "-C", str(repo.path), "rev-parse", "--verify", "--quiet",
         f"{remote}/{default}"],
        cwd=repo.path,
    )
    current_tip = check_proc.stdout.strip()
    if current_tip and current_tip != tip_sha:
        return LandResult(
            ok=False, step="push", branch=branch, pr_url=pr_url, landed_sha=landed_sha,
            stderr=f"{default} advanced from {tip_sha[:12]} to {current_tip[:12]} "
                   "during land; retry")

    # Pushed from the MAIN repo, deliberately NOT the worktree: `add_worktree`
    # installs a pre-push hook there (push_hook.py) that refuses any push
    # whose resolved ref matches `never_push_to` — the agent's second
    # enforcement point. This IS the one sanctioned protected-branch write
    # (a human `nh approve`, never the agent), and the main repo carries no
    # such hook, so pushing from there is what makes this write reach the
    # remote at all. Both worktrees share one object database, so the sha
    # created in the worktree is already visible here.
    push_proc = _sh(
        ["git", "push", remote, f"{landed_sha}:refs/heads/{default}"],
        cwd=repo.path,
    )
    if push_proc.returncode != 0:
        return LandResult(ok=False, step="push", branch=branch, pr_url=pr_url,
                           landed_sha=landed_sha,
                           stderr=_cap(push_proc.stdout + "\n" + push_proc.stderr))

    ls_proc = _sh(["git", "ls-remote", remote, f"refs/heads/{default}"], cwd=repo.path)
    remote_sha = (ls_proc.stdout.split() or [""])[0]
    if remote_sha != landed_sha:
        return LandResult(
            ok=False, step="push", branch=branch, pr_url=pr_url, landed_sha=landed_sha,
            stderr=f"remote ref did not advance to {landed_sha} "
                   f"(saw {remote_sha or '(none)'})")

    # -- step 8: close the PR, without a comment ---------------------------#
    close_note = _close_pr(pr_url)
    msg = f"landed {landed_sha[:12]} onto {default}"
    if close_note:
        msg += f"; {close_note}"
    return LandResult(ok=True, step="close_pr", branch=branch, pr_url=pr_url,
                       landed_sha=landed_sha, message=msg)
