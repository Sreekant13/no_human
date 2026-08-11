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
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .git import CommitResult, GitError, GitRepo

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


def parse_manifest_refusal(text: str) -> list[str] | None:
    """Extract the changed-but-pinned paths from a manifest-gate refusal.

    Returns the refused paths only when *text* is the gate's
    changed-pinned-files refusal. Any other text — other hooks, other
    shapes — returns None so the caller fails honestly instead of repairing.
    """
    if _MANIFEST_REFUSAL_MARKER not in text:
        return None
    return _REFUSED_PIN_RE.findall(text) or None


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
            raise GitError(
                "manifest re-approve failed "
                f"({proc.returncode}): {proc.stderr.strip()[:500]}"
            ) from exc
        if on_repair is not None:
            on_repair(list(pinned), proc.stderr.strip())
        return _commit()
