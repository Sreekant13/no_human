"""Golden task set (PLAN.md 21.1) — frozen (input → known-good output) pairs.

Each golden task is a self-contained YAML file under ``eval/golden_tasks/``:

    id: add-mul
    title: "Add mul() to calc"
    description: "..."
    acceptance_criteria: ["mul(a,b) returns a*b"]
    setup:                       # files written into a fresh git repo @ base
      calc.py: "def add(a, b):\\n    return a + b\\n"
      test_calc.py: "..."
    known_good_diff: |           # the reference diff (for the intent judge)
      ...
    held_out_tests: |            # run against the agent's result → mergeable?
      from calc import mul
      def test_mul(): assert mul(2, 3) == 6
    impossible: false            # red-team: correct behavior is to ESCALATE
    tempts_tamper: false         # red-team: a flaky/underspecified trap

The ``setup`` files are committed as the pre-fix base; the agent works from
there. Nothing here touches the live system — replay clones into a sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GOLDEN_DIR = Path(__file__).resolve().parents[3] / "eval" / "golden_tasks"


@dataclass
class GoldenTask:
    id: str
    title: str
    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    setup: dict[str, str] = field(default_factory=dict)
    known_good_diff: str = ""
    held_out_tests: str = ""
    impossible: bool = False
    tempts_tamper: bool = False
    path: Path | None = None

    @property
    def is_red_team(self) -> bool:
        return self.impossible or self.tempts_tamper

    @staticmethod
    def from_dict(data: dict[str, Any], *, path: Path | None = None) -> "GoldenTask":
        return GoldenTask(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            acceptance_criteria=list(data.get("acceptance_criteria", []) or []),
            setup=dict(data.get("setup", {}) or {}),
            known_good_diff=data.get("known_good_diff", ""),
            held_out_tests=data.get("held_out_tests", ""),
            impossible=bool(data.get("impossible", False)),
            tempts_tamper=bool(data.get("tempts_tamper", False)),
            path=path,
        )


def load_golden_tasks(directory: Path = GOLDEN_DIR) -> list[GoldenTask]:
    """Load every ``*.yaml`` golden task from ``directory`` (sorted by id)."""
    directory = Path(directory)
    tasks: list[GoldenTask] = []
    if not directory.exists():
        return tasks
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        data = yaml.safe_load(path.read_text()) or {}
        if data:
            tasks.append(GoldenTask.from_dict(data, path=path))
    return sorted(tasks, key=lambda t: t.id)
