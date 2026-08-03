"""Which pool worker is running the current asyncio task, and how many peers.

WHY THIS EXISTS, stated plainly so nobody re-derives it: on 2026-07-11 a 3-way
parallel run plus the operator's own outer agent session killed the REVIEWER's
nested Agent-SDK subprocess with "Stream closed", and the pool was dropped back
to one worker. Reconstructing that afterwards was impossible from the database:
every reviewer session records its model, turns and tokens, and NOT ONE records
how many sibling sessions were live at the same instant. The failure was
therefore un-attributable to concurrency by anything except the operator's
memory — which is exactly the kind of signal this codebase says not to trust
(constraint #4). This module is the missing datum, nothing more.

It is a :class:`contextvars.ContextVar`, not a module global, because the pool
is N coroutines on ONE event loop (``core/scheduler.py`` dispatches with
``asyncio.ensure_future``). A global would be last-writer-wins across
concurrent workers and would name the WRONG worker in the one report that
exists to name the right one. A ContextVar is copied into each task at creation
and is per-task from then on, which is the exact shape of the pool.

Reading it is always optional: every consumer must work unchanged when no
context has been set (a single ``nh run``, a unit test, the CLI's one-shot
paths). "Unknown" is a legitimate answer and is rendered as one.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerContext:
    """One pool worker's identity and the concurrency it was dispatched into.

    ``inflight`` is the number of tasks the scheduler had claimed at dispatch
    time INCLUDING this one, so a single-worker pool reads 1 of 1. It is a
    snapshot, not a live gauge: a session that runs for ten minutes may see
    peers start and finish. That is deliberate — the question a post-mortem
    asks is "what was this dispatched into", and a live gauge read at failure
    time answers a different one.
    """

    worker: str
    inflight: int
    max_workers: int

    def describe(self) -> str:
        return (
            f"worker {self.worker}, dispatched at {self.inflight} of "
            f"{self.max_workers} pool slot(s) busy"
        )


_CURRENT: contextvars.ContextVar[WorkerContext | None] = contextvars.ContextVar(
    "no_human_worker_context", default=None
)


def set_worker_context(ctx: WorkerContext | None) -> None:
    """Bind (or clear) the calling asyncio task's worker context."""
    _CURRENT.set(ctx)


def current_worker_context() -> WorkerContext | None:
    """The calling task's worker context, or None outside a pool."""
    return _CURRENT.get()


def describe_concurrency() -> str:
    """A human-readable concurrency phrase for an error message.

    Never raises and never returns an empty string: an unattributed failure
    must say that it is unattributed rather than read as a single-worker run,
    because "no context" and "one worker" are different facts and only one of
    them exonerates parallelism.
    """
    ctx = current_worker_context()
    if ctx is None:
        return "no worker context recorded (not dispatched by the pool)"
    return ctx.describe()
