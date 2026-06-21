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
        await self.db.commit()

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
                 status=:status, blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority, context=:context, plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        await self.db.commit()
        return task

    # ---------------------------- attempts --------------------------------- #

    async def create_attempt(self, task_id: str, attempt_number: int) -> str:
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
