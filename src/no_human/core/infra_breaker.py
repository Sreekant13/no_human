"""Process-wide circuit breaker for consecutive zero-token SDK failures.

INCIDENT (2026-08-13 17:14-18:15): the operator's subscription hit its weekly
limit and the server kept dispatching. 7 tasks failed BUDGET_EXHAUSTED at
attempts 9/9 — four of them (79183501, 2893fa27, e58a81d6, edeff716) with ZERO
tokens across ALL 9 attempts, every dispatch dying in the SDK before doing any
work. `orchestrator._infra_sdk_failure` classifies each dead dispatch and
parks its OWN task in `paused_quota` — that is a per-task fix, one task at a
time. This module is the FLEET-WIDE half: three of those dead dispatches in a
row, across three DIFFERENT tasks, means the account or transport itself is
down, not three unlucky tasks, so the whole pool should stop dispatching
rather than feed the next queued task straight into the same wall.

A process singleton, not a per-orchestrator counter: the orchestrator is
constructed fresh per task (`Scheduler.factory`), so a counter living on an
orchestrator instance would reset every dispatch — the same "unbounded loop
wearing a cap's clothes" hazard the `_MAX_REVIEW_INFRA_PARKS` comment in
`orchestrator.py` already names for a sibling problem.
"""

from __future__ import annotations

# A safety bound, not a tuning knob (resolved intake answer, 2026-08-14):
# hardcoded rather than configurable, because a fleet-wide pause is a
# circuit breaker and misconfiguration risk outweighs operational
# flexibility. Low enough to catch a sustained SDK failure pattern quickly,
# high enough that a single unlucky task retrying across its own attempts
# does not, on its own, pause every other task in the fleet.
_BREAKER_THRESHOLD = 3

# Memory bound only — correctness only ever needs the last few entries once
# the streak has reached the threshold, since a tripped breaker stays tripped
# until `record_healthy()`/`reset()` clears it.
_MAX_TRACKED = 64


class InfraBreaker:
    """Trips when a CONSECUTIVE run of infra-classified failures (no healthy
    result and no `record_healthy()` in between) reaches `_BREAKER_THRESHOLD`
    and spans at least that many DISTINCT task ids.

    Distinct, not just consecutive-count: one task dying three times in a row
    is that task's problem (its own `max_attempts` loop already bounds the
    damage); three DIFFERENT tasks each dying zero-token in a row is the
    account's or the transport's, which is exactly what dispatching a fourth
    task into would repeat.
    """

    def __init__(self) -> None:
        self._recent_task_ids: list[str] = []

    def record_infra_failure(self, task_id: str) -> bool:
        """Record one infra-classified failure for `task_id`.

        Returns True iff this call is the one that JUST tripped the breaker
        (an edge, not a level) — callers use that to emit the fleet-pause
        event exactly once instead of on every subsequent infra failure while
        already tripped.
        """
        was_tripped = self.tripped() is not None
        self._recent_task_ids.append(task_id)
        if len(self._recent_task_ids) > _MAX_TRACKED:
            self._recent_task_ids = self._recent_task_ids[-_MAX_TRACKED:]
        return self.tripped() is not None and not was_tripped

    def record_healthy(self) -> None:
        """A result that streamed real tokens: the account/transport works,
        so whatever consecutive infra streak was building is over."""
        self._recent_task_ids.clear()

    def tripped(self) -> str | None:
        """The trip reason while tripped, else None."""
        if len(self._recent_task_ids) < _BREAKER_THRESHOLD:
            return None
        distinct = set(self._recent_task_ids)
        if len(distinct) < _BREAKER_THRESHOLD:
            return None
        return (
            f"{_BREAKER_THRESHOLD} consecutive zero-token/auth SDK failures "
            f"across {len(distinct)} distinct tasks — account or transport "
            "appears down, not the tasks"
        )

    def reset(self) -> None:
        """Full clear — tests use this so the process singleton cannot leak
        state between them."""
        self._recent_task_ids.clear()


_singleton = InfraBreaker()


def infra_breaker() -> InfraBreaker:
    """The process-wide breaker instance. A module-level singleton, not a
    per-orchestrator attribute — see the module docstring."""
    return _singleton
