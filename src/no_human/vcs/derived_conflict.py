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
needs a coder round exactly as before this module existed — UNLESS the
conflict's SHAPE, not just its filename, proves there is no hand decision in
it at all: every conflicting hunk in the file differs ONLY in a rule's
numeric win-count, never a pattern, a verb, a comment, or the line order
(`classification_count_only`). That shape means both sides independently
bumped the SAME rule for files each independently added — two reviewed
counts meeting, not two reviewed decisions colliding — and the correct
number is base + (branch - merge-base), written under exactly that equality
by `reconcile_merge_count_drift` (reused, never reimplemented here). This is
the same repair `reconcile_merge_count_drift` already made for a CLEAN merge
with a stale count (INCIDENT 2026-08-20, task c309a6a3); `mechanically_resolvable`
extends its use to the case where the count itself is what conflicts.

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
    _RULE_LINE_RE,
    CLASSIFICATION_NAME,
    COUNT_DRIFT_RE,
    _cap,
    _cleanup_worktree,
    _sh,
    _ship_classified_paths,
    reconcile_merge_count_drift,
)
from .git import GitError, GitRepo, ProtectedBranch
from .pr_watcher import (
    _base_tips,
    _git_rc,
    classification_decisions,
    merge_tree_conflicts,
    refs_resolvable,
)

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
#: `EXPORT_CLASSIFICATION.txt` does NOT qualify FOR MEMBERSHIP HERE, despite
#: sitting right next to the manifest in the same export gate: its rule lines
#: carry a hand-maintained win-COUNT (`ship 293  tests/*.py`), and no command
#: in `export_guard.py` re-tallies that count — `approve` rebuilds manifest
#: pins and nothing else, `verify` only checks the count against the tree and
#: REFUSES on a mismatch, it never repairs one. Taking `--ours` on a real
#: conflict there would, IN GENERAL, silently discard a hand decision (a
#: ship/drop flip, a new pattern) that only a coder round can make correctly
#: — the exact thing this module exists to avoid doing to genuinely derived
#: content. So a conflict touching this file — alone, or mixed with the
#: manifest — falls through to a coder round BY DEFAULT.
#:
#: The one exception is not a filename rule but a SHAPE rule, decided by
#: `classification_count_only` and applied by `mechanically_resolvable`: when
#: every conflicting hunk in the classification file differs ONLY in a rule's
#: numeric win-count (never a pattern, a verb, a comment, or the line order),
#: both sides made the identical decision and independently bumped the same
#: tally for files each added — that is merge arithmetic, not a hand
#: decision, and `reconcile_merge_count_drift` (reused, not reimplemented)
#: already makes exactly this repair for a cleanly-merged file. Taking
#: `--ours` is safe only because eligibility already proved both sides carry
#: identical decisions.
#:
#: Exact repo-root paths, never a glob or basename — `docs/RELEASE_MANIFEST.txt`
#: must NOT qualify (same doctrine as pr_watcher._GENERATED_LEDGERS). Adding a
#: second derived file is a one-line change HERE and nowhere else — but see
#: the membership rule above before adding one. `DERIVED_ARTEFACTS` itself
#: stays `RELEASE_MANIFEST.txt`-only; the classification file is admitted per
#: conflict, per `mechanically_resolvable`, never unconditionally.
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


async def fetch_conflict_refs(repo_path: str, base: str, branch: str) -> bool:
    """Best-effort ``git fetch origin <base> <branch>`` — the common cause of
    an enumeration failure (`conflicting_paths` raising, or returning
    ``None``) is a stale/missing ref in the watcher's checkout. Returns
    whether the fetch succeeded; the caller retries the enumeration EITHER
    WAY (per the intake answer: a transient fetch failure must not
    short-circuit the retry).

    ``_git_rc`` already swallows `OSError` (git absent) and enforces
    `_GIT_TIMEOUT`, returning `rc=1` for both — so this never raises for
    those; it only additionally guards empty arguments.
    """
    if not repo_path or not base or not branch:
        return False
    rc, _ = await _git_rc(repo_path, "fetch", "--quiet", "origin", base, branch)
    return rc == 0


def all_derived(paths: set[str] | None) -> bool:
    """True iff `paths` is non-empty and every path in it is a derived
    artefact — the mechanical-resolution eligibility test. `None` (could not
    enumerate) and an empty set both read as "not eligible": the caller must
    never resolve a conflict it could not confirm is derived-only."""
    return bool(paths) and paths <= DERIVED_ARTEFACTS


def _normalized_classification_lines(text: str) -> list[str]:
    """`text` split into lines with every rule line's win-count digits
    replaced by a single `#` placeholder — everything else (verb, both
    spacing runs, pattern, and every non-rule line) kept byte-for-byte. Used
    only for the strict textual half of `classification_count_only`: two
    files whose rule lines match under this normalisation differ in COUNT
    ONLY, never in alignment/spacing — a whitespace reflow of the count
    changes `sp1`/`sp2` and so still compares unequal."""
    out: list[str] = []
    for line in text.splitlines():
        m = _RULE_LINE_RE.match(line)
        if m:
            out.append(f"{m['verb']}{m['sp1']}#{m['sp2']}{m['pattern']}")
        else:
            out.append(line)
    return out


