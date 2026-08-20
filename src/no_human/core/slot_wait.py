"""The "waiting for a worker slot" concept, in one place.

A resumed task (`wake._resume`) is written IMPLEMENTING before any worker is
attached — deliberately (see `blockers/wake.py`). When the pool is full, the
scheduler leaves it unclaimed rather than mislabel it. That silence is
by design; the task's RECORD staying silent about it is not. This module
defines the one event kind that says so, and the pure predicate that reads it
back from a task's event log.

WHAT ENDS A WAIT (review round 2 of PR #525): the first sign that a worker
has the task — ANY run-sourced event after the wait (`repo_config`,
`state: context`, `attempt_start`, a coder tool call, ...). The first version
ended a wait only on `attempt_start`, so a PENDING task that got a slot read
"waiting for a worker slot" through its whole CONTEXT/PLANNING phase — the
lie in the opposite direction. Watcher ticks and human verbs are not a
worker: they never end a wait.

A wait is also only ever OPEN on a task the scheduler could still claim
(`CLAIMABLE_STATUSES`): a task that waited, then parked at a gate or ended,
is not waiting, whatever its last events say.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

KIND = "waiting_for_slot"

#: Event sources that mean a worker/run is acting on the task. An event from
#: any of these, of any kind other than the wait itself, ends the wait.
RUN_SOURCES = frozenset({"orchestrator", "agent", "reviewer", "scheduler"})

#: The statuses the scheduler claims from (`Scheduler._CLAIMABLE`). A wait can
#: only be open while the task sits in one of them.
CLAIMABLE_STATUSES = frozenset({"implementing", "pending"})


def waiting_text(busy: int, total: int, since_iso: str) -> str:
    return f"waiting for a worker slot ({busy}/{total} busy) since {since_iso}"


def ends_wait(event: Mapping) -> bool:
    """Is this event a worker acting on the task (so any open wait is over)?"""
    return (event.get("kind") != KIND
            and str(event.get("source") or "") in RUN_SOURCES)


def is_waiting_for_slot(events: Iterable[Mapping], *,
                        status: str | None = None) -> bool:
    """True when the newest wait-relevant event is the wait itself and the
    task is still claimable. ``events`` arrives oldest -> newest
    (`Store.list_events`). ``status`` is the task's current status value;
    when given and not claimable the answer is False regardless of events.

    Empty, or a task whose event log carries neither a wait nor a run event,
    is False.
    """
    if status is not None and str(status) not in CLAIMABLE_STATUSES:
        return False
    last: str | None = None
    for event in events:
        kind = event.get("kind")
        if kind == KIND:
            last = KIND
        elif ends_wait(event):
            last = "run"
    return last == KIND
