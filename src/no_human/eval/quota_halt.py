"""Detect a bench run walled by quota saturation, mid-run — not after.

The 09-02 incident: a full-corpus run hit the subscription quota wall after
~64 measured specs, then burned through the remaining 72 recording each as
`outcome_status="crashed"` with every token field 0, and *completed* —
finalizing away its own `progress.json` checkpoint. `--resume` had nothing to
resume: the only recovery was re-running the whole corpus, re-spending
everything the walled run already spent.

A genuine crash (a bad spec, a sandbox setup failure) is NOT guaranteed to
burn tokens before it dies — `bench_run`'s own crash handler (out of scope
here) hardcodes every token field to 0 on ANY exception, quota wall or not.
So "ran, zero tokens" alone cannot tell the two apart; a corpus of specs
broken for an unrelated reason (a bad repo pin, a corrupted clone, a tamper
trip) would crash the same way and — left unchecked — trip this same halt,
mislabel it `quota_saturation`, and `--resume` would just re-run the same
broken specs into the same halt forever, since nothing about them ever
changes. The one signal that DOES survive onto a crashed `BenchScore` is its
`notes` text (`str(exc)`, redacted) — so a zero-token crash only counts
toward the streak when that text carries the quota wall's own wording.

Three-in-a-row *quota-shaped* zero-spend is the quota wall, not the model.
`QuotaHaltDetector` is the pure, unit-testable rule that tells `bench_run`
when to stop launching new specs and leave the checkpoint holding only what
was actually scored.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .northstar_card import score_did_priced_work, score_ran

QUOTA_HALT_CONSECUTIVE_DEAD = 3     # the one knob; NO config surface
HALTED_REASON_QUOTA = "quota_saturation"

# The SDK CLI's own wording for the exact 09-02 incident this module exists
# for ("Stream closed" — the bundled binary's Bun runtime dying mid-stream;
# see `commands.py`'s crash-handler comment and `agent/claude_backend.py`'s
# `_TRANSPORT_FAILURE_MARKERS`, which this deliberately mirrors). Kept as its
# own local tuple rather than imported: `claude_backend`'s check runs on a
# structured `AgentResult` and corroborates the marker against `is_error`/
# `stop_reason`; a bench crash note is a bare, already-redacted `str(exc)`
# with no such structure to corroborate against, so the two checks can't
# share code, only wording.
_QUOTA_CRASH_MARKERS = ("stream closed", "connection error")


def _is_quota_shaped_crash(score: Any) -> bool:
    """Does this zero-token crash carry the quota wall's own signature?

    Every OTHER field on a crashed `BenchScore` is hardcoded to 0 regardless
    of cause (see module docstring), so `notes` — the only place any cause
    survives — is the only thing left to check. A crash whose note doesn't
    match is a real, unrelated failure: it neither extends nor breaks the
    streak (like a skip), so a broken (non-quota) corpus never gets
    mislabeled `quota_saturation` and never gets trapped resuming into itself.
    """
    notes = (getattr(score, "notes", "") or "").lower()
    return any(marker in notes for marker in _QUOTA_CRASH_MARKERS)


@dataclass
class QuotaHaltDetector:
    """Tracks a running streak of ran-but-zero-priced-token spec results.

    ``observe()`` is called once per completed spec, under the same lock that
    saves the checkpoint. Once ``threshold`` land in a row, the detector
    ``stopped`` and freezes: nothing observed afterward can un-stop it or
    resurrect a row already marked ``dropped``.
    """

    threshold: int = QUOTA_HALT_CONSECUTIVE_DEAD
    stopped: bool = False
    reason: str = ""
    streak: list[tuple[str, int]] = field(default_factory=list)
    dropped: set[tuple[str, int]] = field(default_factory=set)

    def observe(self, score: Any) -> bool:
        """Record one spec's result. Returns True exactly on the transition
        into ``stopped`` — never again afterward."""
        if not score_ran(score):
            return False  # a skip never ran; it neither extends nor breaks
        key = (score.task_id, score.trial)
        if score_did_priced_work(score):
            if not self.stopped:
                for k in self.streak:
                    self.dropped.discard(k)
                self.streak = []
            return False
        # Ran, spent nothing across every token class — but that shape alone
        # is also a plain setup/sandbox crash, not just the quota wall (see
        # module docstring). Only a crash whose note carries the wall's own
        # wording counts: an unrelated crash is neither work nor a quota
        # sign, so — like a skip — it neither extends nor breaks the streak,
        # and it is never dropped from the checkpoint.
        if not _is_quota_shaped_crash(score):
            return False
        self.streak.append(key)
        self.dropped.add(key)
        if not self.stopped and len(self.streak) >= self.threshold:
            self.stopped = True
            self.reason = HALTED_REASON_QUOTA
            return True
        return False

    def scored(self, scores: list) -> list:
        """The rows a checkpoint (or the final card) should hold. Identity
        while not stopped — a normal run never loses a row. Once stopped,
        drops the zero-token rows so ``--resume`` re-runs them."""
        if not self.stopped:
            return list(scores)
        return [s for s in scores if (s.task_id, s.trial) not in self.dropped]

    def keep_or_clear(self, ckpt: Path) -> None:
        """Clean completion still unlinks the checkpoint; a quota halt leaves
        it in place so ``--resume`` has something to resume from."""
        if not self.stopped:
            ckpt.unlink(missing_ok=True)


def resume_command(*, full: bool, quick: bool, limit: int, specs_dir,
                    label: str, parallel: int, trials: int) -> str:
    """Rebuild the exact `nh bench run ... --resume` re-invocation, quoting
    any free-text operator input (label, specs-dir)."""
    parts = ["nh", "bench", "run"]
    if full:
        parts.append("--full")
    if quick:
        parts.append("--quick")
    if limit:
        parts += ["--limit", str(limit)]
    if specs_dir:
        parts += ["--specs-dir", shlex.quote(str(specs_dir))]
    if label:
        parts += ["--label", shlex.quote(label)]
    if parallel and parallel != 1:
        parts += ["--parallel", str(parallel)]
    if trials and trials != 1:
        parts += ["--trials", str(trials)]
    parts.append("--resume")
    return " ".join(parts)
