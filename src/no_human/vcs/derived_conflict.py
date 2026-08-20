"""Mechanical resolution for a PR conflict confined to derived artefacts.

Bugfix context: `_check_pr_conflict` (blockers/wake.py) used to open a full
coder round for *every* CONFLICTING PR, including the case where the ONLY
conflicting path is a file that is regenerated from the tree — never
authored, never touched by a coder. A coder round can't fix that: it never
edits `RELEASE_MANIFEST.txt`, so the round burns an attempt (a real task,
8e153f1e, spent ~4.5M tokens across two such rounds that pushed zero files)
and the PR stays CONFLICTING. This module answers the two questions the rung
needs — "is this conflict confined to derived artefacts?" and, if so,
"resolve it" — without a coder in the loop.

`EXPORT_CLASSIFICATION.txt` is NOT a derived artefact by this module's
membership rule (see `DERIVED_ARTEFACTS` below) even though it sits next to
`RELEASE_MANIFEST.txt` in the export gate: its per-rule win-COUNTs are
hand-maintained, not rebuilt by any command, so a conflict touching it still
needs a coder round exactly as before this module existed. ONE exception,
which is not a decision: when the file merged CLEANLY and a count is stale
only because both sides bumped it for files each added, the correct number is
base + (branch - merge-base), and `_reconcile_merge_count_drift` writes it
under exactly that equality (INCIDENT 2026-08-20, task c309a6a3).

`resolve_derived_conflict` is synchronous (it shells out, like
`approve_merge.py`, which it reuses); the async watcher calls it via
`asyncio.to_thread`.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .approve_merge import (
    _APPROVE_TIMEOUT_S,
    _VERIFY_TIMEOUT_S,
    CLASSIFICATION_NAME,
    COUNT_DRIFT_RE,
    _cap,
    _cleanup_worktree,
    _sh,
    _ship_classified_paths,
    reconcile_merge_count_drift,
)
from .git import GitError, GitRepo, ProtectedBranch
from .pr_watcher import _base_tips, _git_rc, merge_tree_conflicts, refs_resolvable

#: Repo-root paths that are REGENERATED FROM THE TREE, never authored: their
#: bytes are a pure function of the other files in the commit, so a merge
#: conflict in one of them carries no human decision to resolve — the only
#: correct resolution is to take either side and re-derive. MEMBERSHIP RULE:
#: a file belongs here IFF a documented command rebuilds ALL of its content
#: from the tree, with no hand-edit ever required or permitted.
#: `RELEASE_MANIFEST.txt` qualifies — it is one `<sha256>  <path>` pin per
#: shipped file, and `export_guard.py approve` rewrites every pin from the
#: working tree.
#:
#: `EXPORT_CLASSIFICATION.txt` does NOT qualify, despite sitting right next to
#: the manifest in the same export gate: its rule lines carry a hand-maintained
#: win-COUNT (`ship 293  tests/*.py`), and no command in `export_guard.py`
#: re-tallies that count — `approve` rebuilds manifest pins and nothing else,
#: `verify` only checks the count against the tree and REFUSES on a mismatch,
#: it never repairs one (the one repair this module makes, the merge
#: arithmetic in `_reconcile_merge_count_drift`, applies only to a file that
#: did NOT conflict). Taking `--ours` on a real conflict there would
#: silently discard a hand decision (a ship/drop flip, a new pattern) that
#: only a coder round can make correctly — the exact thing this module exists
#: to avoid doing to genuinely derived content. So a conflict touching this
#: file — alone, or mixed with the manifest — falls through to a coder round.
#:
#: Exact repo-root paths, never a glob or basename — `docs/RELEASE_MANIFEST.txt`
#: must NOT qualify (same doctrine as pr_watcher._GENERATED_LEDGERS). Adding a
#: second derived file is a one-line change HERE and nowhere else — but see
#: the membership rule above before adding one.
DERIVED_ARTEFACTS = frozenset({"RELEASE_MANIFEST.txt"})


def _export_guard_argv() -> list[str]:
    """Base argv for invoking ``export_guard.py``. A module-level seam so
    tests can monkeypatch it to ``[sys.executable, "scripts/export_guard.py"]``
    without a `uv` dependency in the test sandbox, mirroring the documented
    ``uv run python scripts/export_guard.py`` invocation everywhere else."""
    return ["uv", "run", "python", "scripts/export_guard.py"]


async def resolve_base_tip(repo_path: str, base: str) -> str | None:
    """Resolve ``base`` (a branch name, e.g. the task's recorded
    ``base_branch``) to the concrete commit sha this repo should treat as the
    tip a PR targets — preferring the upstream remote-tracking tip over a
    possibly-stale local ref (see ``pr_watcher._base_tips``'s docstring for
    why), and returning ``None`` when neither resolves. Shared by
    `conflicting_paths` (which only needs to *ask* git a question) and
    `wake.py` (which needs a concrete sha to hand `resolve_derived_conflict`,
    a synchronous function that cannot itself await a git probe)."""
    if not repo_path or not base:
        return None
    if not await refs_resolvable(repo_path, base):
        return None
    tips = await _base_tips(repo_path, base)
    ref = tips[-1] if tips else base
    rc, sha = await _git_rc(repo_path, "rev-parse", "--verify", "--quiet",
                            f"{ref}^{{commit}}")
    return sha.strip() if rc == 0 and sha.strip() else None


async def conflicting_paths(repo_path: str, base_tip: str,
                            branch: str) -> set[str] | None:
    """The set of paths `git merge-tree` reports as conflicted for merging
    ``base_tip`` into ``branch``, or ``None`` when the question could not be
    asked at all (git missing, unresolvable refs, unparseable output) — a
    thin wrapper over `pr_watcher.merge_tree_conflicts` that resolves
    ``base_tip`` through `resolve_base_tip` first."""
    if not repo_path or not branch or not base_tip:
        return None
    if not await refs_resolvable(repo_path, branch):
        return None
    resolved_base = await resolve_base_tip(repo_path, base_tip)
    if resolved_base is None:
        return None
    result = await merge_tree_conflicts(repo_path, branch, resolved_base)
    if result is None:
        return None
    return result[1]


def all_derived(paths: set[str] | None) -> bool:
    """True iff `paths` is non-empty and every path in it is a derived
    artefact — the mechanical-resolution eligibility test. `None` (could not
    enumerate) and an empty set both read as "not eligible": the caller must
    never resolve a conflict it could not confirm is derived-only."""
    return bool(paths) and paths <= DERIVED_ARTEFACTS


@dataclass
class DerivedResolution:
    """Result of `resolve_derived_conflict`. `step` names where it stopped —
    one of "worktree", "merge", "regenerate", "verify", "commit", "push", or
    "ok" — so an escalation can name the failing step, never just "it
    failed"."""

    ok: bool
    step: str
    pushed_sha: str = ""
    unpinned: list[str] = field(default_factory=list)
    detail: str = ""
    #: Non-empty when a win-count in EXPORT_CLASSIFICATION.txt — a file this
    #: module otherwise never touches — was rewritten by merge arithmetic;
    #: the human gate must see that, so the caller puts it in the event.
    reconciled: str = ""


