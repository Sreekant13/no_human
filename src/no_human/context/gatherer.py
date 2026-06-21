"""Parallel context gathering with per-source timeouts (PLAN.md 4.2)."""

from __future__ import annotations

import asyncio
import logging

from ..core.task import Task
from .base import ContextChunk, ContextSource, TaskContext, check_completeness

log = logging.getLogger("no_human.context")


class ContextGatherer:
    def __init__(self, sources: list[ContextSource], *, per_source_timeout: float = 30.0):
        self.sources = sources
        self.timeout = per_source_timeout

    async def gather(self, task: Task) -> TaskContext:
        ctx = TaskContext()

        async def _one(src: ContextSource) -> tuple[str, list[ContextChunk] | Exception]:
            try:
                chunks = await asyncio.wait_for(src.gather(task), self.timeout)
                return src.name, chunks
            except asyncio.TimeoutError:
                return src.name, TimeoutError(f"{src.name} timed out after {self.timeout}s")
            except Exception as exc:  # noqa: BLE001 — one bad source must not abort the rest
                return src.name, exc

        results = await asyncio.gather(*(_one(s) for s in self.sources))
        for name, outcome in results:
            if isinstance(outcome, Exception):
                ctx.errors[name] = str(outcome)
                log.warning("context source %s failed: %s", name, outcome)
            else:
                ctx.chunks.extend(outcome)

        ctx.chunks.sort(key=lambda c: c.score, reverse=True)
        ctx.completeness = check_completeness(task, ctx)
        return ctx
