"""Past-session memory lookup (file-backed markdown + SQLite, no vectors)."""

from __future__ import annotations

from ..core.db import Store
from ..core.task import Task
from .base import ContextChunk, keywords


class SessionsSource:
    name = "sessions"

    def __init__(self, store: Store, limit: int = 5):
        self.store = store
        self.limit = limit

    async def gather(self, task: Task) -> list[ContextChunk]:
        terms = keywords(task, limit=6)
        if not terms:
            return []
        # LIKE-based recall over the file-backed memories index. FTS5 is a later
        # optimization; the markdown files remain the source of truth.
        clauses = " OR ".join("content LIKE ? OR title LIKE ?" for _ in terms)
        params: list[str] = []
        for t in terms:
            params += [f"%{t}%", f"%{t}%"]
        cur = await self.store.db.execute(
            f"SELECT type, title, content FROM memories WHERE confirmed = 1 AND "
            f"({clauses}) LIMIT ?",
            (*params, self.limit),
        )
        rows = await cur.fetchall()
        return [
            ContextChunk(source="sessions", title=f"[{r['type']}] {r['title']}",
                         content=r["content"][:2000])
            for r in rows
        ]
