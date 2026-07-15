"""Termination bounds and stuck detection (PLAN.md 4.3, constraint §3.5).

These are hard, enforced limits — not advisory. They keep the loop from
doom-looping on an impossible task or stacking corrections on a stale context.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields


@dataclass
class Bounds:
    """Per-task / per-attempt caps, sourced from config ``bounds``."""

    max_attempts: int = 3
    # Measured on task 84251cb2 (docs/BASELINE_M0.md): 22 turns to reach the
    # reviewer, and >40 to act on its three findings — that run escalated
    # mid-fix at a cap of 40, having already committed a correct fix. A cap
    # that stops a nearly-finished attempt wastes the whole attempt's burn.
    #
    # Set to 500 by the user (2026-07-10): give a real task room to finish.
    # Know what this cap is and is not. It is NOT a cost control — it is the
    # last-resort stop for an agent that has stopped making progress. It is no
    # longer the only one: since ARCH_REVIEW B2 #1 the stuck detectors have a
    # HARD tier (doom_loop_abort/edit_abort below) that ends the attempt
    # deterministically, work checkpointed — the advisory tier still only
    # emits telemetry. Attempt 11 once burned 3.4M cache-read in 41 turns
    # looping on one file with ~12x that headroom here; that class of runaway
    # now aborts at the hard threshold instead of burning to this cap.
    max_turns_per_attempt: int = 500
    # P3 (megaplan): complex tasks (many files / large plan / decompose verdict)
    # get a larger turn budget so they don't exhaust turns mid-implementation and
    # fail with an empty diff (B5). Applied only to the complexity-flagged subset.
    complex_multiplier: float = 1.5
    # Lifetime caps across the task's WHOLE life, resumes included.
    # `max_attempts` bounds ONE loop; every `nh reply`/resume starts a fresh
    # loop, which is how task 84251cb2 reached attempt 17 and 21.2M cache-read
    # tokens without any cap ever firing. Exceeding either cap raises a
    # BUDGET_EXHAUSTED blocker whose option can raise the budget for that one
    # task — the human decides, the loop never silently continues. 9 attempts =
    # three full bounded loops. 8M tokens: the enterprise profile now bills PER
    # TOKEN, so the cap is a cost guardrail, not just a doom-loop stop. Measured
    # 2026-07-13 (29 tasks): the largest PR-producing task burned 6.15M and every
    # other success ≤1.71M — NO success sat above 6.15M. Six failed/parked tasks
    # sat above 8M (CI_GATE 61.5M + 10.18M, metrics-core 20.8M, and ordinary failed feature
    # tasks at 22.8M/15M/12.7M). So 8M clears every real success with headroom and
    # parks a runaway an order of magnitude sooner than the old 25M did (which the
    # 61.5M task blew past entirely, across resumes).
    lifetime_attempts: int = 9
    lifetime_tokens: int = 8_000_000

    @staticmethod
    def from_config(cfg: dict | None) -> "Bounds":
        """Config overrides; every default lives on the dataclass, once.

        This used to repeat each default as a literal here, so the dataclass,
        this function and ``DEFAULT_CONFIG`` were three places to change and
        two places to forget. Unknown keys are ignored rather than crashing on
        a hand-edited config.
        """
        known = {f.name for f in fields(Bounds)}
        return Bounds(**{k: v for k, v in (cfg or {}).items() if k in known})

    def turns_for(self, *, complex_task: bool = False) -> int:
        """Turn budget for one attempt. Complex tasks get
        ``max_turns_per_attempt × complex_multiplier`` (megaplan P3 / B5)."""
        base = self.max_turns_per_attempt
        if complex_task and self.complex_multiplier > 1:
            return int(round(base * self.complex_multiplier))
        return base


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
    # Hard-abort tier (ARCH_REVIEW B2 #1). The advisory thresholds above emit
    # telemetry; crossing a hard threshold ends the ATTEMPT (StuckAbort in the
    # orchestrator's sink — work checkpointed, bounded loop retries with fresh
    # context). Set far above the advisory tier so they fire only on
    # unambiguous runaways: 9 identical consecutive calls, one file edited
    # 15×, or 12 consecutive calls alternating between the same two actions.
    doom_loop_abort: int = 9
    edit_abort: int = 15
    ping_pong_abort_window: int = 12
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

    def detect_ping_pong(self, window: int = 4) -> bool:
        """R2.1: detect an A-B-A-B alternating pattern in the last ``window``
        tool calls (4 = advisory; ``ping_pong_abort_window`` = hard)."""
        sigs = self._tool_signatures
        if len(sigs) < window:
            return False
        tail = sigs[-window:]
        a, b = tail[0], tail[1]
        if a == b:
            return False
        return all(s == (a if i % 2 == 0 else b) for i, s in enumerate(tail))

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
    def hard_stuck_reason(self) -> str | None:
        """A reason iff a HARD threshold is crossed — the abort tier, not the
        advisory one. The orchestrator's sink raises StuckAbort on this."""
        if self._consecutive_repeats >= self.doom_loop_abort:
            return (
                f"doom-loop: identical tool call repeated "
                f"{self._consecutive_repeats}× consecutively"
            )
        hot = [(f, c) for f, c in self._edit_counts.items()
               if c >= self.edit_abort]
        if hot:
            path, count = max(hot, key=lambda fc: fc[1])
            return f"edit-loop: {path} edited {count}×"
        if self.detect_ping_pong(self.ping_pong_abort_window):
            return (
                f"ping-pong: alternating between two actions for "
                f"{self.ping_pong_abort_window} consecutive calls"
            )
        return None


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
