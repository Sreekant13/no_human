"""Queue health: stuck detection + drain ETA (D2 #4, watchtower `health.py`).

Zero LLM cost — pure task timestamps. Two questions the unattended operator
actually has, neither of which the board could answer:

- **Is the queue stuck?** Open work exists AND nothing has completed in
  ``stuck_after_minutes``. (Watchtower's definition, adapted: no completion in
  the window, not "no activity" — a task can look busy while looping.)
- **When will it drain?** From the completion rate in a recent window, not a
  guess: ``ETA = open / (completed_in_window / window)``. Unknown when the
  window is empty (say so rather than inventing a number).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Statuses that mean "the queue still owes the operator work".
OPEN_STATUSES = ("pending", "implementing", "reviewing")
# Statuses that mean the task reached a HUMAN gate — the queue's job is done;
# a board full of these is success, not a stall.
GATE_STATUSES = ("awaiting_approval", "escalated", "awaiting_input", "blocked")
DONE_STATUSES = ("done", "failed")


@dataclass
class QueueHealth:
    open_tasks: int = 0
    at_gate: int = 0
    completed_in_window: int = 0
    window_minutes: int = 30
    stuck: bool = False
    stuck_reason: str = ""
    eta_minutes: float | None = None   # None = unknowable, not "zero"

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_tasks": self.open_tasks,
            "at_gate": self.at_gate,
            "completed_in_window": self.completed_in_window,
            "window_minutes": self.window_minutes,
            "stuck": self.stuck,
            "stuck_reason": self.stuck_reason,
            "eta_minutes": (round(self.eta_minutes, 1)
                            if self.eta_minutes is not None else None),
        }


def _iso_cutoff(minutes: int, *, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    return (base - timedelta(minutes=minutes)).isoformat()


async def queue_health(
    store: Any, *, stuck_after_minutes: int = 30, window_minutes: int = 30,
    now: datetime | None = None,
) -> QueueHealth:
    db = store.db

    async def count(sql: str, *args) -> int:
        cur = await db.execute(sql, args)
        row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    open_q = ",".join("?" * len(OPEN_STATUSES))
    gate_q = ",".join("?" * len(GATE_STATUSES))
    done_q = ",".join("?" * len(DONE_STATUSES))

    h = QueueHealth(window_minutes=window_minutes)
    h.open_tasks = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({open_q})", *OPEN_STATUSES)
    h.at_gate = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({gate_q})", *GATE_STATUSES)

    cutoff = _iso_cutoff(window_minutes, now=now)
    h.completed_in_window = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({done_q}) "
        "AND updated_at >= ?", *DONE_STATUSES, cutoff)

    if h.open_tasks == 0:
        return h  # nothing owed → never stuck, ETA 0 is meaningless

    stuck_cutoff = _iso_cutoff(stuck_after_minutes, now=now)
    recent_completion = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({done_q}) "
        "AND updated_at >= ?", *DONE_STATUSES, stuck_cutoff)
    if recent_completion == 0:
        h.stuck = True
        h.stuck_reason = (
            f"{h.open_tasks} task(s) open and nothing has completed in "
            f"{stuck_after_minutes} minutes")

    if h.completed_in_window > 0:
        rate_per_min = h.completed_in_window / window_minutes
        h.eta_minutes = h.open_tasks / rate_per_min
    return h
