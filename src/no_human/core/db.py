"""Async SQLite store (WAL). Single-user, single-host — no Postgres (§3.6)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple, TypeVar

import aiosqlite

from .task import Task, TaskStatus, assert_transition

log = logging.getLogger("no_human.db")

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_T = TypeVar("_T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialized_write(
    fn: Callable[..., Awaitable[_T]],
) -> Callable[..., Awaitable[_T]]:
    """Run one Store write — every statement of it plus its COMMIT — as a
    single critical section on the shared connection.

    ONE `aiosqlite.Connection` is shared by every coroutine (the pool runs
    `concurrency.max_workers` tasks against one Store). aiosqlite serialises
    *individual* operations on its worker thread, but never a *sequence* of
    them: each `await` is a scheduling point where another coroutine's write —
    and, fatally, its `commit()` — runs in the middle of ours. Two consequences,
    both of which this decorator fixes:

    1. `commit()` ends the connection's implicit transaction, so a foreign
       commit lands halfway through any multi-statement write here
       (`create_attempt`'s UPDATE+INSERT, `_migrate`, `update_attempt`'s
       read-modify-write, `add_memory`'s dedupe-then-insert). The atomicity
       those writes assume was never real once two tasks ran at once.
    2. If the statement the foreign commit interrupts is a *writer that has
       produced a row* — `UPDATE … RETURNING`, whose VDBE stays live between
       `execute()` and the fetch — SQLite refuses the COMMIT outright:
       ``OperationalError: cannot commit transaction - SQL statements in
       progress``. That crash killed real attempts (see
       `tests/test_db_concurrency.py`).

    Reads are deliberately NOT serialised: they never COMMIT, so they cannot
    split someone else's transaction, and a live SELECT cursor does not block
    COMMIT (SQLite only refuses on `db->nVdbeWrite > 0`). The lock is per-Store,
    i.e. per-connection, which is the right scope — cross-connection and
    cross-process serialisation is SQLite's own job and is unchanged.
    """

    @functools.wraps(fn)
    async def wrapper(self: "Store", *args: Any, **kwargs: Any) -> _T:
        async with self._write_lock:
            return await fn(self, *args, **kwargs)

    wrapper.__nh_serialized_write__ = True  # type: ignore[attr-defined]
    return wrapper


class JiraImportedTaskRow(NamedTuple):
    """One row of the Jira-picker imported-chip projection (SCRUM-54) — only
    the four columns the chip lookup needs, never a full Task hydration."""

    external_id: str
    id: str
    status: str
    created_at: str


class Store:
    """Thin async wrapper over the tasks/attempts tables."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._db: aiosqlite.Connection | None = None
        # Guards every write critical section on this connection — see
        # `serialized_write` for why one connection + N coroutines needs it.
        self._write_lock = asyncio.Lock()

    async def connect(self) -> "Store":
        # no_human.db sits beside the credential store; the directory must be
        # private even when the DB is what creates it.
        from ..config import ensure_private_dir
        ensure_private_dir(self.path.parent)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._migrate()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "Store":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store not connected; call connect() first")
        return self._db

    @serialized_write
    async def _migrate(self) -> None:
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            await self.db.executescript(sql_file.read_text())
        await self._ensure_task_columns()
        await self.db.commit()

    async def _ensure_task_columns(self) -> None:
        """Add columns that SQLite cannot create idempotently in a .sql file
        (no ADD COLUMN IF NOT EXISTS). Safe to run on every connect."""
        cur = await self.db.execute("PRAGMA table_info(tasks)")
        existing = {row["name"] for row in await cur.fetchall()}
        wanted = {
            "kind": "TEXT DEFAULT 'feature'",
            "linked_repos": "TEXT",  # JSON list of additional repo paths
            "parent_id": "TEXT",  # LeadAgent: compound task sub-task linkage
            # Cooperative cancellation. A dedicated column, NOT task.context:
            # the CLI and the running orchestrator both hold a Task copy, and
            # `update_task` rewrites the whole mutable surface from it — so a
            # flag in `context` is clobbered by whichever writer flushes last.
            # `update_task`'s column list deliberately omits this one, leaving
            # the CLI its sole writer and the orchestrator its sole consumer.
            "cancel_requested": "TEXT",  # reason, or NULL for "keep running"
        }
        for col, decl in wanted.items():
            if col not in existing:
                await self.db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)"
        )
        # Phase 7d: cache metric columns on attempts (validates Phase 2a caching).
        cur2 = await self.db.execute("PRAGMA table_info(attempts)")
        att_existing = {row["name"] for row in await cur2.fetchall()}
        att_wanted = {
            "cache_read_tokens": "INTEGER DEFAULT 0",
            "cache_creation_tokens": "INTEGER DEFAULT 0",
            # The REVIEWER's burn, in its own columns. It was thrown away after the verdict,
            # so the DB held only the coder's tokens and no cost surface could price the gate
            # (59 Opus-4-8 runs over full diffs, costing nothing on the record). Separate from
            # the coder's so by_tier/by_profile keep attributing coder spend to the coder.
            "review_tokens_used": "INTEGER DEFAULT 0",
            "review_cache_read_tokens": "INTEGER DEFAULT 0",
            "review_cache_creation_tokens": "INTEGER DEFAULT 0",
            # PLANNING burn (single planner, MoA proposers, aggregator): ran on
            # separate readonly backends and was persisted NOWHERE — the docs
            # even claimed it lived "inside the coder's session" (ARCH_REVIEW
            # #5; ~917k cache-read priced at $0 on one measured task). Written
            # once, onto the attempt row of the attempt the plan fed.
            "plan_tokens_used": "INTEGER DEFAULT 0",
            "plan_cache_read_tokens": "INTEGER DEFAULT 0",
            "plan_cache_creation_tokens": "INTEGER DEFAULT 0",
            # UTILITY-tier burn (supervisor checks, distillation,
            # stuck-hypothesis) — discarded entirely before B2 #6.
            "utility_tokens_used": "INTEGER DEFAULT 0",
            "utility_cache_read_tokens": "INTEGER DEFAULT 0",
            "utility_cache_creation_tokens": "INTEGER DEFAULT 0",
            # Which model actually ran which role on this attempt. Nothing
            # recorded it, which is how a frozen config.yaml silently inverted
            # coder and reviewer for a week.
            "models": "TEXT DEFAULT '{}'",
            # Which subscription paid for this attempt (profile name, never a
            # token). NULL on attempts that predate auth profiles.
            "auth_profile": "TEXT",
        }
        for col, decl in att_wanted.items():
            if col not in att_existing:
                await self.db.execute(f"ALTER TABLE attempts ADD COLUMN {col} {decl}")
        # D2 #3 curator: memories gain a recoverable archive flag — the
        # curator NEVER deletes (broker invariant); archived rows leave the
        # pending queue but stay queryable.
        cur_m = await self.db.execute("PRAGMA table_info(memories)")
        mem_existing = {row["name"] for row in await cur_m.fetchall()}
        if "archived" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN archived INTEGER DEFAULT 0")

        # Phase 6a: test_layers column on projects (JSON-encoded TestPlan layers).
        cur3 = await self.db.execute("PRAGMA table_info(projects)")
        proj_existing = {row["name"] for row in await cur3.fetchall()}
        if "test_layers" not in proj_existing:
            await self.db.execute(
                "ALTER TABLE projects ADD COLUMN test_layers TEXT DEFAULT '[]'"
            )
        # Phase 7e: history cache table — content-signature keyed so onboarding
        # doesn't re-extract every request. "Re-scan" forces refresh.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS history_cache (
                content_sig TEXT PRIMARY KEY,
                cascade_id TEXT NOT NULL,
                title TEXT,
                findings_json TEXT,
                ingested_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Real spend that NO ATTEMPT ROW can own. Two sources, both intake:
        #
        #  * The interactive grill (`nh task add --grill`, the board's
        #    /api/grill endpoints) runs BEFORE a task exists, so
        #    `attempts.utility_*` is not merely the wrong column — there is no
        #    row, and often no task ever (the operator can walk away mid-
        #    wizard). Those rows carry task_id NULL.
        #  * Pre-attempt intake on a task that never reached an attempt (parked
        #    at the plan gate, escalated on an unavailable input, decomposed).
        #    The task id IS known, so those rows carry it — but no attempt
        #    spent it, and inventing an attribution is how a cost surface
        #    starts lying.
        #
        # `site` says which, per row, so the residual stays diagnosable instead
        # of being one anonymous number.
        #
        # DELIBERATELY NOT summed into per-task cost (`lifetime_usage`,
        # `eval/northstar`): those answer "what did THIS task cost", and this
        # table is by construction the spend no attempt owns. It is the
        # whole-cost residual — read it for the true total, not the per-task
        # one. `nh status` prints it whenever it is non-zero.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS unattributed_usage (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                site TEXT NOT NULL,
                model TEXT,
                task_id TEXT,
                tokens_used INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0
            )
        """)
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_unattributed_usage_task "
            "ON unattributed_usage(task_id)"
        )

    # ----------------------------- tasks ---------------------------------- #

    @serialized_write
    async def create_task(self, task: Task) -> Task:
        row = task.to_row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        await self.db.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", row
        )
        await self.db.commit()
        return task

    async def get_task(self, task_id: str) -> Task | None:
        cur = await self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return Task.from_row(dict(row)) if row else None

    async def find_task(self, prefix: str) -> Task | None:
        """Resolve a task by full id or a unique id prefix (CLI convenience)."""
        cur = await self.db.execute(
            "SELECT * FROM tasks WHERE id = ? OR id LIKE ? LIMIT 2",
            (prefix, prefix + "%"),
        )
        rows = await cur.fetchall()
        if len(rows) == 1:
            return Task.from_row(dict(rows[0]))
        return None

    async def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        if status is not None:
            cur = await self.db.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            )
        rows = await cur.fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    async def get_task_by_source_external_id(
        self, source: str, external_id: str
    ) -> Task | None:
        """Filtered dedupe lookup for any external-source intake (Slack, and
        usable by Jira too) — one indexed-shape query instead of hydrating
        every task via `list_tasks()` just to scan for a match."""
        cur = await self.db.execute(
            "SELECT * FROM tasks WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        )
        row = await cur.fetchone()
        return Task.from_row(dict(row)) if row else None

    async def list_jira_imported_tasks(self) -> list[JiraImportedTaskRow]:
        """Narrow projection for the Jira picker's imported-chip lookup
        (SCRUM-54): only (external_id, id, status, created_at) for
        jira-sourced tasks with a linked external_id, via one filtered SQL
        query — never a full `list_tasks()` hydration of every task's every
        column just to read four fields."""
        cur = await self.db.execute(
            "SELECT external_id, id, status, created_at FROM tasks "
            "WHERE source = 'jira' AND external_id IS NOT NULL"
        )
        rows = await cur.fetchall()
        return [
            JiraImportedTaskRow(
                external_id=r["external_id"], id=r["id"],
                status=r["status"], created_at=r["created_at"],
            )
            for r in rows
        ]

    @serialized_write
    async def set_status(
        self,
        task: Task,
        new_status: TaskStatus,
        *,
        validate: bool = True,
        human_override: bool = False,
    ) -> Task | None:
        """Transition a task, enforcing the legal-transition map by default.

        CAS guard (SCRUM-73): the WHERE clause is checked against the live DB
        row inside this one statement, not the possibly-stale `task.status`
        this caller is holding — a worker coroutine can hold IMPLEMENTING
        while a human's `shipped` verb already wrote DONE, and
        IMPLEMENTING->REVIEWING passes `assert_transition` on the stale
        value. Terminal here means the row reads DONE, or reads FAILED with a
        `cancel_reason` recorded in context (an explicit human cancel, not a
        plain failure) — a plain FAILED row stays writable so `nh task retry`
        / `POST /api/tasks/{id}/retry` keep working. Once a row is terminal,
        only a write that keeps its status unchanged may land; every other
        write (including validate=False ones) is a no-op that returns None.

        `human_override=True` bypasses the guard entirely — reserved for the
        human verbs that are allowed to move a row OUT of a terminal state
        (retry, cancel, shipped). Every other call site (watcher,
        orchestrator, scheduler, pipeline) must leave it at the default so a
        stale in-process handle can never clobber a human's terminal write.
        """
        if validate:
            assert_transition(task.status, new_status)
        now = _now()
        if human_override:
            cur = await self.db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_status.value, now, task.id),
            )
        else:
            cur = await self.db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? "
                "WHERE id = ? AND ("
                "  status = ?"
                "  OR NOT ("
                "    status = ?"
                "    OR (status = ? AND json_extract(context, '$.cancel_reason') IS NOT NULL)"
                "  )"
                ")",
                (
                    new_status.value, now, task.id,
                    new_status.value,
                    TaskStatus.DONE.value, TaskStatus.FAILED.value,
                ),
            )
        await self.db.commit()
        if cur.rowcount == 0:
            row_cur = await self.db.execute(
                "SELECT status FROM tasks WHERE id = ?", (task.id,)
            )
            row = await row_cur.fetchone()
            if row is not None:
                log.warning(
                    "set_status: blocked %s -> %s on terminal row %s",
                    row["status"], new_status.value, task.id,
                )
                task.status = TaskStatus(row["status"])
            return None
        task.status = new_status
        task.updated_at = now
        return task

    @serialized_write
    async def update_task(self, task: Task) -> Task:
        """Persist the full mutable surface of a task row.

        CAS guard (SCRUM-73): mirrors set_status's terminal definition (done,
        or failed with a `cancel_reason` in context) — a terminal row's
        status column is protected from being resurrected by a stale
        in-memory `task.status`, via a CASE keyed on the row's own
        pre-update status/context (evaluated atomically inside this one
        statement, before this call's own :context write applies). Every
        other column still writes normally, so e.g. the Jira poller can keep
        updating context write-back markers on an already-DONE row. No
        override parameter here — callers that must move a row OUT of a
        terminal state go through set_status(..., human_override=True)
        instead, since update_task never carries that intent.
        """
        task.updated_at = _now()
        row = task.to_row()
        await self.db.execute(
            """UPDATE tasks SET
                 external_id=:external_id, source=:source, title=:title,
                 description=:description, requirements=:requirements,
                 acceptance_criteria=:acceptance_criteria, repo_path=:repo_path,
                 kind=:kind, parent_id=:parent_id,
                 status = CASE
                            WHEN (
                              status = 'done'
                              OR (status = 'failed'
                                  AND json_extract(context, '$.cancel_reason') IS NOT NULL)
                            ) AND status != :status
                            THEN status ELSE :status END,
                 blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority, context=:context, plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        # Deliberately NOT `UPDATE … RETURNING status`. A writer that has
        # produced a row leaves its VDBE live between `execute()` and the fetch,
        # and every `await` in that gap is a scheduling point: SQLite refuses any
        # COMMIT while a write statement is in progress ("cannot commit
        # transaction - SQL statements in progress"). That was this method's
        # half of KI-1. The write lock alone would close it, but only while the
        # ONLY thing that can reach this connection is a lock-taking Store
        # method, and only because CPython's refcounting happens to finalize the
        # abandoned cursor if this frame unwinds (a cancellation, an exception) —
        # measured, not assumed, but an implementation detail no invariant should
        # rest on. A plain UPDATE parks nothing. The read-back below is inside
        # the same uncommitted transaction and the same critical section, so it
        # observes exactly what RETURNING did.
        cur = await self.db.execute(
            "SELECT status FROM tasks WHERE id = ?", (task.id,)
        )
        result = await cur.fetchone()
        await self.db.commit()
        if result is not None and result["status"] != row["status"]:
            log.warning(
                "update_task: blocked status %s -> %s on terminal row %s",
                result["status"], row["status"], task.id,
            )
            task.status = TaskStatus(result["status"])
        return task

    @serialized_write
    async def merge_context(self, task_id: str, patch: dict) -> dict:
        """Atomically merge *patch* into the task's context (RFC 7396).

        The lost-update fix for concurrent context writers: `update_task`
        rewrites the whole context blob from a Task copy, so the watcher, the
        CLI and the orchestrator (different coroutines AND different
        processes) clobber each other — whichever flushes last wins (the
        cancel_requested column above documents the same failure). A single
        `json_patch` UPDATE is atomic under SQLite's write serialization, so
        concurrent merges of different keys both survive, across processes.

        Semantics (RFC 7396): nested dicts merge recursively; lists/scalars
        replace; a ``None`` value DELETES the key. Returns the merged context.
        """
        await self.db.execute(
            """UPDATE tasks SET
                 context = json_patch(COALESCE(context, '{}'), ?),
                 updated_at = ?
               WHERE id = ?""",
            (json.dumps(patch), _now(), task_id),
        )
        await self.db.commit()
        cur = await self.db.execute(
            "SELECT context FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    @serialized_write
    async def append_context_list(self, task_id: str, key: str, item: dict) -> None:
        """Atomically append *item* to the context list at *key* (created if
        absent). List appends cannot be expressed as a merge patch (RFC 7396
        replaces arrays wholesale), so this uses json_set's '[#]' append —
        one UPDATE, no read-modify-write."""
        assert "." not in key and "[" not in key, "flat keys only"
        await self.db.execute(
            f"""UPDATE tasks SET
                 context = json_set(
                   json_patch(COALESCE(context, '{{}}'),
                              CASE WHEN json_extract(COALESCE(context,'{{}}'),
                                        '$.{key}') IS NULL
                                   THEN json_object('{key}', json_array())
                                   ELSE '{{}}' END),
                   '$.{key}[#]', json(?)),
                 updated_at = ?
               WHERE id = ?""",
            (json.dumps(item), _now(), task_id),
        )
        await self.db.commit()

    @serialized_write
    async def update_task_columns(self, task: Task) -> Task:
        """Persist the task's mutable columns EXCEPT context. Multi-writer
        zones (watcher, CLI, gate) must write context only via merge_context/
        append_context_list — this companion writes the rest without
        clobbering concurrent context merges with a stale blob."""
        task.updated_at = _now()
        row = task.to_row()
        await self.db.execute(
            """UPDATE tasks SET
                 external_id=:external_id, source=:source, title=:title,
                 description=:description, requirements=:requirements,
                 acceptance_criteria=:acceptance_criteria, repo_path=:repo_path,
                 kind=:kind, parent_id=:parent_id,
                 status=:status, blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority, plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        await self.db.commit()
        return task

    @serialized_write
    async def request_cancel(self, task_id: str, reason: str) -> None:
        """Ask a running task to stop at its next cooperative checkpoint.

        A targeted UPDATE of one column: it must not read-modify-write the task
        row, or it would race the orchestrator that owns every other column.
        """
        await self.db.execute(
            "UPDATE tasks SET cancel_requested = ? WHERE id = ?", (reason, task_id)
        )
        await self.db.commit()

    async def get_cancel_request(self, task_id: str) -> str | None:
        """The pending cancellation reason for *task_id*, or None."""
        cur = await self.db.execute(
            "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cur.fetchone()
        return row["cancel_requested"] if row else None

    @serialized_write
    async def clear_cancel_request(self, task_id: str) -> None:
        """Drop a pending cancellation, once honoured or withdrawn."""
        await self.db.execute(
            "UPDATE tasks SET cancel_requested = NULL WHERE id = ?", (task_id,)
        )
        await self.db.commit()

    async def list_subtasks(self, parent_id: str) -> list[Task]:
        """Return all sub-tasks of a compound parent task."""
        cur = await self.db.execute(
            "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at",
            (parent_id,),
        )
        rows = await cur.fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    async def count_subtasks(self, parent_id: str) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_id = ?", (parent_id,)
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ---------------------------- attempts --------------------------------- #

    @serialized_write
    async def create_attempt(self, task_id: str, attempt_number: int) -> str:
        # An earlier attempt of this task still 'in_progress' cannot be running:
        # attempts are serial, so a new one starting means the old process died
        # (kill -9, crash) without ever closing its row. Left alone, those rows
        # make `attempts.status` untrustworthy as a completion signal — the
        # baseline had three of them. Close them for what they are.
        await self.db.execute(
            "UPDATE attempts SET status = 'interrupted', "
            "failure_reason = COALESCE(NULLIF(TRIM(failure_reason), ''), "
            "'interrupted: superseded by a newer attempt — the prior worker "
            "process died without closing its row') "
            "WHERE task_id = ? AND status = 'in_progress' AND attempt_number < ?",
            (task_id, attempt_number),
        )
        attempt_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO attempts (id, task_id, attempt_number) VALUES (?, ?, ?)",
            (attempt_id, task_id, attempt_number),
        )
        await self.db.commit()
        return attempt_id

    @serialized_write
    async def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        if not fields:
            return
        # Observability backstop (C2): a failed attempt with no stated reason
        # is undiagnosable — task 6cfdb936 burned attempts on exactly that.
        # When the caller marks failed without a reason AND the row has none,
        # stamp a loud sentinel instead of leaving silence. Never clobbers a
        # reason set by an earlier update.
        if fields.get("status") == "failed" and not fields.get("failure_reason"):
            fields.pop("failure_reason", None)
            cur = await self.db.execute(
                "SELECT COALESCE(failure_reason, '') FROM attempts WHERE id = ?",
                (attempt_id,))
            row = await cur.fetchone()
            if row is not None and not row[0].strip():
                fields["failure_reason"] = (
                    "(no failure reason recorded — observability gap; "
                    "report which stage failed silently)")
        # JSON-encode dict/list values transparently.
        clean = {
            k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in fields.items()
        }
        assignments = ", ".join(f"{k} = :{k}" for k in clean)
        clean["id"] = attempt_id
        await self.db.execute(
            f"UPDATE attempts SET {assignments} WHERE id = :id", clean
        )
        await self.db.commit()

    async def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number",
            (task_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def attempts_by_task(self) -> dict[str, list[dict[str, Any]]]:
        """All attempts, grouped by task — ONE query.

        B2 #16: the board issued list_attempts PER TASK, every 2 seconds, per
        connected socket (an N+1 over the whole task history on every tick).
        """
        cur = await self.db.execute(
            "SELECT * FROM attempts ORDER BY task_id, attempt_number")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in await cur.fetchall():
            row = dict(r)
            grouped.setdefault(row["task_id"], []).append(row)
        return grouped

    async def count_attempts(self, task_id: str) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE task_id = ?", (task_id,)
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # The four model tiers the attempts table meters, and the three token
    # columns each one carries. `eval/northstar.py` already sums exactly this
    # set to report cost; the budget gate below now matches it, so the two can
    # no longer disagree about what a task spent.
    _USAGE_TIERS = ("", "review_", "plan_", "utility_")

    @classmethod
    def _usage_columns(cls) -> tuple[str, ...]:
        cols: list[str] = []
        for tier in cls._USAGE_TIERS:
            cols.append("tokens_used" if tier == "" else f"{tier}tokens_used")
            cols.append(f"{tier}cache_read_tokens")
            cols.append(f"{tier}cache_creation_tokens")
        return tuple(cols)

    async def lifetime_usage(self, task_id: str) -> tuple[int, int]:
        """(attempts, tokens) spent over the task's WHOLE life, resumes included.

        Tokens = everything the attempt metered: in/out, cache reads AND cache
        creation, across all four model tiers (coder, reviewer, planner,
        utility). Cache reads are where the bulk of the burn lives (~83%), but
        this used to sum ONLY the coder's ``tokens_used + cache_read_tokens``
        — 2 of 12 columns. The gate was therefore blind to every reviewer,
        planner and utility token, and to cache creation everywhere. Measured
        over 574 real attempt rows that blind spot is 16.2% of true spend, and
        a task whose burn was mostly reviewer or utility could never trip the
        cap at all. Cache creation is billed, so a spend gate must count it.

        Interrupted/killed rows count: they spent the attempt even if their
        token columns under-report (pre-1638427 rows recorded zero).
        """
        summed = " + ".join(f"COALESCE({c}, 0)" for c in self._usage_columns())
        cur = await self.db.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM({summed}), 0) AS toks "
            "FROM attempts WHERE task_id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
        return (int(row["n"]), int(row["toks"])) if row else (0, 0)

    # ---------------------- unattributed usage ledger ----------------------- #

    @serialized_write
    async def record_unattributed_usage(
        self, *, site: str, tokens_used: int = 0, cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0, model: str | None = None,
        task_id: str | None = None,
    ) -> str | None:
        """Book utility-tier spend that no attempt row can own.

        ``site`` names WHERE it was spent — the live values are ``"api.grill"``,
        ``"api.grill_stream"``, ``"api.grill_stream.evaluate_spec"``,
        ``"cli.task_add.grill"``, ``"orphaned_utility_usage"`` and
        ``"orphaned_plan_usage"`` — so the residual stays diagnosable rather
        than being one anonymous number.
        Returns the row id, or None when there was nothing to record — a call
        that reports zero across all three figures writes no row, so the table
        holds spend and never padding.

        NOT YET BOOKED ANYWHERE, and this ledger is their natural home — five
        further LLM sites still record nothing, verified present as of this
        commit: the GUI transcript analyzer (`api/app.py:2925`, review tier),
        the WikiGenerator (`api/app.py:3008` + `docs_gen.py:118`,
        ``max_turns=12``), and three CLI backends (`cli/commands.py:1776`,
        `:2310`, `:3138`). Deliberately left out of this change, which is
        scoped to the six intake sites.
        """
        tokens_used = int(tokens_used or 0)
        cache_read_tokens = int(cache_read_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        if not (tokens_used or cache_read_tokens or cache_creation_tokens):
            return None
        row_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO unattributed_usage (id, ts, site, model, task_id, "
            "tokens_used, cache_read_tokens, cache_creation_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, _now(), site, model, task_id, tokens_used,
             cache_read_tokens, cache_creation_tokens),
        )
        await self.db.commit()
        return row_id

    async def unattributed_usage_totals(
        self, task_id: str | None = None
    ) -> dict[str, int]:
        """Totals over the unattributed ledger: ``{calls, tokens_used,
        cache_read_tokens, cache_creation_tokens, total}``.

        ``task_id=None`` totals the WHOLE ledger (the default question — "how
        much intake spend does no task own"); pass an id to scope it.
        """
        sql = ("SELECT COUNT(*) AS calls, "
               "COALESCE(SUM(tokens_used), 0) AS tokens_used, "
               "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
               "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens "
               "FROM unattributed_usage")
        args: tuple[Any, ...] = ()
        if task_id is not None:
            sql += " WHERE task_id = ?"
            args = (task_id,)
        cur = await self.db.execute(sql, args)
        row = await cur.fetchone()
        out = {k: int(row[k] if row else 0) for k in (
            "calls", "tokens_used", "cache_read_tokens", "cache_creation_tokens")}
        out["total"] = (out["tokens_used"] + out["cache_read_tokens"]
                        + out["cache_creation_tokens"])
        return out

    # --------------------------- memories ---------------------------------- #
    # The human-confirmed learning queue (PLAN.md 4.5): proposals land here
    # with confirmed=0 and never enter the active rule set until a human
    # confirms them (avoids leniency-biased lessons accumulating silently).

    @serialized_write
    async def add_memory(
        self, *, mem_type: str, title: str, content: str,
        tags: list[str] | None = None, project: str | None = None,
        source: str = "proposed", confirmed: bool = False,
        dedupe_key: str | None = None,
    ) -> str | None:
        """Insert a memory. If ``dedupe_key`` matches an existing memory's
        signature (stored in file_path), skip and return None."""
        if dedupe_key is not None:
            cur = await self.db.execute(
                "SELECT id FROM memories WHERE file_path = ? LIMIT 1", (dedupe_key,)
            )
            if await cur.fetchone():
                return None
        mem_id = uuid.uuid4().hex
        await self.db.execute(
            """INSERT INTO memories
                 (id, type, title, content, file_path, tags, project, source, confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, mem_type, title, content, dedupe_key,
             json.dumps(tags or []), project, source, 1 if confirmed else 0),
        )
        await self.db.commit()
        return mem_id

    async def list_memories(
        self, *, confirmed: bool | None = None, source: str | None = None,
        mem_type: str | None = None, project: str | None = None,
        include_global: bool = True, include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List memories, optionally scoped to a project.

        When ``project`` is given, only rules/skills attached to that project are
        returned, plus globals (``project IS NULL``) unless ``include_global`` is
        False. When ``project`` is None, no project filter is applied (all rows).
        """
        clauses, params = [], []
        if not include_archived:
            # archived is NULL on rows that predate the column — treat as live
            clauses.append("(archived IS NULL OR archived = 0)")
        if confirmed is not None:
            clauses.append("confirmed = ?")
            params.append(1 if confirmed else 0)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if mem_type is not None:
            clauses.append("type = ?")
            params.append(mem_type)
        if project is not None:
            if include_global:
                clauses.append("(project = ? OR project IS NULL)")
            else:
                clauses.append("project = ?")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = await self.db.execute(
            f"SELECT * FROM memories{where} ORDER BY created_at DESC", params
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ----------------------------- playbooks ------------------------------ #

    @serialized_write
    async def add_playbook(
        self, *, title: str, trigger_keywords: list[str] | None = None,
        procedure: str = "", postconditions: list[str] | None = None,
        forbidden: list[str] | None = None,
        required_from_user: list[str] | None = None,
        project: str | None = None,
    ) -> str:
        """Insert an operator-authored playbook (1.4). Returns its id."""
        pb_id = uuid.uuid4().hex
        await self.db.execute(
            """INSERT INTO playbooks
                 (id, title, trigger_keywords, procedure, postconditions,
                  forbidden, required_from_user, project)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pb_id, title, json.dumps(trigger_keywords or []), procedure,
             json.dumps(postconditions or []), json.dumps(forbidden or []),
             json.dumps(required_from_user or []), project),
        )
        await self.db.commit()
        return pb_id

    async def list_playbooks(
        self, *, project: str | None = None, include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """All playbooks, optionally scoped to a project (globals included
        unless ``include_global`` is False). Mirrors ``list_memories``."""
        clauses, params = [], []
        if project is not None:
            if include_global:
                clauses.append("(project = ? OR project IS NULL)")
            else:
                clauses.append("project = ?")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = await self.db.execute(
            f"SELECT * FROM playbooks{where} ORDER BY created_at DESC", params
        )
        return [dict(r) for r in await cur.fetchall()]

    @serialized_write
    async def delete_playbook(self, prefix: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM playbooks WHERE id = ? OR id LIKE ?",
            (prefix, prefix + "%"))
        await self.db.commit()
        return cur.rowcount > 0

    # --------------------------- PR merge order (2.2) --------------------- #

    @serialized_write
    async def add_pr_edge(self, *, child_pr: str, parent_pr: str,
                          project: str | None = None) -> None:
        """Record that child_pr must merge AFTER parent_pr (2.2)."""
        await self.db.execute(
            "INSERT OR IGNORE INTO pr_edges (child_pr, parent_pr, project) "
            "VALUES (?, ?, ?)", (child_pr, parent_pr, project))
        await self.db.commit()

    async def list_pr_edges(
        self, *, project: str | None = None,
    ) -> list[tuple[str, str]]:
        """All (child_pr, parent_pr) edges, optionally scoped to a project."""
        if project is not None:
            cur = await self.db.execute(
                "SELECT child_pr, parent_pr FROM pr_edges "
                "WHERE project = ? OR project IS NULL", (project,))
        else:
            cur = await self.db.execute("SELECT child_pr, parent_pr FROM pr_edges")
        return [(r["child_pr"], r["parent_pr"]) for r in await cur.fetchall()]

    @serialized_write
    async def delete_pr_edges_for(self, pr: str) -> int:
        """Remove every edge touching a PR (e.g. once it merges or closes)."""
        cur = await self.db.execute(
            "DELETE FROM pr_edges WHERE child_pr = ? OR parent_pr = ?", (pr, pr))
        await self.db.commit()
        return cur.rowcount

    async def find_memory(self, prefix: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM memories WHERE id = ? OR id LIKE ? LIMIT 2",
            (prefix, prefix + "%"),
        )
        rows = await cur.fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    @serialized_write
    async def archive_memory(self, mem_id: str, reason: str = "") -> bool:
        """Recoverable archive (curator action — never a delete). The reason
        is appended to content so recovery keeps the audit trail."""
        suffix = f"\n\n[archived: {reason}]" if reason else ""
        cur = await self.db.execute(
            "UPDATE memories SET archived = 1, content = content || ? "
            "WHERE id = ? AND archived = 0", (suffix, mem_id))
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def confirm_memory(self, mem_id: str) -> bool:
        """Promote a proposed memory into the active set (one-click confirm)."""
        cur = await self.db.execute(
            "UPDATE memories SET confirmed = 1, source = 'confirmed', "
            "updated_at = ? WHERE id = ?",
            (_now(), mem_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def delete_memory(self, mem_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        await self.db.commit()
        return cur.rowcount > 0

    # ----------------------- task events (persisted) ----------------------- #

    @serialized_write
    async def save_events(self, task_id: str, events: list[dict[str, Any]]) -> None:
        """Persist a batch of task events so they survive a server restart."""
        if not events:
            return
        await self.db.executemany(
            "INSERT INTO task_events (task_id, ts, data) VALUES (?, ?, ?)",
            [(task_id, e.get("ts", 0), json.dumps(e)) for e in events],
        )
        await self.db.commit()

    async def list_events(self, task_id: str) -> list[dict[str, Any]]:
        """Return persisted events for a task, ordered oldest → newest."""
        cur = await self.db.execute(
            "SELECT data FROM task_events WHERE task_id = ? ORDER BY ts ASC, id ASC",
            (task_id,),
        )
        rows = await cur.fetchall()
        return [json.loads(r["data"]) for r in rows]

    async def last_event_ts(self, task_id: str) -> float | None:
        """Epoch seconds of the newest persisted event, or None if none. Used
        by the stuck-active-task watchdog to detect a task frozen mid-run."""
        cur = await self.db.execute(
            "SELECT MAX(ts) FROM task_events WHERE task_id = ?", (task_id,))
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # ----------------------- project profiles ----------------------------- #

    @serialized_write
    async def upsert_profile(self, profile: "ProjectProfile") -> None:
        d = profile.to_dict()
        await self.db.execute(
            """INSERT INTO project_profiles
                 (repo_path, ecosystem, install_cmd, test_cmd, lint_cmd,
                  confirmed, data, updated_at)
               VALUES (:repo_path, :ecosystem, :install_cmd, :test_cmd, :lint_cmd,
                       :confirmed, :data, :updated_at)
               ON CONFLICT(repo_path) DO UPDATE SET
                 ecosystem=excluded.ecosystem, install_cmd=excluded.install_cmd,
                 test_cmd=excluded.test_cmd, lint_cmd=excluded.lint_cmd,
                 confirmed=excluded.confirmed, data=excluded.data,
                 updated_at=excluded.updated_at""",
            {
                "repo_path": d["repo_path"], "ecosystem": d["ecosystem"],
                "install_cmd": d["install_cmd"], "test_cmd": d["test_cmd"],
                "lint_cmd": d["lint_cmd"], "confirmed": 1 if d["confirmed"] else 0,
                "data": json.dumps(d), "updated_at": _now(),
            },
        )
        await self.db.commit()

    async def get_profile(self, repo_path: str) -> "ProjectProfile | None":
        from ..profile import ProjectProfile
        cur = await self.db.execute(
            "SELECT data FROM project_profiles WHERE repo_path = ?", (str(repo_path),)
        )
        row = await cur.fetchone()
        return ProjectProfile.from_dict(json.loads(row["data"])) if row else None

    async def list_profiles(self) -> list[dict[str, Any]]:
        """Return all onboarded repo profiles as dicts."""
        cur = await self.db.execute(
            "SELECT repo_path, ecosystem, confirmed, data FROM project_profiles "
            "ORDER BY repo_path"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ----------------------------- projects --------------------------------- #

    @serialized_write
    async def create_project(self, project: "Project") -> "Project":
        from ..project_model import Project
        row = project.to_row()
        await self.db.execute(
            "INSERT INTO projects (id, name, repo_paths, primary_repo, test_layers) "
            "VALUES (:id, :name, :repo_paths, :primary_repo, :test_layers)",
            row,
        )
        await self.db.commit()
        return project

    async def get_project(self, project_id: str) -> "Project | None":
        from ..project_model import Project
        cur = await self.db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        row = await cur.fetchone()
        return Project.from_row(row) if row else None

    async def get_project_by_name(self, name: str) -> "Project | None":
        from ..project_model import Project
        cur = await self.db.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        )
        row = await cur.fetchone()
        return Project.from_row(row) if row else None

    async def list_projects(self) -> list["Project"]:
        from ..project_model import Project
        cur = await self.db.execute(
            "SELECT * FROM projects ORDER BY name"
        )
        rows = await cur.fetchall()
        return [Project.from_row(r) for r in rows]

    async def find_project_by_repo(self, repo_path: str) -> "Project | None":
        """Find the project whose ``repo_paths`` contains *repo_path*."""
        for proj in await self.list_projects():
            if repo_path in proj.repo_paths:
                return proj
        return None

    @serialized_write
    async def update_project(self, project: "Project") -> None:
        row = project.to_row()
        await self.db.execute(
            "UPDATE projects SET name = :name, repo_paths = :repo_paths, "
            "primary_repo = :primary_repo, test_layers = :test_layers, "
            "updated_at = :updated_at WHERE id = :id",
            {**row, "updated_at": _now()},
        )
        await self.db.commit()

    @serialized_write
    async def delete_project(self, project_id: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM projects WHERE id = ?", (project_id,)
        )
        await self.db.commit()
        return cur.rowcount > 0

    # ----------------------- history cache (Phase 7e) ---------------------- #

    async def history_cache_get(self, content_sig: str) -> dict | None:
        """Return cached ingestion result for a transcript content signature."""
        cur = await self.db.execute(
            "SELECT * FROM history_cache WHERE content_sig = ?", (content_sig,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    @serialized_write
    async def history_cache_put(
        self, content_sig: str, cascade_id: str, title: str, findings_json: str,
    ) -> None:
        """Cache ingestion result keyed by content signature (upsert)."""
        await self.db.execute(
            "INSERT OR REPLACE INTO history_cache "
            "(content_sig, cascade_id, title, findings_json) VALUES (?, ?, ?, ?)",
            (content_sig, cascade_id, title, findings_json),
        )
        await self.db.commit()

    @serialized_write
    async def history_cache_clear(self) -> int:
        """Clear the entire history cache (Re-scan). Returns rows deleted."""
        cur = await self.db.execute("DELETE FROM history_cache")
        await self.db.commit()
        return cur.rowcount

    async def list_history_cache(self) -> list[dict[str, Any]]:
        """All cached IDE-transcript ingestion results (title + findings),
        most recent first — for `nh recall` to search alongside tasks/memories."""
        cur = await self.db.execute(
            "SELECT * FROM history_cache ORDER BY ingested_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