async def classification_count_only(repo_path: str, base_tip_sha: str,
                                     branch: str) -> bool:
    """True iff `EXPORT_CLASSIFICATION.txt` differs between `base_tip_sha`
    and `branch` ONLY in the numeric win-count of otherwise-identical rule
    lines — never a pattern, a verb, a comment, an added/removed/reordered
    rule line, or a whitespace reflow. This is a conflict-SHAPE test: it says
    nothing about whether the file is currently a git conflict (the caller,
    `mechanically_resolvable`, combines this with the conflicting-paths set).

    Two independent checks must both pass:
      1. The DECISION sequence — `pr_watcher.classification_decisions`, which
         already elides each rule's count — is identical on both sides. Reused
         as-is, never reparsed here.
      2. A strict textual check (`_normalized_classification_lines`) that
         additionally requires identical line count, identical order, and
         identical whitespace everywhere except the count digits themselves —
         closing the hole check 1's `str.split()` leaves open for a pure
         spacing reflow.

    Fails closed (`False`) on any git failure, an absent file, or an empty
    read on either side — the same doctrine as `all_derived`'s `None`-is-
    ineligible: an unknown conflict shape is never treated as count-only.
    """
    decisions_base = await classification_decisions(repo_path, base_tip_sha)
    decisions_branch = await classification_decisions(repo_path, branch)
    if decisions_base is None or decisions_branch is None:
        return False
    if decisions_base != decisions_branch:
        return False

    rc_base, text_base = await _git_rc(repo_path, "show",
                                        f"{base_tip_sha}:{CLASSIFICATION_NAME}")
    rc_branch, text_branch = await _git_rc(repo_path, "show",
                                            f"{branch}:{CLASSIFICATION_NAME}")
    if rc_base != 0 or rc_branch != 0 or not text_base or not text_branch:
        return False

    return (_normalized_classification_lines(text_base)
            == _normalized_classification_lines(text_branch))


