"""Repair a manifest pre-commit refusal by running the gate's own FIX.

This lives in its OWN module deliberately: the egress allowlist can only
charge a dynamic exec (`sys.executable` on a runtime path) to a whole file,
and parking that wildcard on ``vcs/git.py`` would blind the gate to any
future dynamic exec added there (review finding F1, 2026-08-12). Here the
wildcard covers ~40 lines that do nothing else.

2026-08-11 incident: three tasks finished their work, passed their tests,
then died as ``task_crashed`` because the manifest pre-commit gate refused
the pipeline's own commit (changed pinned files, manifest not re-approved)
and the raw ``GitError`` propagated uncaught.

2026-08-12 incident (task 27e7352b): the fix above only *reacts* to the
pre-commit gate's refusal, and that gate is deliberately narrow — it blocks
an already-PINNED file whose content changed, never a brand-new
ship-classified file with no pin yet (``scripts/precommit_manifest_gate.py``
says so in its own docstring). A new file sails straight through it and
dies later, unpinned, in TESTING (``tests/test_pr_body_layout.py`` shipped
with no approval row). So there are now two steps, in order:

1. ``approve_pending_pins`` — PROACTIVE, runs before every commit attempt.
   It performs the guard's own ``approve --all --prune`` maintenance so any
   new-or-changed ship-classified file is pinned *in the same commit* as the
   file it pins. It never raises: a refusal here (a real leak candidate, or
   an unclassified file) is advisory only — the file stays unpinned and
   fails later, honestly, by name.
2. ``commit_with_manifest_repair`` — REACTIVE, unchanged: if the pre-commit
   gate still refuses (an already-pinned file the proactive pass could not
   or would not touch, e.g. a scan-refused approval), perform the gate's
   documented FIX for that one refusal and retry once.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .approve_merge import COUNT_DRIFT_RE, reconcile_commit_count_drift
from .git import CommitResult, GitError, GitRepo, _branch_protected

_logger = logging.getLogger(__name__)

# The manifest pre-commit gate's refusal header
# (scripts/precommit_manifest_gate.py). Matching on this exact marker keeps
# the repair path from firing on any other hook's failure.
_MANIFEST_REFUSAL_MARKER = "no_human pre-commit gate: REFUSED"

# One refused file in the gate's output: the path on its own indented line,
# the pin/staged hash pair on the next. Only the changed-pinned-file shape
# matches — an unclassified/unknown file is a ledger DECISION the pipeline
# must never make on its own (export_guard's approve refuses those anyway;
# verified against the real guard in review). Paths may contain spaces
# (`\S.*\S|\S`), though no shipped path currently does.
_REFUSED_PIN_RE = re.compile(r"^\s{2}(\S.*\S|\S)\n\s+pinned [0-9a-f]", re.MULTILINE)

# A bounded ceiling for the approve run: it re-scans each refused file
# locally (~0.24s/file measured) and dials nothing; minutes means wedged.
_APPROVE_TIMEOUT_S = 120

# `--all` re-hashes every ship-classified file that is new or changed since
# its last pin — a bigger sweep than the reactive path's named-file re-scan,
# so it gets a longer, but still bounded, ceiling.
_PREAPPROVE_TIMEOUT_S = 300

# scripts/export_guard.py's own output lines for a successful pin/prune
# (`_cmd_approve`: `print(f"approved  {digest[:12]}  {rel} ({state})")` and
# `print(f"pruned    {rel} (no longer ships)")`) — matched here rather than
# re-parsed from a second grammar, so this stays in lockstep with whatever
# the guard actually printed.
_APPROVED_RE = re.compile(r"^approved\s+[0-9a-f]+\s+(\S.*\S|\S) \(", re.MULTILINE)
_PRUNED_RE = re.compile(r"^pruned\s+(\S.*\S|\S) \(", re.MULTILINE)


def _stage_untracked_for_approve(repo: GitRepo, paths: list[str] | None) -> None:
    """Make brand-new files TRACKED before the guard looks at them.

    ``export_guard.py`` classifies its ship-set from ``git ls-files`` — the
    INDEX, not the working tree — and documents that an untracked file is
    invisible to it. ``approve_pending_pins`` runs before the commit that
    would normally stage a new file for the first time, so without this a
    brand-new ship-classified file sails through ``--all`` unpinned: the
    exact tests/test_pr_body_layout.py incident (task 27e7352b).

    This performs only the ADD half of what ``GitRepo.commit_paths`` /
    ``commit_all`` (git.py) already do at commit time — the same files,
    staged a little earlier so the guard can see them too. It is never a new
    decision about what gets committed: every path added here is one
    ``commit_paths``'s own sweep would have staged anyway a few lines later.
    """
    if not paths:
        repo.stage_all()
        return
    root = repo.path
    rel_paths: list[str] = []
    for p in paths:
        try:
            rel_paths.append(str(Path(p).resolve().relative_to(root)))
        except ValueError:
            continue  # outside the repo — commit_paths will skip it too
    untracked = repo._run(
        "ls-files", "--others", "--exclude-standard", check=False
    ).splitlines()
    for u in untracked:
        u = u.strip()
        if u and Path(u).suffix.lower() in GitRepo._CODE_EXTS:
            rel_paths.append(u)
    rel_paths = [r for r in dict.fromkeys(rel_paths)
                 if r and not GitRepo._is_ephemeral_path(r)]
    if rel_paths:
        repo._run("add", "--", *rel_paths, check=False)


def approve_pending_pins(
    repo: GitRepo,
    paths: list[str] | None,
    on_repair: Callable[[list[str], str], None] | None = None,
) -> None:
    """Proactively pin new/changed ship-classified files before the commit.

    Runs the guard's own documented maintenance command,
    ``export_guard.py approve --all --prune`` — never ``--acknowledge``:
    that flag is what would turn hash maintenance into leak laundering, and
    granting it is a ledger DECISION no pipeline may make on its own, the
    same doctrine as the reactive repair's unclassified refusal below.

    A no-op (no subprocess spawned) unless the target repo has the gate
    (``scripts/export_guard.py`` and ``RELEASE_MANIFEST.txt`` both exist),
    the working tree actually has changes to maintain, and the current
    branch is not protected (nothing here should touch the index on a
    branch the commit itself refuses to land on).

    Exit-code contract (``scripts/export_guard.py:_cmd_approve``), because
    this *is* the integration surface:

    * ``0`` — pins written for exactly the files whose content diverged,
      plus any pruned rows. Parsed and reported through ``on_repair`` so the
      ledger change is never silent (review finding: a prior version wrote
      the manifest on a run that ended non-zero without ever calling back).
    * ``1`` — one or more files refused on scan hits (a real leak
      candidate). Advisory only: never raise, retry, or acknowledge. But the
      files that WERE cleanly approved in the very same run still get
      reported through ``on_repair`` — that write already happened, and the
      only way to keep it off the ledger record would be to hide it. The
      refused file stays unpinned and fails the export tests by name later;
      the gate stays closed.
    * ``2`` — refused before any pin was written (unclassified file, a
      classification count mismatch, or the advisory scan unable to run).
      Nothing changed, so there is nothing to report through ``on_repair``;
      the guard's own FIX text is logged so it stays visible. Classification
      is a ledger DECISION; no pipeline makes it — same doctrine as the
      reactive repair's unclassified refusal.
    * ``TimeoutExpired`` / ``OSError`` — logged, never fails a commit that
      would otherwise succeed.
    """
    root = Path(repo.path)
    guard = root / "scripts" / "export_guard.py"
    if not guard.exists() or not (root / "RELEASE_MANIFEST.txt").exists():
        return
    if _branch_protected(repo.current_branch(), repo.never_push_to):
        return
    if not repo.has_changes():
        return

    _stage_untracked_for_approve(repo, paths)

    try:
        proc = subprocess.run(
            [sys.executable, str(guard), "approve", "--all", "--prune"],
            cwd=repo.path, capture_output=True, text=True,
            timeout=_PREAPPROVE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _logger.warning(
            "manifest pin maintenance timed out after %ss; continuing "
            "without it", _PREAPPROVE_TIMEOUT_S)
        return
    except OSError as exc:
        _logger.warning("manifest pin maintenance failed to start: %s", exc)
        return

    out = proc.stdout or ""
    approved = _APPROVED_RE.findall(out)
    pruned = _PRUNED_RE.findall(out)
    tail = (out.strip() + "\n" + (proc.stderr or "").strip()).strip()[:1000]

    if proc.returncode == 0:
        if (approved or pruned) and on_repair is not None:
            on_repair(approved, "pre-commit pin maintenance: " + tail[:500])
        return

    if proc.returncode == 1:
        _logger.warning(
            "manifest pin maintenance: %s file(s) refused on scan hits:\n%s",
            out.count("REFUSED"), tail)
        if (approved or pruned) and on_repair is not None:
            on_repair(
                approved,
                "pre-commit pin maintenance (partial — some file(s) refused "
                "on scan hits): " + tail[:500],
            )
        return

    # 2 (or anything else the guard might exit with): refused before any pin
    # was written. Nothing to report through on_repair — nothing changed.
    _logger.warning(
        "manifest pin maintenance: approve --all --prune refused (%s):\n%s",
        proc.returncode, tail)


def parse_manifest_refusal(text: str) -> list[str] | None:
    """Extract the changed-but-pinned paths from a manifest-gate refusal.

    Returns the refused paths only when *text* is the gate's
    changed-pinned-files refusal. Any other text — other hooks, other
    shapes — returns None so the caller fails honestly instead of repairing.
    """
    if _MANIFEST_REFUSAL_MARKER not in text:
        return None
    return _REFUSED_PIN_RE.findall(text) or None


# Substrings this module raises when its OWN reactive repair could not
# reconcile a refusal (`commit_with_manifest_repair`, below) — distinct from
# the pre-commit gate's own refusal header, but still the export/manifest
# gate declining a commit, not an unrelated hook failure.
_REAPPROVE_FAILURE_MARKERS = (
    "manifest re-approve failed",
    "manifest re-approve timed out",
)


def is_gate_refusal(text: str) -> bool:
    """True when *text* is the export/manifest gate declining a commit —
    either the pre-commit gate's own refusal header, or this module's
    re-approve failure raised when the repair could not reconcile it.

    A checkpoint caller uses this to decide whether an unrepairable refusal
    is safe to bypass for a `[WIP-*]` commit (the gate protects what SHIPS;
    a WIP checkpoint ships nothing). Anything else — a merge conflict, a
    permission error, an unrelated hook failure — returns False so those
    keep failing the checkpoint exactly as before.
    """
    if _MANIFEST_REFUSAL_MARKER in text:
        return True
    return any(marker in text for marker in _REAPPROVE_FAILURE_MARKERS)


def commit_with_manifest_repair(
    repo: GitRepo,
    paths: list[str] | None,
    message: str,
    on_repair: Callable[[list[str], str], None] | None = None,
) -> CommitResult:
    """Commit, and if the manifest pre-commit gate refuses because
    already-pinned files changed, perform the gate's own documented FIX
    (``export_guard.py approve <paths>`` — hash maintenance for files that
    are ALREADY classified ship; the re-derived manifest is staged by the
    retry's modified-tracked-files sweep) and retry ONCE.

    ``on_repair(approved_paths, approve_stderr)`` is called after a
    successful approve so the caller can put the pipeline-granted approval
    on the task's event record — the release ledger must never change
    silently (review finding F3).

    Anything else propagates ``GitError`` for the caller to turn into an
    honest attempt failure: an unclassified-file refusal never parses (and
    the target repo's guard refuses it besides), a failed or timed-out
    approve raises, a second refusal raises, and a missing guard script
    means this repo does not use the gate. ``ProtectedBranch`` passes
    through untouched — the repair never runs for it (nothing to parse).
    """
    approve_pending_pins(repo, paths, on_repair=on_repair)

    def _commit() -> CommitResult:
        if paths:
            return repo.commit_paths(list(paths), message)
        return repo.commit_all(message)

    try:
        return _commit()
    except GitError as exc:
        pinned = parse_manifest_refusal(str(exc))
        if not pinned:
            raise
        guard = Path(repo.path) / "scripts" / "export_guard.py"
        if not guard.exists():
            raise
        try:
            proc = subprocess.run(
                [sys.executable, str(guard), "approve", *pinned],
                cwd=repo.path, capture_output=True, text=True,
                timeout=_APPROVE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise GitError(
                f"manifest re-approve timed out after {_APPROVE_TIMEOUT_S}s"
            ) from exc
        if proc.returncode != 0:
            reconciled, reconciled_note = _try_reconcile_count_drift(repo, proc.stderr)
            if reconciled:
                try:
                    retry = subprocess.run(
                        [sys.executable, str(guard), "approve", *pinned],
                        cwd=repo.path, capture_output=True, text=True,
                        timeout=_APPROVE_TIMEOUT_S,
                    )
                except subprocess.TimeoutExpired:
                    raise GitError(
                        f"manifest re-approve timed out after {_APPROVE_TIMEOUT_S}s "
                        "(after a count-drift reconciliation)"
                    ) from exc
                if retry.returncode == 0:
                    if on_repair is not None:
                        on_repair(
                            list(pinned),
                            f"count drift reconciled: {reconciled_note}; "
                            + retry.stderr.strip(),
                        )
                    return _commit()
                raise GitError(
                    "manifest re-approve failed "
                    f"({retry.returncode}) even after reconciling the attempt's own "
                    f"count drift ({reconciled_note}): {retry.stderr.strip()[:500]}"
                ) from exc
            # The reconciler's refusal (when it ran) travels with the guard's:
            # a human reading the failed attempt must see that the safety net
            # ran and the arithmetic it declined on, not only the stale number.
            why_not = (
                f"; count-drift reconciliation declined: {reconciled_note}"
                if reconciled_note else ""
            )
            raise GitError(
                "manifest re-approve failed "
                f"({proc.returncode}): {proc.stderr.strip()[:500]}{why_not}"
            ) from exc
        if on_repair is not None:
            on_repair(list(pinned), proc.stderr.strip())
        return _commit()


def _try_reconcile_count_drift(
        repo: GitRepo, refusal_text: str) -> tuple[bool, str | None]:
    """When `export_guard.py approve`'s reactive re-scan refuses (2) with a
    win-COUNT that drifted, and that drift is fully explained by THIS
    ATTEMPT's own diff (one added/removed file, not a hand decision),
    rewrite the declared count and return the reconciliation note.

    Reuses `approve_merge.reconcile_commit_count_drift` — the same refusal
    parser, in-place rewriter and staging step as the merge-time reconciler
    (`reconcile_merge_count_drift`, PR #511) — for the commit-time
    arithmetic: `real - declared == added - removed` in `git diff
    --name-status HEAD`, not base+branch-ancestor arithmetic. ``HEAD`` is
    this attempt's own base: this reactive path only runs while creating a
    brand-new commit for the current round, so HEAD is the tip immediately
    before the round's changes.

    Returns ``(True, note)`` when the count was rewritten and staged.
    Returns ``(False, None)`` when the refusal names no count drift at all
    (nothing attempted), and ``(False, note)`` when the reconciler ran and
    declined — an unexplained drift, a duplicated rule, a rule the diff
    never touched, or any other hand decision — with ``note`` carrying its
    arithmetic. The caller raises the guard's own refusal and appends that
    note, so the failed attempt shows WHY the safety net did not apply.
    """
    if not COUNT_DRIFT_RE.search(refusal_text):
        return False, None
    ok, note = reconcile_commit_count_drift(Path(repo.path), "HEAD", refusal_text)
    return (True, note) if ok else (False, note)
