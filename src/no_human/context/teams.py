"""Microsoft Teams context via the M365 connector (read-only).

The gatherer depends only on a small ``CommsClient`` interface: ``search(query,
limit) -> list[dict]``. In production a Graph-backed client (config token) is
injected; in tests/demo a client wrapping data pulled via the M365 MCP connector
is injected. This keeps comms read-only and the gatherer source-agnostic.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..core.task import Task
from .base import ContextChunk, keywords


class CommsClient(Protocol):
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        ...


class GraphTeamsClient:
    """Production client: Microsoft Graph search. Stub until a token is set."""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = (config or {}).get("context", {}).get("m365", {})
        self.token = cfg.get("token")

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.token:
            raise RuntimeError(
                "M365 Graph token not configured (context.m365.token). Provide a "
                "read-only token, or inject an MCP-backed client."
            )
        # Graph /search/query wiring lands with the token; intentionally not
        # guessed here. The interface (search -> list[{title,body,from,url}]) is
        # what the gatherer relies on.
        raise NotImplementedError


class TeamsSource:
    name = "teams"

    def __init__(self, client: CommsClient, limit: int = 5):
        self.client = client
        self.limit = limit

    async def gather(self, task: Task) -> list[ContextChunk]:
        query = self._query(task)
        if not query:
            return []
        results = self.client.search(query, limit=self.limit)
        chunks = []
        for m in results:
            who = m.get("from") or m.get("sender") or ""
            chunks.append(ContextChunk(
                source="teams",
                title=(m.get("title") or m.get("subject") or who or "message")[:120],
                content=(m.get("body") or m.get("content") or "")[:2000],
                ref=m.get("url") or m.get("webUrl") or "",
                score=1.0,
            ))
        return chunks

    def _query(self, task: Task) -> str:
        terms = keywords(task, limit=4)
        if task.external_id:
            return task.external_id
        return " ".join(terms[:3])
