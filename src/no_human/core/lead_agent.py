"""LeadAgent: compound task decomposition and DAG-based sub-task orchestration.

Lifecycle:
  1. Planner detects complexity → emits a DECOMPOSE_PLAN block
  2. Orchestrator._drive() creates a LeadAgent and calls decompose() + park()
  3. decompose() creates sub-tasks in the DB with parent_id linkage
  4. park() sets the parent to COMPOUND_PARENT and returns immediately,
     freeing the worker slot for sub-tasks to use
  5. Scheduler._run() calls check_completion() after each sub-task finishes
  6. check_completion() unblocks dependents, handles failures, and marks
     the parent DONE when all sub-tasks complete

Sub-tasks are first-class tasks dispatched by the existing Scheduler.tick().
The LeadAgent never runs agents directly — it only creates/monitors tasks.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from .db import Store
from .task import Task, TaskStatus, TERMINAL_STATES

log = logging.getLogger("no_human.lead_agent")

MAX_SUBTASKS_DEFAULT = 50
POLL_INTERVAL_SECONDS = 15.0
MAX_RETRIES_PER_SUBTASK = 1


class DecompositionError(ValueError):
    """Raised when a decomposition plan is invalid."""


class LeadAgent:
    """Coordinates compound task decomposition and execution."""

    def __init__(
        self,
        store: Store,
        *,
        config: dict[str, Any] | None = None,
        emit: Any | None = None,
    ):
        self.store = store
        self.config = config or {}
        self._emit = emit or (lambda kind, text, **kw: None)
        la_cfg = self.config.get("lead_agent", {})
        self.max_subtasks = la_cfg.get("max_subtasks", MAX_SUBTASKS_DEFAULT)
        self.poll_interval = la_cfg.get("poll_interval", POLL_INTERVAL_SECONDS)

    # ---------------------------------------------------------------------- #
    # Decomposition                                                           #
    # ---------------------------------------------------------------------- #

    async def decompose(self, parent: Task, plan: dict) -> list[Task]:
        """Create sub-tasks from a decomposition plan dict.

        ``plan`` has the shape emitted by the planner:
          {"decompose": true, "subtasks": [...], "justification": "..."}

        Each subtask entry:
          {"title": str, "kind": str, "description": str,
           "acceptance_criteria": [str], "depends_on": [str],
           "repo_path": str | None, "linked_repos": [str]}

        Returns the list of created Task objects.
        """
        subtask_specs = plan.get("subtasks", [])
        if not subtask_specs:
            raise DecompositionError("decomposition plan has no subtasks")
        if len(subtask_specs) > self.max_subtasks:
            raise DecompositionError(
                f"decomposition plan has {len(subtask_specs)} subtasks "
                f"(max {self.max_subtasks})"
            )

        self._validate_dag(subtask_specs)

        # Build title → index map for dependency resolution.
        title_map: dict[str, int] = {}
        for i, spec in enumerate(subtask_specs):
            t = spec.get("title", "").strip()
            if not t:
                raise DecompositionError(f"subtask {i} has no title")
            title_map[t] = i

        created: list[Task] = []
        title_to_id: dict[str, str] = {}

        for i, spec in enumerate(subtask_specs):
            title = spec["title"].strip()
            deps = spec.get("depends_on", [])
            dep_ids = [title_to_id[d] for d in deps if d in title_to_id]
            has_unmet_deps = bool(deps)

            task = Task.new(
                title=title,
                source=parent.source,
                repo_path=spec.get("repo_path") or parent.repo_path,
                description=spec.get("description"),
                kind=spec.get("kind", "feature"),
                parent_id=parent.id,
            )
            task.acceptance_criteria = spec.get("acceptance_criteria", [])
            task.linked_repos = spec.get("linked_repos", []) or parent.linked_repos
            task.priority = parent.priority
            task.config = dict(parent.config)

            # Store dependency metadata in context.
            task.context = {
                "depends_on_ids": dep_ids,
                "depends_on_titles": deps,
                "parent_title": parent.title,
            }

            if has_unmet_deps:
                task.status = TaskStatus.BLOCKED
                task.blocker = {
                    "category": "DEPENDENCY_WAIT",
                    "wake_condition": "subtask_deps_met",
                    "root_cause_hypothesis": (
                        f"Waiting for dependencies: {', '.join(deps)}"
                    ),
                    "goal": title,
                    "confidence": 1.0,
                    "transient": False,
                }

            await self.store.create_task(task)
            title_to_id[title] = task.id
            created.append(task)
            log.info(
                "sub-task %s/%s created: %s [%s] %s",
                i + 1, len(subtask_specs), task.id[:8], task.kind,
                "BLOCKED" if has_unmet_deps else "PENDING",
            )

        self._emit(
            "decompose",
            f"created {len(created)} sub-tasks "
            f"({sum(1 for t in created if t.status == TaskStatus.PENDING)} ready, "
            f"{sum(1 for t in created if t.status == TaskStatus.BLOCKED)} waiting)",
        )
        return created

    # ---------------------------------------------------------------------- #
    # Park (non-blocking) — replaces the old blocking drive() poll loop       #
    # ---------------------------------------------------------------------- #

    async def park_parent(self, parent: Task) -> "TaskOutcome":
        """Set parent to COMPOUND_PARENT and return immediately.

        The worker slot is freed as soon as this returns.  Sub-task
        completion is detected by the Scheduler calling check_completion()
        after each sub-task finishes.
        """
        from .orchestrator import TaskOutcome

        await self.store.set_status(
            parent, TaskStatus.COMPOUND_PARENT, validate=False,
        )
        self._emit(
            "state", "compound_parent",
            status="compound_parent",
        )
        return TaskOutcome(
            parent, status=TaskStatus.COMPOUND_PARENT,
            detail="parked — sub-tasks dispatched to scheduler",
        )

    # ---------------------------------------------------------------------- #
    # Completion check — called by the Scheduler after each sub-task ends     #
    # ---------------------------------------------------------------------- #

    async def check_completion(self, parent: Task) -> bool:
        """Evaluate compound task progress after a sub-task finishes.

        Returns True if the parent reached a terminal state (DONE, FAILED,
        ESCALATED) and False if sub-tasks are still in progress.
        """
        subtasks = await self.store.list_subtasks(parent.id)
        if not subtasks:
            await self.store.set_status(
                parent, TaskStatus.FAILED, validate=False,
            )
            return True

        done = [t for t in subtasks if t.status == TaskStatus.DONE]
        failed = [t for t in subtasks if t.status == TaskStatus.FAILED]

        # Unblock sub-tasks whose dependencies are now met.
        unblocked = await self._unblock_ready(parent.id, subtasks)
        if unblocked:
            self._emit("unblock", f"unblocked {unblocked} sub-task(s)")

        # All done → quality gate → mark parent DONE.
        if len(done) == len(subtasks):
            gate_cfg = self.config.get("lead_agent", {})
            if gate_cfg.get("quality_gate", False):
                gate_result = await self._run_quality_gate(parent, done)
                if gate_result is not None:
                    return True  # quality gate set parent to ESCALATED

            self._emit(
                "compound_done",
                f"all {len(subtasks)} sub-tasks completed",
            )
            await self.store.set_status(
                parent, TaskStatus.DONE, validate=False,
            )
            return True

        # Handle failures: retry once (via context flag), then escalate.
        for t in failed:
            retried_ids = set((parent.context or {}).get("retried_subtask_ids", []))
            if t.id not in retried_ids:
                retried_ids.add(t.id)
                ctx = parent.context or {}
                ctx["retried_subtask_ids"] = list(retried_ids)
                parent.context = ctx
                await self.store.update_task(parent)
                log.info("retrying failed sub-task %s", t.id[:8])
                self._emit("retry", f"retrying sub-task: {t.title}")
                await self.store.set_status(
                    t, TaskStatus.PENDING, validate=False,
                )
            else:
                self._emit(
                    "escalate",
                    f"sub-task failed after retry: {t.title}",
                )
                parent.blocker = {
                    "category": "SCOPE_EXPLOSION",
                    "question": (
                        f"Sub-task '{t.title}' failed after retry. "
                        f"Review the failure and decide how to proceed."
                    ),
                    "goal": parent.title,
                    "confidence": 0.8,
                }
                await self.store.update_task(parent)
                await self.store.set_status(
                    parent, TaskStatus.ESCALATED, validate=False,
                )
                return True

        return False  # still in progress

    async def _unblock_ready(
        self, parent_id: str, subtasks: list[Task] | None = None,
    ) -> int:
        """Move sub-tasks from BLOCKED→PENDING when all deps are DONE.

        Branch-chaining: if the single dependency produced a PR branch
        (``context["pr_branch"]``), propagate it as ``context["base_branch"]``
        on the dependent sub-task so it starts from the dependency's work
        instead of the pre-decomposition base.
        """
        if subtasks is None:
            subtasks = await self.store.list_subtasks(parent_id)

        done_ids = {t.id for t in subtasks if t.status == TaskStatus.DONE}
        task_by_id = {t.id: t for t in subtasks}
        unblocked = 0

        for t in subtasks:
            if t.status != TaskStatus.BLOCKED:
                continue
            wake = (t.blocker or {}).get("wake_condition", "")
            if wake != "subtask_deps_met":
                continue
            dep_ids = (t.context or {}).get("depends_on_ids", [])
            if not dep_ids or all(d in done_ids for d in dep_ids):
                # Branch-chaining: propagate dependency's PR branch.
                if len(dep_ids) == 1:
                    dep_task = task_by_id.get(dep_ids[0])
                    dep_branch = (
                        (dep_task.context or {}).get("pr_branch")
                        if dep_task else None
                    )
                    if dep_branch:
                        ctx = t.context or {}
                        ctx["base_branch"] = dep_branch
                        t.context = ctx
                        log.info(
                            "chain base_branch=%s from dep %s → sub-task %s",
                            dep_branch, dep_ids[0][:8], t.id[:8],
                        )
                    else:
                        log.warning(
                            "dep %s has no pr_branch; sub-task %s will use "
                            "default base branch",
                            dep_ids[0][:8], t.id[:8],
                        )

                t.blocker = None
                await self.store.update_task(t)
                await self.store.set_status(t, TaskStatus.PENDING)
                unblocked += 1
                log.info("unblocked sub-task %s (deps met)", t.id[:8])

        return unblocked

    # ---------------------------------------------------------------------- #
    # Quality gate                                                            #
    # ---------------------------------------------------------------------- #

    async def _run_quality_gate(
        self, parent: Task, done: list[Task],
    ) -> "TaskOutcome | None":
        """Check that completed sub-tasks integrate cleanly.

        Returns None on pass (caller proceeds to DONE), or a TaskOutcome
        on failure (caller returns it immediately). Current implementation:
        run the repo's test suite on the combined branch. If tests fail,
        escalate with integration failure details.
        """
        from .orchestrator import TaskOutcome

        self._emit("quality_gate", "running integration check on combined work")

        repo_path = parent.repo_path
        if not repo_path:
            return None  # no repo → nothing to integrate

        import subprocess
        from pathlib import Path

        repo = Path(repo_path)
        if not (repo / ".git").is_dir():
            return None

        # Try to detect and run the test command.
        from ..profile import ProjectProfile
        prof = None
        try:
            prof = await self.store.get_profile(repo_path)
        except Exception:  # noqa: BLE001
            pass

        test_cmd = (prof.test_cmd if prof and prof.confirmed else None)
        if not test_cmd:
            self._emit("quality_gate", "skipped (no confirmed test command)")
            return None

        try:
            proc = subprocess.run(
                test_cmd, shell=True, capture_output=True, text=True,
                timeout=300, cwd=str(repo),
            )
        except subprocess.TimeoutExpired:
            self._emit("quality_gate_failed", "integration tests timed out (300s)")
            parent.blocker = {
                "category": "SCOPE_EXPLOSION",
                "question": "Integration tests timed out after all sub-tasks completed.",
                "goal": parent.title,
            }
            await self.store.update_task(parent)
            await self.store.set_status(parent, TaskStatus.ESCALATED, validate=False)
            return TaskOutcome(
                parent, status=TaskStatus.ESCALATED,
                detail="quality gate: integration tests timed out",
            )

        if proc.returncode != 0:
            snippet = (proc.stdout + proc.stderr)[-500:]
            self._emit("quality_gate_failed", f"integration tests failed:\n{snippet}")
            parent.blocker = {
                "category": "SCOPE_EXPLOSION",
                "question": (
                    f"Integration tests failed after all sub-tasks completed. "
                    f"Output:\n{snippet}"
                ),
                "goal": parent.title,
            }
            await self.store.update_task(parent)
            await self.store.set_status(parent, TaskStatus.ESCALATED, validate=False)
            return TaskOutcome(
                parent, status=TaskStatus.ESCALATED,
                detail=f"quality gate: integration tests failed (rc={proc.returncode})",
            )

        self._emit("quality_gate", "integration tests passed")
        return None  # pass → caller proceeds to DONE

    # ---------------------------------------------------------------------- #
    # DAG validation                                                          #
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _validate_dag(subtask_specs: list[dict]) -> None:
        """Topological sort to detect cycles. Raises DecompositionError on cycle.

        v1 scope: linear chains only — each sub-task may depend on at most one
        other sub-task.  Multi-parent dependencies (diamond DAGs) are rejected
        because branch-chaining cannot safely determine a base branch when
        multiple parents produce independent PR branches.
        """
        titles = {s.get("title", "").strip() for s in subtask_specs}
        adj: dict[str, list[str]] = defaultdict(list)
        in_degree: dict[str, int] = {t: 0 for t in titles}

        for spec in subtask_specs:
            title = spec.get("title", "").strip()
            deps = spec.get("depends_on", [])
            if len(deps) > 1:
                raise DecompositionError(
                    f"subtask '{title}' has {len(deps)} dependencies — "
                    "multi-parent decomposition not yet supported. "
                    "Restructure as a linear chain or independent sub-tasks."
                )
            for dep in deps:
                dep = dep.strip()
                if dep not in titles:
                    raise DecompositionError(
                        f"subtask '{title}' depends on unknown '{dep}'"
                    )
                adj[dep].append(title)
                in_degree[title] = in_degree.get(title, 0) + 1

        # Kahn's algorithm.
        queue = [t for t, d in in_degree.items() if d == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited != len(titles):
            raise DecompositionError(
                "dependency cycle detected in decomposition plan"
            )