def _run_export_guard(worktree_path: Path, subargs: list[str], *,
                      timeout: float) -> subprocess.CompletedProcess | None:
    """Run `export_guard.py` with `subargs` in `worktree_path`; `None` means
    it timed out (the caller treats that as a failure, never as success)."""
    try:
        return _sh([*_export_guard_argv(), *subargs], cwd=worktree_path,
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _unmerged_paths(worktree_path: Path) -> set[str]:
    """Paths `git ls-files -u` reports as still conflicted (any stage)."""
    out = _sh(["git", "ls-files", "-u", "-z"], cwd=worktree_path).stdout
    paths: set[str] = set()
    for field_ in out.split("\0"):
        if not field_:
            continue
        _meta, tab, path = field_.partition("\t")
        if tab and path:
            paths.add(path)
    return paths


def _parse_not_ship_classified(text: str) -> list[str]:
    """Extract the offending paths from an `export_guard.py approve` refusal
    of shape ``approve: REFUSED — not ship-classified (...):\\n  path1\\n
    path2`` (see `export_guard.py::_cmd_approve`) — each listed path is
    indented by exactly two spaces on its own line, directly after the
    header line. Returns `[]` when the text doesn't match that shape (the
    caller then treats the whole batch as an unretried failure)."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "REFUSED" in line and "not ship-classified" in line:
            start = i + 1
            break
    if start is None:
        return []
    found: list[str] = []
    for line in lines[start:]:
        if line.startswith("  ") and line.strip():
            found.append(line.strip())
        else:
            break
    return found


def resolve_derived_conflict(repo_path: str, branch: str, base_tip_sha: str,
                             remote: str = "origin") -> DerivedResolution:
    """Mechanically resolve a PR conflict already confirmed (by the caller,
    via `all_derived`) to be confined to `DERIVED_ARTEFACTS`: merge the base
    tip into a detached worktree of the branch, take either side of the
    derived files and regenerate them from the merged tree, verify, and push
    — no coder session. See the module docstring for why this exists and
    docs/PLAN.md's step-by-step for the exact procedure this implements.

    Never force-pushes; never pushes a tree that fails `export_guard.py
    verify`. A failure at any step is reported via `DerivedResolution.ok =
    False` with `step`/`detail` naming what happened — the caller escalates,
    it never retries with force or weakens a gate.
    """
    root = Path(repo_path)
    guard = root / "scripts" / "export_guard.py"
    if not guard.exists():
        return DerivedResolution(
            ok=False, step="regenerate",
            detail=f"{root} has no scripts/export_guard.py — not an export-"
                   "gated repo, mechanical resolution does not apply")

    try:
        repo = GitRepo(root)
    except GitError as exc:
        return DerivedResolution(ok=False, step="worktree", detail=_cap(str(exc)))

    branch_tip_sha = repo.resolve_commitish(branch)
    if not branch_tip_sha:
        return DerivedResolution(
            ok=False, step="worktree",
            detail=f"branch {branch!r} does not resolve to a commit")

    tmp_dir = Path(tempfile.mkdtemp(prefix="nh-derived-"))
    shutil.rmtree(tmp_dir, ignore_errors=True)  # add_worktree needs the name free
    try:
        try:
            repo.add_worktree(tmp_dir, base=branch_tip_sha, detach=True)
        except (GitError, ProtectedBranch) as exc:
            return DerivedResolution(ok=False, step="worktree", detail=_cap(str(exc)))

        return _resolve_in_worktree(
            repo=repo, worktree_path=tmp_dir, remote=remote, branch=branch,
            base_tip_sha=base_tip_sha,
        )
    finally:
        _cleanup_worktree(repo, tmp_dir)


def _resolve_in_worktree(*, repo: GitRepo, worktree_path: Path, remote: str,
                         branch: str, base_tip_sha: str) -> DerivedResolution:
    branch_tip_sha = _sh(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    # -- step 3: merge --------------------------------------------------- #
    # rc != 0 is expected (that is the conflict this whole module exists to
    # resolve); rc 0 with nothing left conflicted is also fine (someone else
    # resolved it between enumeration and now) — continue either way. But
    # never resolve a conflict this routine did not enumerate: if anything
    # OUTSIDE DERIVED_ARTEFACTS is still conflicted (the base moved in a way
    # that changed the shape of the conflict), bail — a coder round handles
    # that, not this one.
    _sh(["git", "merge", "--no-edit", base_tip_sha], cwd=worktree_path)
    unmerged = _unmerged_paths(worktree_path)
    outside = unmerged - DERIVED_ARTEFACTS
    if outside:
        _sh(["git", "merge", "--abort"], cwd=worktree_path)
        return DerivedResolution(
            ok=False, step="merge",
            detail=_cap("merge produced conflict(s) outside the derived set "
                        f"(base moved?): {sorted(outside)}"))

    # -- step 4: take either side of the derived files; they are rebuilt -- #
    for path in sorted(unmerged & DERIVED_ARTEFACTS):
        co = _sh(["git", "checkout", "--ours", "--", path], cwd=worktree_path)
        if co.returncode != 0:
            _sh(["git", "merge", "--abort"], cwd=worktree_path)
            return DerivedResolution(ok=False, step="merge", detail=_cap(co.stderr))
        add = _sh(["git", "add", "--", path], cwd=worktree_path)
        if add.returncode != 0:
            _sh(["git", "merge", "--abort"], cwd=worktree_path)
            return DerivedResolution(ok=False, step="merge", detail=_cap(add.stderr))

    # -- step 5: regenerate the pins for what the branch actually changed - #
    # No --diff-filter here: a path base added/changed that the (pre-merge)
    # branch tip never had reads as "deleted" in this diff direction and
    # would be dropped by --diff-filter=d, even though it merged in cleanly
    # from base and needs its pin. Keep every name diff reports (including
    # D) — a path genuinely absent from the merged tree is filtered out
    # below by `_ship_classified_paths`, which checks `git ls-files` on the
    # current (post-merge) worktree, so this is safe either way.
    diff_proc = _sh(
        ["git", "diff", "--name-only", f"{base_tip_sha}..{branch_tip_sha}"],
        cwd=worktree_path,
    )
    if diff_proc.returncode != 0:
        return DerivedResolution(ok=False, step="regenerate", detail=_cap(diff_proc.stderr))
    changed = sorted({
        p.strip() for p in diff_proc.stdout.splitlines()
        if p.strip() and p.strip() not in DERIVED_ARTEFACTS
    })
    shipped_changed = _ship_classified_paths(worktree_path, changed)
    unpinned = sorted(set(changed) - set(shipped_changed))
    reconciled = ""

    if shipped_changed:
        _sh(["git", "add", "-A", "--", *shipped_changed], cwd=worktree_path)
        approve_proc = _run_export_guard(
            worktree_path, ["approve", *shipped_changed], timeout=_APPROVE_TIMEOUT_S)
        if approve_proc is None:
            return DerivedResolution(
                ok=False, step="regenerate", unpinned=unpinned,
                detail=f"export_guard approve timed out after {_APPROVE_TIMEOUT_S}s")

        if approve_proc.returncode == 2 and COUNT_DRIFT_RE.search(
                approve_proc.stdout + approve_proc.stderr):
            ok, note = reconcile_merge_count_drift(
                worktree_path, base_tip_sha, branch_tip_sha,
                approve_proc.stdout + approve_proc.stderr)
            if not ok:
                return DerivedResolution(
                    ok=False, step="regenerate", unpinned=unpinned,
                    detail=_cap(f"{CLASSIFICATION_NAME} count drift is not merge "
                                f"arithmetic ({note}):\n"
                                + approve_proc.stdout + approve_proc.stderr))
            reconciled = note
            # The rewritten classification is itself a shipped, pinned file
            # wherever the repo ships it — re-pin it too, or step-7 verify
            # refuses the tree on its stale hash (found by the land-path
            # fixture, whose classification is pinned like the real repo's).
            retry_targets = list(dict.fromkeys(
                [*shipped_changed,
                 *_ship_classified_paths(worktree_path, [CLASSIFICATION_NAME])]))
            approve_proc = _run_export_guard(
                worktree_path, ["approve", *retry_targets], timeout=_APPROVE_TIMEOUT_S)
            if approve_proc is None:
                return DerivedResolution(
                    ok=False, step="regenerate", unpinned=unpinned,
                    detail=f"export_guard approve timed out after {_APPROVE_TIMEOUT_S}s "
                           f"(after count reconcile: {note})")

        if approve_proc.returncode == 2:
            combined = approve_proc.stdout + approve_proc.stderr
            refused = _parse_not_ship_classified(combined)
            if refused:
                # Belt-and-braces: a drop-classified path in the branch's own
                # diff — not a failure, just nothing to pin. Retry once with
                # those paths removed.
                unpinned = sorted(set(unpinned) | set(refused))
                retry_targets = [p for p in shipped_changed if p not in refused]
                if retry_targets:
                    approve_proc = _run_export_guard(
                        worktree_path, ["approve", *retry_targets],
                        timeout=_APPROVE_TIMEOUT_S)
                    if approve_proc is None:
                        return DerivedResolution(
                            ok=False, step="regenerate", unpinned=unpinned,
                            detail="export_guard approve timed out after "
                                   f"{_APPROVE_TIMEOUT_S}s (retry)")
                else:
                    approve_proc = None  # nothing left that is ship-classified

        if approve_proc is not None and approve_proc.returncode != 0:
            # exit 1 (scan-hit refusal) is never retried.
            why = ("scan-hit refusal" if approve_proc.returncode == 1
                   else "refused before writing pins")
            return DerivedResolution(
                ok=False, step="regenerate", unpinned=unpinned,
                detail=_cap(f"export_guard approve {why} "
                            f"({approve_proc.returncode}):\n"
                            + approve_proc.stdout + approve_proc.stderr))

    # -- step 6: commit --------------------------------------------------- #
    for name in sorted(DERIVED_ARTEFACTS):
        if (worktree_path / name).exists():
            add = _sh(["git", "add", "--", name], cwd=worktree_path)
            if add.returncode != 0:
                return DerivedResolution(ok=False, step="regenerate",
                                          unpinned=unpinned, detail=_cap(add.stderr))

    status = _sh(["git", "status", "--porcelain"], cwd=worktree_path).stdout
    if status.strip():
        commit_proc = _sh(["git", "commit", "--no-edit"], cwd=worktree_path)
        if commit_proc.returncode != 0:
            return DerivedResolution(
                ok=False, step="commit", unpinned=unpinned,
                detail=_cap(commit_proc.stdout + "\n" + commit_proc.stderr))
    # else: `git merge` above already auto-committed (no conflicts, nothing
    # left to stage) — HEAD is already the merge commit.
    merge_sha = _sh(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    # -- step 7: verify BEFORE any push ------------------------------------ #
    verify_proc = _run_export_guard(worktree_path, ["verify"], timeout=_VERIFY_TIMEOUT_S)
    if verify_proc is None:
        return DerivedResolution(
            ok=False, step="verify", unpinned=unpinned,
            detail=f"export_guard verify timed out after {_VERIFY_TIMEOUT_S}s")
    if verify_proc.returncode != 0:
        return DerivedResolution(
            ok=False, step="verify", unpinned=unpinned,
            detail=_cap(verify_proc.stdout + "\n" + verify_proc.stderr))

    # -- step 8: push from the MAIN repo, not the worktree ----------------- #
    # `add_worktree` installs a pre-push guard (push_hook.py) there that
    # refuses any push matching `never_push_to` — the second enforcement
    # point (approve_merge.py documents the same reasoning for `land_task`).
    # This routine only ever pushes the PR's OWN feature branch, but pushing
    # from the main repo is what makes the push land at all: both share one
    # object database, so the sha created in the worktree is already visible
    # here. Plain (non-force) push only — the merge commit is a fast-forward
    # descendant of the branch tip.
    push_proc = _sh(
        ["git", "push", remote, f"{merge_sha}:refs/heads/{branch}"],
        cwd=repo.path,
    )
    if push_proc.returncode != 0:
        return DerivedResolution(
            ok=False, step="push", unpinned=unpinned,
            detail=_cap(push_proc.stdout + "\n" + push_proc.stderr))

    # Compare-and-swap the local branch ref so it isn't left stale; best
    # effort only — a failure here doesn't undo an already-successful push.
    _sh(["git", "update-ref", f"refs/heads/{branch}", merge_sha, branch_tip_sha],
        cwd=repo.path)

    return DerivedResolution(
        ok=True, step="ok", pushed_sha=merge_sha, unpinned=unpinned,
        reconciled=reconciled,
        detail=f"regenerated derived artefact(s) from the merged tree, "
               f"pushed {merge_sha[:8]}"
               + (f"; unpinned (drop-classified): {', '.join(unpinned)}"
                  if unpinned else "")
               + (f"; {CLASSIFICATION_NAME} count reconciled by merge "
                  f"arithmetic: {reconciled}" if reconciled else ""),
    )
