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
    max_turns_per_attempt: int = 60
    escalate_after: int = 3
    max_correction_rounds: int = 2

    @staticmethod
    def from_config(cfg: dict | None) -> "Bounds":
        cfg = cfg or {}
        return Bounds(
            max_attempts=cfg.get("max_attempts", 3),
            max_turns_per_attempt=cfg.get("max_turns_per_attempt", 60),
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

    Three detection layers (R2.3, AgentPatterns):
      1. **Edit-count per file** — same file edited ≥ ``edit_threshold`` times.
      2. **Doom-loop** — identical tool+input repeated consecutively.
      3. **Ping-pong** — A-B-A-B alternating pattern (R2.1, Broker).
    The hard iteration cap (``max_turns``) is Layer 3 — outside this class.
    """

    threshold: int = 2
    doom_loop_threshold: int = 3
    # R2.3 Layer 1: edit-count per file.
    edit_threshold: int = 5
    _seen: dict[str, int] = field(default_factory=dict)
    _last: str | None = None
    _tool_signatures: list[str] = field(default_factory=list)
    _consecutive_repeats: int = 0
    # R2.3 Layer 1: per-file edit counts.
    _edit_counts: dict[str, int] = field(default_factory=dict)

    def record(self, error_text: str) -> bool:
        """Record a failure. Return True if we are now stuck (reset context)."""
        sig = error_signature(error_text)
        self._seen[sig] = self._seen.get(sig, 0) + 1
        self._last = sig
        return self._seen[sig] >= self.threshold

    def record_tool_call(self, tool_name: str, tool_input_summary: str) -> bool:
        """Record a tool call signature. Return True if doom-looping.

        A doom-loop is the same tool+input repeated consecutively — the agent
        is retrying the exact same action expecting different results.
        """
        sig = f"{tool_name}:{tool_input_summary[:100]}"
        if self._tool_signatures and self._tool_signatures[-1] == sig:
            self._consecutive_repeats += 1
        else:
            self._consecutive_repeats = 1
        self._tool_signatures.append(sig)
        # Keep bounded
        if len(self._tool_signatures) > 50:
            self._tool_signatures = self._tool_signatures[-50:]
        return self._consecutive_repeats >= self.doom_loop_threshold

    def record_edit(self, file_path: str) -> bool:
        """R2.3 Layer 1: track per-file edit count. Return True if looping."""
        self._edit_counts[file_path] = self._edit_counts.get(file_path, 0) + 1
        return self._edit_counts[file_path] >= self.edit_threshold

    def detect_ping_pong(self) -> bool:
        """R2.1: detect A-B-A-B alternating pattern in last 4 tool calls."""
        sigs = self._tool_signatures
        if len(sigs) < 4:
            return False
        return (sigs[-4] == sigs[-2] and sigs[-3] == sigs[-1]
                and sigs[-4] != sigs[-3])

    @property
    def stuck_reason(self) -> str | None:
        """Return a human-readable reason if any detector fired, else None."""
        if self._consecutive_repeats >= self.doom_loop_threshold:
            return (
                f"doom-loop: identical tool call repeated "
                f"{self.doom_loop_threshold}× consecutively"
            )
        hot_files = [f for f, c in self._edit_counts.items()
                     if c >= self.edit_threshold]
        if hot_files:
            return (
                f"edit-loop: {hot_files[0]} edited {self._edit_counts[hot_files[0]]}× "
                f"— consider a different approach"
            )
        if self.detect_ping_pong():
            return "ping-pong: alternating between two actions (A-B-A-B pattern)"
        return None

    @property
    def health(self) -> dict[str, int]:
        """Return context-health signals for telemetry / the supervisor."""
        return {
            "unique_errors": len(self._seen),
            "consecutive_repeats": self._consecutive_repeats,
            "total_tool_calls": len(self._tool_signatures),
            "max_file_edits": max(self._edit_counts.values(), default=0),
            "ping_pong": int(self.detect_ping_pong()),
        }

    def is_repeat(self, error_text: str) -> bool:
        return error_signature(error_text) in self._seen

    def reset(self) -> None:
        self._seen.clear()
        self._last = None
        self._tool_signatures.clear()
        self._consecutive_repeats = 0
        self._edit_counts.clear()


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
