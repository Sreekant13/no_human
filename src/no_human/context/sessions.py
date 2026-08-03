"""Past-session memory lookup (file-backed markdown + SQLite, no vectors)."""

from __future__ import annotations

from ..core.db import Store
from ..core.task import Task
from .base import ContextChunk, keywords


def _has_banned_term(title: str | None, content: str | None) -> bool:
    """True when a stored memory carries a vendor/employer term.

    This source reads the `memories` table with its own SQL, so it bypasses
    `list_memories` -> `filter_triggered` -> `Orchestrator._active_memories`
    and the screen that sits on that assignment. Its chunk TITLES reach
    `_context_digest` and go verbatim into the implement prompt, which makes it
    a second, independent route from the learning store into a prompt — proven
    at runtime, not inferred: a confirmed rule whose title carried a banned term
    appeared in the digest under `[sessions]`.

    Screened here rather than at the caller because the bypass IS the direct
    query; anything layered above it would miss this for the same reason the
    original screen did.

    Fails OPEN on a matcher error, matching `_screen_memories_for_terms`: this
    is advisory context, and a matcher bug that silently emptied it would
    degrade every run for a reason nobody could see.
    """
    from ..eval.vendor_terms import find_banned_terms

    try:
        return bool(find_banned_terms(f"{title or ''}\n{content or ''}"))
    except Exception:  # noqa: BLE001 — never let the screen become the failure
        return False


class SessionsSource:
    name = "sessions"

    def __init__(self, store: Store, limit: int = 5):
        self.store = store
        self.limit = limit

    @staticmethod
    async def _scope_of(repo_path: str) -> str | None:
        """B4 scope identity for the task's checkout, off-thread (it shells
        out to git). None — no remote, no repo, git failing — is always safe:
        the query falls back to path matching."""
        import asyncio

        from ..learning.scope import resolve_project_scope
        try:
            return await asyncio.to_thread(resolve_project_scope, repo_path)
        except Exception:  # noqa: BLE001 — advisory context, never the failure
            return None

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
        # `archived` is honoured here the way `Store.list_memories` honours it.
        # This source queries `memories` directly, so an operator who archived a
        # rule was still getting it back through this path.
        #
        # B4 PROJECT SCOPING, for the same bypass reason: this is a second,
        # independent route from the learning store into a prompt, so it must
        # honour the same boundary `list_memories(project=…, scope=…)` draws —
        # this task's project (by remote identity, then by checkout path for
        # legacy rows) plus explicit globals. A keyword match is not a licence
        # for one repo's lessons to reach another repo's prompt. A task with
        # no repo at all gets globals only.
        scope_clause = "(project IS NULL AND project_scope IS NULL)"
        if task.repo_path:
            scope = await self._scope_of(task.repo_path)
            if scope is not None:
                scope_clause = ("(project_scope = ? OR project = ? "
                                "OR (project IS NULL AND project_scope IS NULL))")
                params += [scope, task.repo_path]
            else:
                scope_clause = ("(project = ? "
                                "OR (project IS NULL AND project_scope IS NULL))")
                params.append(task.repo_path)
        rows = await self.store.query(
            f"SELECT type, title, content FROM memories WHERE confirmed = 1 AND "
            f"(archived IS NULL OR archived = 0) AND ({clauses}) AND "
            f"{scope_clause} LIMIT ?",
            (*params, self.limit),
        )
        chunks = [
            ContextChunk(source="sessions", title=f"[{r['type']}] {r['title']}",
                         content=r["content"][:2000])
            for r in rows if not _has_banned_term(r["title"], r["content"])
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
            rows = await self.store.query(
                """SELECT te.task_id, json_extract(te.data, '$.kind'),
                          substr(json_extract(te.data, '$.text'), 1, 700)
                   FROM events_fts f JOIN task_events te ON te.id = f.rowid
                   WHERE events_fts MATCH ? ORDER BY rank LIMIT 3""",
                (query,),
            )
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
