"""Outlook email context via the M365 connector (read-only).

Same ``CommsClient`` interface as Teams (search -> list[dict]); a Graph-backed
client is injected in production, an MCP-backed one in tests/demo.
"""

from __future__ import annotations

from ..core.task import Task
from .base import ContextChunk, keywords
from .teams import CommsClient


class OutlookSource:
    name = "outlook"

    def __init__(self, client: CommsClient, limit: int = 5):
        self.client = client
        self.limit = limit

    async def gather(self, task: Task) -> list[ContextChunk]:
        query = task.external_id or " ".join(keywords(task, limit=3))
        if not query:
            return []
        results = self.client.search(query, limit=self.limit)
        return [
            ContextChunk(
                source="outlook",
                title=(m.get("subject") or m.get("title") or "email")[:120],
                content=(m.get("body") or m.get("content") or "")[:2000],
                ref=m.get("url") or m.get("webLink") or "",
                score=1.0,
            )
            for m in results
        ]
