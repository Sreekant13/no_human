"""Project — a named group of repos for multi-repo task orchestration.

A project is the unit of work: when a user creates a task, they pick a project
(not a bare repo_path).  The project's ``primary_repo`` becomes the task's
``repo_path``; additional repos become ``linked_repos``.  Rules/skills scoped
to a project use ``memories.project = project.name``.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Project:
    id: str
    name: str
    repo_paths: list[str] = field(default_factory=list)
    primary_repo: str | None = None
    test_layers: str = "[]"  # JSON-encoded TestPlan layers (Phase 6a)
    created_at: str | None = None
    updated_at: str | None = None

    @staticmethod
    def new(name: str, *, repo_paths: list[str] | None = None,
            primary_repo: str | None = None) -> "Project":
        paths = repo_paths or []
        return Project(
            id=uuid.uuid4().hex,
            name=name,
            repo_paths=paths,
            primary_repo=primary_repo or (paths[0] if paths else None),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "repo_paths": json.dumps(self.repo_paths),
            "primary_repo": self.primary_repo,
            "test_layers": self.test_layers,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any] | Any) -> "Project":
        d = dict(row)
        return cls(
            id=d["id"],
            name=d["name"],
            repo_paths=json.loads(d.get("repo_paths") or "[]"),
            primary_repo=d.get("primary_repo"),
            test_layers=d.get("test_layers") or "[]",
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )

    @property
    def test_plan(self):
        """Return a TestPlan from the stored layers JSON (Phase 6a)."""
        from .testing.test_layers import TestPlan
        return TestPlan.from_json(self.test_layers)

    @property
    def linked_repos(self) -> list[str]:
        """All repos other than the primary."""
        return [r for r in self.repo_paths if r != self.primary_repo]
