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
        chunks = [
            ContextChunk(source="sessions", title=f"[{r['type']}] {r['title']}",
                         content=r["content"][:2000])
            for r in rows
        ]
        chunks += await self._recall_failures(terms)
        return chunks

    async def _recall_failures(self, terms: list[str]) -> list[ContextChunk]:
        """W3.3: ranked full-text recall over the failure/fix record
        (events_fts, migration 0006) — "a similar failure was handled in
        task X" reaches the planner/coder instead of being rediscovered.
        Advisory context only; empty on any FTS error (older DBs, odd
        tokens) — recall must never break gathering."""
        # FTS5 treats bare punctuation as operators; quote each term.
        query = " OR ".join('"' + t.replace('"', "") + '"' for t in terms if t)
        if not query:
            return []
        try:
            cur = await self.store.db.execute(
                """SELECT te.task_id, json_extract(te.data, '$.kind'),
                          substr(json_extract(te.data, '$.text'), 1, 700)
                   FROM events_fts f JOIN task_events te ON te.id = f.rowid
                   WHERE events_fts MATCH ? ORDER BY rank LIMIT 3""",
                (query,),
            )
            rows = await cur.fetchall()
        except Exception:  # noqa: BLE001 — recall is a bonus, never a blocker
            return []
        return [
            ContextChunk(
                source="sessions",
                title=f"[past {r[1]}] task {str(r[0])[:8]}",
                content=f"A similar {r[1]} was recorded on task "
                        f"{str(r[0])[:8]}:\n{r[2]}",
            )
            for r in rows
        ]
