"""Async SQLite store (WAL). Single-user, single-host — no Postgres (§3.6)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .task import Task, TaskStatus, assert_transition

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Thin async wrapper over the tasks/attempts tables."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> "Store":
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    # ----------------------------- tasks ---------------------------------- #

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

    async def set_status(
        self, task: Task, new_status: TaskStatus, *, validate: bool = True
    ) -> Task:
        """Transition a task, enforcing the legal-transition map by default."""
        if validate:
            assert_transition(task.status, new_status)
        task.status = new_status
        task.updated_at = _now()
        await self.db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (new_status.value, task.updated_at, task.id),
        )
        await self.db.commit()
        return task

    async def update_task(self, task: Task) -> Task:
        """Persist the full mutable surface of a task row."""
        task.updated_at = _now()
        row = task.to_row()
        await self.db.execute(
            """UPDATE tasks SET
                 external_id=:external_id, source=:source, title=:title,
                 description=:description, requirements=:requirements,
                 acceptance_criteria=:acceptance_criteria, repo_path=:repo_path,
                 kind=:kind, parent_id=:parent_id,
                 status=:status, blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority, context=:context, plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        await self.db.commit()
        return task

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

    async def create_attempt(self, task_id: str, attempt_number: int) -> str:
        # An earlier attempt of this task still 'in_progress' cannot be running:
        # attempts are serial, so a new one starting means the old process died
        # (kill -9, crash) without ever closing its row. Left alone, those rows
        # make `attempts.status` untrustworthy as a completion signal — the
        # baseline had three of them. Close them for what they are.
        await self.db.execute(
            "UPDATE attempts SET status = 'interrupted' "
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

    async def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        if not fields:
            return
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

    async def count_attempts(self, task_id: str) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE task_id = ?", (task_id,)
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # --------------------------- memories ---------------------------------- #
    # The human-confirmed learning queue (PLAN.md 4.5): proposals land here
    # with confirmed=0 and never enter the active rule set until a human
    # confirms them (avoids leniency-biased lessons accumulating silently).

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
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """List memories, optionally scoped to a project.

        When ``project`` is given, only rules/skills attached to that project are
        returned, plus globals (``project IS NULL``) unless ``include_global`` is
        False. When ``project`` is None, no project filter is applied (all rows).
        """
        clauses, params = [], []
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

    async def find_memory(self, prefix: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM memories WHERE id = ? OR id LIKE ? LIMIT 2",
            (prefix, prefix + "%"),
        )
        rows = await cur.fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    async def confirm_memory(self, mem_id: str) -> bool:
        """Promote a proposed memory into the active set (one-click confirm)."""
        cur = await self.db.execute(
            "UPDATE memories SET confirmed = 1, source = 'confirmed', "
            "updated_at = ? WHERE id = ?",
            (_now(), mem_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def delete_memory(self, mem_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        await self.db.commit()
        return cur.rowcount > 0

    # ----------------------- task events (persisted) ----------------------- #

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

    # ----------------------- project profiles ----------------------------- #

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

    async def update_project(self, project: "Project") -> None:
        row = project.to_row()
        await self.db.execute(
            "UPDATE projects SET name = :name, repo_paths = :repo_paths, "
            "primary_repo = :primary_repo, test_layers = :test_layers, "
            "updated_at = :updated_at WHERE id = :id",
            {**row, "updated_at": _now()},
        )
        await self.db.commit()

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
