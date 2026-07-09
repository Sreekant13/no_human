"""Incremental event persistence.

Two things run tasks — the scheduler pool (`nh start`) and the CLI in-process
path (`nh task add --run`, `nh reply --run`) — and both must land their events
in SQLite *while the run is live*. Persisting once at the end lost the whole
history to any crash; persisting from only one of the two call sites meant a
`--run` task showed up on the board as "Waiting for events…" forever.

Two rules this must honour:

- ``Store.save_events`` INSERTs. Only the unflushed tail may be handed to it, or
  every event is written once per flush.
- Persistence must never break the run. A failed batch is dropped with a
  warning, never re-raised and never retried into unbounded memory.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("no_human.events")


class EventPersister:
    """Buffer events from a synchronous sink; flush them to SQLite on a timer.

    Use as an async context manager: the flusher starts on enter and, on exit,
    is stopped and drained so nothing recorded is lost.
    """

    def __init__(self, store: Any, task_id: str, *, interval: float = 2.0) -> None:
        self._store = store
        self._task_id = task_id
        self._interval = interval
        self._pending: list[dict] = []
        self._stop = asyncio.Event()
        self._flusher: asyncio.Task | None = None

    def record(self, event: dict) -> None:
        """Queue one event. Safe to call from a sync sink — never blocks."""
        self._pending.append(event)

    async def flush(self) -> None:
        """Write the queued tail. Never raises."""
        if not self._pending:
            return
        batch, self._pending[:] = list(self._pending), []
        try:
            await self._store.save_events(self._task_id, batch)
        except Exception:  # noqa: BLE001 — persistence must not break the run
            log.warning("failed to persist %d events for task %s",
                        len(batch), self._task_id[:8])

    async def _run(self) -> None:
        # Stopped by an event, not by cancel(): cancel() could land inside
        # save_events after a batch was taken off _pending, dropping it silently.
        # CancelledError is not an Exception, so flush() cannot absorb it.
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            await self.flush()

    def start(self) -> None:
        """Begin flushing on a timer. Must be called from a running event loop."""
        self._stop.clear()
        self._flusher = asyncio.create_task(self._run())

    async def aclose(self) -> None:
        """Stop the timer and write whatever it has not taken."""
        self._stop.set()
        if self._flusher is not None:
            await self._flusher
            self._flusher = None
        await self.flush()

    async def __aenter__(self) -> "EventPersister":
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
