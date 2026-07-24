"""Eval harness entrypoints (PLAN.md Part 21): golden-set replay + shadow mode.

``run_eval`` loads the golden set, replays each task in an isolated sandbox,
aggregates a scorecard, and diffs it against the previous run with a CI gate.
``run_shadow`` (21.3) runs a *real* incoming task end-to-end in a sandbox clone
and produces the draft PR locally — without pushing — for human comparison.
"""

from __future__ import annotations

import asyncio
import shutil
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

    created_tmp = workdir is None
    base_tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nh-eval-"))
    try:
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
    finally:
        # We own the sandbox only when we created it; a caller-supplied workdir
        # is theirs. Best-effort so cleanup never masks a real error (0.4 —
        # nh-eval-* dirs used to leak on every run, crash or not).
        if created_tmp:
            shutil.rmtree(base_tmp, ignore_errors=True)


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

    created_tmp = workdir is None
    base_tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nh-shadow-"))
    sandbox = base_tmp / "clone"
    # Local sandbox copy: no network, isolated inodes, instant on APFS
    # (same class as the bench sandbox — see northstar._sandbox_copy).
    from .northstar import _sandbox_copy
    _sandbox_copy(Path(repo_path), sandbox)
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=sandbox,
                   check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fdx"], cwd=sandbox,
                   check=True, capture_output=True)
    # Push-proof (review F2): the copy carries the source's REAL remotes and
    # the orchestrator pushes at finalize — strip them all and point origin
    # at a throwaway local bare so ShadowResult's "never pushes" is true by
    # construction, not by luck.
    shadow_bare = base_tmp / "shadow-remote.git"
    subprocess.run(["git", "init", "--bare", str(shadow_bare)],
                   check=True, capture_output=True)
    _remotes = subprocess.run(["git", "remote"], cwd=sandbox,
                              capture_output=True, text=True).stdout.split()
    for _r in _remotes:
        subprocess.run(["git", "remote", "remove", _r], cwd=sandbox,
                       capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(shadow_bare)],
                   cwd=sandbox, check=True, capture_output=True)
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
        # B6: neither run_shadow nor the backend bounds a coder TURN by wall
        # clock (only max_turns), so a hung Claude SDK subprocess would wedge
        # the run forever at 0% CPU. Bound the whole task and fail honestly on
        # timeout. Safe to cancel: the sandbox is a throwaway clone dropped in
        # the finally below. Generous default so a legitimately long run is not
        # cut off; override via bounds.shadow_timeout_s.
        timeout_s = float((config.get("bounds") or {}).get("shadow_timeout_s") or 1800)
        try:
            outcome = await asyncio.wait_for(orch.run_task(task), timeout=timeout_s)
        except asyncio.TimeoutError:
            if on_event:
                on_event({"source": "shadow", "kind": "shadow_timeout",
                          "text": f"shadow aborted after {timeout_s:.0f}s — a hung "
                                  "backend turn; failed honestly instead of wedging"})
            diff = subprocess.run(
                ["git", "diff", base_sha, "HEAD"], cwd=sandbox,
                capture_output=True, text=True,
            ).stdout
            return ShadowResult(
                task_id=task.id, outcome_status="timed_out",
                draft_diff=diff, pushed=False,
                notes=f"shadow run timed out after {timeout_s:.0f}s (no coder-turn "
                      "watchdog — B6); the backend subprocess hung, no diff forced.",
            )
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
        # ShadowResult (incl. draft_diff) is already built before this runs, so
        # the clone is safe to drop. Only when we created the tmp (0.4).
        if created_tmp:
            shutil.rmtree(base_tmp, ignore_errors=True)
