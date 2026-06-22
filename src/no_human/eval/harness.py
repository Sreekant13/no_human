"""Eval harness entrypoints (PLAN.md Part 21): golden-set replay + shadow mode.

``run_eval`` loads the golden set, replays each task in an isolated sandbox,
aggregates a scorecard, and diffs it against the previous run with a CI gate.
``run_shadow`` (21.3) runs a *real* incoming task end-to-end in a sandbox clone
and produces the draft PR locally — without pushing — for human comparison.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .golden import GoldenTask, load_golden_tasks
from .replay import ReplayRunner, TaskScore
from .scorecard import GateResult, Scorecard, ci_gate


@dataclass
class EvalRun:
    scorecard: Scorecard
    gate: GateResult
    previous: Scorecard | None = None


async def run_eval(
    config: dict,
    *,
    backend_factory: Callable[[GoldenTask], Any],
    golden_tasks: list[GoldenTask] | None = None,
    reviewer: Any | None = None,
    judge: Any | None = None,
    previous: Scorecard | None = None,
    now: str = "",
    on_event: Callable[[dict], None] | None = None,
    workdir: Path | None = None,
) -> EvalRun:
    """Replay the golden set and produce a gated scorecard."""
    tasks = golden_tasks if golden_tasks is not None else load_golden_tasks()
    runner = ReplayRunner(
        config, backend_factory=backend_factory, reviewer=reviewer,
        judge=judge, on_event=on_event,
    )
    scores: list[TaskScore] = []

    base_tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nh-eval-"))
    for golden in tasks:
        task_dir = base_tmp / golden.id
        task_dir.mkdir(parents=True, exist_ok=True)
        score = await runner.run_one(golden, workdir=task_dir)
        scores.append(score)
        if on_event:
            on_event({"kind": "golden_done", "task": golden.id, "correct": score.correct})

    card = Scorecard(scores=scores, created_at=now)
    gate = ci_gate(card, previous)
    return EvalRun(scorecard=card, gate=gate, previous=previous)


@dataclass
class ShadowResult:
    task_id: str
    outcome_status: str
    draft_diff: str = ""
    pushed: bool = False  # always False — shadow never pushes
    notes: str = ""


async def run_shadow(
    config: dict,
    *,
    repo_path: str,
    task_title: str,
    backend: Any,
    acceptance_criteria: list[str] | None = None,
    reviewer: Any | None = None,
    workdir: Path | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> ShadowResult:
    """Run a real task end-to-end in a sandbox CLONE — never pushing to the real
    remote (21.3). Produces the draft diff for human comparison."""
    import subprocess

    from ..core.db import Store
    from ..core.orchestrator import Orchestrator
    from ..core.task import Task
    from ..notify.slack import SlackNotifier

    base_tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nh-shadow-"))
    sandbox = base_tmp / "clone"
    # Local clone: no network, isolated from the real repo + remote.
    subprocess.run(["git", "clone", "--no-hardlinks", repo_path, str(sandbox)],
                   check=True, capture_output=True, text=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox, capture_output=True, text=True
    ).stdout.strip()

    store = await Store(base_tmp / "shadow.db").connect()
    try:
        orch = Orchestrator(store, config, backend, SlackNotifier(None),
                            reviewer=reviewer, event_sink=on_event)
        task = Task.new(task_title, repo_path=str(sandbox))
        task.acceptance_criteria = list(acceptance_criteria or [])
        await store.create_task(task)
        outcome = await orch.run_task(task)
        diff = subprocess.run(
            ["git", "diff", base_sha, "HEAD"], cwd=sandbox,
            capture_output=True, text=True,
        ).stdout
        return ShadowResult(
            task_id=task.id, outcome_status=outcome.status.value,
            draft_diff=diff, pushed=False,
            notes="shadow run — clone only, real remote untouched",
        )
    finally:
        await store.close()
