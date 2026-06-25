"""Phase 7.3/7.4: the concurrent task scheduler.

A single-event-loop pool that drains the SQLite queue into at most
``max_workers`` concurrent ``run_task`` coroutines. Concurrency is real because
the two long phases yield: the Agent SDK session is async, and the orchestrator
offloads the blocking test subprocess to a thread. Each task runs in its own git
worktree (see ``Orchestrator`` worktree mode), so same-repo tasks don't collide.

Two coordination rules:
  - **No double-dispatch.** A task id is reserved in ``_inflight`` synchronously
    before its coroutine is scheduled, so the next tick won't re-claim it (even
    though its DB status becomes IMPLEMENTING mid-run).
  - **Shared-quota gate (7.4).** All workers share one subscription. When any
    task parks PAUSED_QUOTA, the pool stops dispatching until the reset time —
    one worker hitting the limit pauses the whole pool, not just itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .db import Store
from .task import TaskStatus

log = logging.getLogger("no_human.scheduler")

# Tasks the scheduler may pick up: freshly created, or flipped back to
# IMPLEMENTING by the WakeWatcher / `nh reply` resume.
_CLAIMABLE = (TaskStatus.PENDING, TaskStatus.IMPLEMENTING)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Scheduler:
    def __init__(
        self,
        store: Store,
        orchestrator_factory: Callable[[], object],
        *,
        max_workers: int = 2,
        wake_watcher: object | None = None,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.store = store
        self.factory = orchestrator_factory
        self.max_workers = max(1, int(max_workers))
        self.wake = wake_watcher
        self._inflight: set[str] = set()
        self._on_event = on_event or (lambda kind, text: None)
        self._quota_cooldown_until: datetime | None = None
        # Per-task event log: task_id -> deque of {ts, source, kind, text, ...}
        self._event_log: dict[str, deque] = {}
        self._MAX_EVENTS = 200

    @property
    def inflight(self) -> set[str]:
        return set(self._inflight)

    def _in_quota_cooldown(self, now: datetime) -> bool:
        return self._quota_cooldown_until is not None and now < self._quota_cooldown_until

    async def _claimable(self) -> list:
        out = []
        for status in _CLAIMABLE:
            for t in await self.store.list_tasks(status):
                if t.id not in self._inflight:
                    out.append(t)
        return out

    async def tick(self, *, now: datetime | None = None) -> list[str]:
        """One scheduling pass: resume parked tasks, then dispatch up to the free
        slots. Returns the task ids started this tick."""
        now = now or datetime.now(timezone.utc)
        if self.wake is not None:
            try:
                await self.wake.tick(now=now)
            except Exception as exc:  # noqa: BLE001 — watcher must not kill the pool
                log.warning("wake tick failed: %s", exc)

        if self._in_quota_cooldown(now):
            return []  # 7.4: pool-wide pause until the subscription resets

        slots = self.max_workers - len(self._inflight)
        if slots <= 0:
            return []
        started: list[str] = []
        for task in (await self._claimable())[:slots]:
            self._inflight.add(task.id)          # reserve BEFORE scheduling
            asyncio.ensure_future(self._run(task))
            started.append(task.id)
        if started:
            self._on_event("dispatch", f"started {len(started)} task(s); "
                           f"{len(self._inflight)}/{self.max_workers} busy")
        return started

    def task_events(self, task_id: str) -> list[dict]:
        """Return captured events for a task (most recent last)."""
        return list(self._event_log.get(task_id, []))

    async def _run(self, task) -> None:
        # Set up per-task event capture.
        buf = deque(maxlen=self._MAX_EVENTS)
        self._event_log[task.id] = buf

        def _sink(event):
            event["ts"] = time.time()
            event["task_id"] = task.id
            buf.append(event)

        try:
            orch = self.factory()
            orch._sink = _sink
            outcome = await orch.run_task(task)
            # 7.4: a quota park pauses the whole pool until the reset time.
            if outcome is not None and outcome.status == TaskStatus.PAUSED_QUOTA:
                resets = _parse_iso(getattr(outcome.task, "wake_check_at", None))
                if resets is not None:
                    self._quota_cooldown_until = resets
                    self._on_event("quota_pause",
                                   f"pool paused until {resets.isoformat()}")
        except Exception as exc:  # noqa: BLE001 — one task must not kill the pool
            import sys, traceback
            print(f"[scheduler] task {task.id[:8]} crashed: {exc}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            log.warning("task %s crashed in pool: %s", task.id[:8], exc)
            self._on_event("task_error", f"{task.id[:8]}: {exc}")
            # Mark the task as FAILED so it doesn't stay stuck.
            try:
                from .task import TaskStatus as _TS
                await self.store.set_status(task, _TS.FAILED, validate=False)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._inflight.discard(task.id)

    async def run_forever(
        self, *, stop: asyncio.Event, poll_interval: float = 10.0
    ) -> None:
        """Loop until ``stop`` is set, then drain in-flight tasks."""
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
        await self.drain()

    async def drain(self) -> None:
        """Wait for all in-flight tasks to finish (best-effort, bounded poll)."""
        while self._inflight:
            await asyncio.sleep(0.05)
