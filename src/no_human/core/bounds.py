"""Termination bounds and stuck detection (PLAN.md 4.3, constraint §3.5).

These are hard, enforced limits — not advisory. They keep the loop from
doom-looping on an impossible task or stacking corrections on a stale context.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class Bounds:
    """Per-task / per-attempt caps, sourced from config ``bounds``."""

    max_attempts: int = 3
    max_turns_per_attempt: int = 40
    escalate_after: int = 3
    max_correction_rounds: int = 2

    @staticmethod
    def from_config(cfg: dict | None) -> "Bounds":
        cfg = cfg or {}
        return Bounds(
            max_attempts=cfg.get("max_attempts", 3),
            max_turns_per_attempt=cfg.get("max_turns_per_attempt", 40),
            escalate_after=cfg.get("escalate_after", 3),
            max_correction_rounds=cfg.get("max_correction_rounds", 2),
        )


def error_signature(text: str) -> str:
    """Reduce an error/output blob to a stable signature for stuck detection.

    Strips volatile tokens (hex ids, line/col numbers, timestamps, paths) so
    that "the same error twice" is recognized even when incidental details
    differ. Two genuinely-identical failures hash equal; progress changes it.
    """
    norm = text.lower()
    norm = re.sub(r"0x[0-9a-f]+", "<hex>", norm)
    norm = re.sub(r"\b[0-9a-f]{8,}\b", "<hash>", norm)
    norm = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}", "<ts>", norm)
    norm = re.sub(r":\d+(:\d+)?", ":<n>", norm)        # file:line:col
    norm = re.sub(r"/[^\s'\"]+", "<path>", norm)        # absolute paths
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


@dataclass
class StuckDetector:
    """Tracks repeated error signatures within an attempt.

    Per §3.5: the *same* error signature seen twice means zero progress — the
    correct response is to reset context in a fresh session, not to keep
    appending corrections to a stale one.
    """

    threshold: int = 2
    _seen: dict[str, int] = field(default_factory=dict)
    _last: str | None = None

    def record(self, error_text: str) -> bool:
        """Record a failure. Return True if we are now stuck (reset context)."""
        sig = error_signature(error_text)
        self._seen[sig] = self._seen.get(sig, 0) + 1
        self._last = sig
        return self._seen[sig] >= self.threshold

    def is_repeat(self, error_text: str) -> bool:
        return error_signature(error_text) in self._seen

    def reset(self) -> None:
        self._seen.clear()
        self._last = None


class QuotaExhausted(Exception):
    """Raised when a subscription usage limit is hit mid-task.

    The orchestrator catches this and parks the task in ``paused_quota`` rather
    than failing it; the watcher resumes when quota refreshes. Carries an
    optional ISO timestamp for when quota is expected back.
    """

    def __init__(self, message: str = "subscription quota exhausted",
                 resets_at: str | None = None):
        super().__init__(message)
        self.resets_at = resets_at