async def mechanically_resolvable(repo_path: str, paths: set[str] | None,
                                  base_tip_sha: str,
                                  branch: str) -> frozenset[str] | None:
    """The eligible-for-mechanical-resolution artefact set for THIS conflict,
    or `None` when no mechanical resolution applies. `DERIVED_ARTEFACTS` for
    the existing manifest-only case — decided by `all_derived` alone, no new
    git calls, exactly the same hot path as before this function existed.
    `DERIVED_ARTEFACTS | {CLASSIFICATION_NAME}` when the conflict is confined
    to the classification file (alone, or together with the manifest) AND
    `classification_count_only` confirms every differing rule line in it is a
    count-only edit. `None` on anything else, including `paths` itself being
    `None`/empty (could not enumerate) — fail closed, the caller must never
    resolve a conflict it could not confirm is one of these two shapes."""
    if not paths:
        return None
    if all_derived(paths):
        return DERIVED_ARTEFACTS
    eligible = DERIVED_ARTEFACTS | {CLASSIFICATION_NAME}
    if paths <= eligible and CLASSIFICATION_NAME in paths:
        if await classification_count_only(repo_path, base_tip_sha, branch):
            return eligible
    return None


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
                             remote: str = "origin", *,
                             eligible: frozenset[str] = DERIVED_ARTEFACTS,
                             ) -> DerivedResolution:
    """Mechanically resolve a PR conflict already confirmed (by the caller,
    via `all_derived` or `mechanically_resolvable`) to be confined to
    `eligible`: merge the base tip into a detached worktree of the branch,
    take either side of the eligible files and regenerate/reconcile them from
    the merged tree, verify, and push — no coder session. See the module
    docstring for why this exists and docs/PLAN.md's step-by-step for the
    exact procedure this implements.

    `eligible` defaults to `DERIVED_ARTEFACTS` (today's manifest-only case,
    unchanged behaviour); the caller passes
    `DERIVED_ARTEFACTS | {CLASSIFICATION_NAME}` when `mechanically_resolvable`
    confirmed the conflict is also eligible by the count-only shape rule.

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
            base_tip_sha=base_tip_sha, eligible=eligible,
        )
    finally:
        _cleanup_worktree(repo, tmp_dir)


def _resolve_in_worktree(*, repo: GitRepo, worktree_path: Path, remote: str,
                         branch: str, base_tip_sha: str,
                         eligible: frozenset[str] = DERIVED_ARTEFACTS,
                         ) -> DerivedResolution:
    branch_tip_sha = _sh(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    # -- step 3: merge --------------------------------------------------- #
    # rc != 0 is expected (that is the conflict this whole module exists to
    # resolve); rc 0 with nothing left conflicted is also fine (someone else
    # resolved it between enumeration and now) — continue either way. But
    # never resolve a conflict this routine did not enumerate: if anything
    # OUTSIDE `eligible` is still conflicted (the base moved in a way that
    # changed the shape of the conflict), bail — a coder round handles that,
    # not this one.
    _sh(["git", "merge", "--no-edit", base_tip_sha], cwd=worktree_path)
    unmerged = _unmerged_paths(worktree_path)
    outside = unmerged - eligible
    if outside:
        _sh(["git", "merge", "--abort"], cwd=worktree_path)
        return DerivedResolution(
            ok=False, step="merge",
            detail=_cap("merge produced conflict(s) outside the derived set "
                        f"(base moved?): {sorted(outside)}"))

    # -- step 4: take either side of the eligible files. For the manifest
    # this is always safe (it is purely regenerated below). For the
    # classification file it is safe ONLY because eligibility already proved
    # (via `classification_count_only`) that both sides carry the identical
    # decisions — the only thing left to differ is the win-count, and that
    # gets repaired by merge arithmetic further down, never guessed here. --
    for path in sorted(unmerged & eligible):
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
        if p.strip() and p.strip() not in eligible
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
            # Wherever a repo SHIPS its classification file it is pinned, and
            # the rewrite stales that pin — re-pin it or step-7 verify refuses.
            # (This repo drops the file, so here it is a no-op; the land
            # fixture ships it and covers the path.)
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
    names_to_add = set(DERIVED_ARTEFACTS)
    if CLASSIFICATION_NAME in eligible:
        names_to_add.add(CLASSIFICATION_NAME)
    for name in sorted(names_to_add):
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
        combined = verify_proc.stdout + verify_proc.stderr
        # Backstop reconcile hop: this fires only when the classification
        # file was ITSELF a conflicted path (so step 4 took `--ours` on it
        # and the resulting declared count is stale) and the step-5
        # approve-step hook above never ran (e.g. `shipped_changed` was
        # empty). `verify` is the last gate before push, so it is the last
        # place left to catch a count drift that is still merge arithmetic.
        # Bounded to exactly one pass — `reconciled` guards against looping.
        if (not reconciled and CLASSIFICATION_NAME in eligible
                and COUNT_DRIFT_RE.search(combined)):
            ok, note = reconcile_merge_count_drift(
                worktree_path, base_tip_sha, branch_tip_sha, combined)
            if not ok:
                return DerivedResolution(
                    ok=False, step="verify", unpinned=unpinned,
                    detail=_cap(f"{CLASSIFICATION_NAME} count drift is not merge "
                                f"arithmetic ({note}):\n{combined}"))
            reconciled = note
            retry_targets = _ship_classified_paths(worktree_path, [CLASSIFICATION_NAME])
            if retry_targets:
                approve_proc = _run_export_guard(
                    worktree_path, ["approve", *retry_targets], timeout=_APPROVE_TIMEOUT_S)
                if approve_proc is None:
                    return DerivedResolution(
                        ok=False, step="regenerate", unpinned=unpinned,
                        detail=f"export_guard approve timed out after "
                               f"{_APPROVE_TIMEOUT_S}s (after post-verify count "
                               f"reconcile: {note})")
                if approve_proc.returncode != 0:
                    return DerivedResolution(
                        ok=False, step="regenerate", unpinned=unpinned,
                        detail=_cap(f"export_guard approve refused after post-"
                                    f"verify count reconcile "
                                    f"({approve_proc.returncode}):\n"
                                    + approve_proc.stdout + approve_proc.stderr))
            add = _sh(["git", "add", "--", CLASSIFICATION_NAME], cwd=worktree_path)
            if add.returncode != 0:
                return DerivedResolution(ok=False, step="verify", unpinned=unpinned,
                                          detail=_cap(add.stderr))
            status = _sh(["git", "status", "--porcelain"], cwd=worktree_path).stdout
            if status.strip():
                amend = _sh(["git", "commit", "--amend", "--no-edit"], cwd=worktree_path)
                if amend.returncode != 0:
                    return DerivedResolution(
                        ok=False, step="commit", unpinned=unpinned,
                        detail=_cap(amend.stdout + "\n" + amend.stderr))
                merge_sha = _sh(["git", "rev-parse", "HEAD"],
                                 cwd=worktree_path).stdout.strip()
            verify_proc = _run_export_guard(worktree_path, ["verify"],
                                             timeout=_VERIFY_TIMEOUT_S)
            if verify_proc is None:
                return DerivedResolution(
                    ok=False, step="verify", unpinned=unpinned,
                    detail=f"export_guard verify timed out after "
                           f"{_VERIFY_TIMEOUT_S}s (after post-verify count "
                           f"reconcile: {note})")
            combined = verify_proc.stdout + verify_proc.stderr
        if verify_proc.returncode != 0:
            return DerivedResolution(
                ok=False, step="verify", unpinned=unpinned,
                detail=_cap(combined))

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
