"""The orchestrator: a small state machine that drives the per-task loop.

The *thinking* is the Claude Agent SDK session's job. The orchestrator supplies
the prompt, owns the deterministic git/PR steps (Part 16 #3: never LLM-generated
git), enforces the bounds (§3.5), runs the tamper guard + tests, and routes
blockers. It never merges.

Phase 0 implements the spine: context (minimal) -> planning (folded) ->
implement -> self-check (advisory) -> review (advisory pass-through; the real
independent reviewer lands in Phase 2) -> test (+ tamper guard) -> finalize
(commit, push, open PR, notify) -> awaiting_approval. The blocker-triage hook is
wired as a state with a stubbed taxonomy (full Part 22 in Phase 5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..agent.claude_backend import AgentEvent, ClaudeBackend
from ..blockers import (
    Blocker,
    BlockerCategory,
    blocker_prompt_suffix,
    fallback_blocker,
    notification_line,
    parse_blocker,
    render_report,
    triage,
)
from ..ci.base import CIResult
from ..notify.slack import SlackNotifier
from ..review import selfcheck
from ..review.reviewer import AdversarialReviewer, ReviewDecision
from ..testing import runner
from ..vcs import GitRepo, ProtectedBranch, open_pr
from .bounds import Bounds, QuotaExhausted, StuckDetector
from .db import Store
from .task import Task, TaskStatus

log = logging.getLogger("no_human.orchestrator")

EventSink = Callable[[dict], None]


@dataclass
class TaskOutcome:
    task: Task
    pr_url: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    detail: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quota_signal(text: str) -> bool:
    t = text.lower()
    return any(
        s in t
        for s in ("usage limit", "quota", "rate_limit", "rate limit exceeded")
    )


class Orchestrator:
    def __init__(
        self,
        store: Store,
        config: dict[str, Any],
        backend: ClaudeBackend,
        notifier: SlackNotifier,
        *,
        event_sink: EventSink | None = None,
        context_gatherer: Any | None = None,
        reviewer: AdversarialReviewer | None = None,
        ci_runner: Any | None = None,
        learning_queue: Any | None = None,
    ):
        self.store = store
        self.config = config
        self.backend = backend
        self.notifier = notifier
        self.bounds = Bounds.from_config(config.get("bounds"))
        self._sink = event_sink or (lambda e: None)
        self.context_gatherer = context_gatherer
        self.reviewer = reviewer
        self.ci_runner = ci_runner
        self.learning_queue = learning_queue

    # ----------------------------- events ---------------------------------- #

    def emit(self, kind: str, text: str = "", **meta: Any) -> None:
        self._sink({"source": "orchestrator", "kind": kind, "text": text, **meta})

    def _agent_sink(self, event: AgentEvent) -> None:
        self._sink(
            {
                "source": "agent",
                "kind": event.kind,
                "text": event.text,
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
                **event.meta,
            }
        )

    # ------------------------------ driver --------------------------------- #

    async def run_task(self, task: Task) -> TaskOutcome:
        """Drive a task through up to ``max_attempts`` attempts."""
        if not task.repo_path:
            return await self._fail(task, "no repo_path set on task")

        repo = self._open_repo(task)
        if repo is None:
            return await self._fail(task, f"not a git repo: {task.repo_path}")

        # Walk the pre-implementation spine. Context/planning are minimal in
        # Phase 0 (real gathering = Phase 1); the states are honoured so the
        # transition map and the board reflect the true lifecycle.
        if task.status == TaskStatus.PENDING:
            await self.store.set_status(task, TaskStatus.CONTEXT)
            self.emit("state", "context", status="context")
            await self._gather_context(task)
            await self.store.set_status(task, TaskStatus.PLANNING)
            self.emit("state", "planning", status="planning")

        # Capture the base branch once and PERSIST it on the task. Re-deriving
        # from current_branch() is wrong on two axes: (1) within a run, after a
        # failed attempt the head points at a feature branch; (2) across runs, a
        # resumed task (nh reply / wake) is checked out on the parked feature
        # branch, so deriving base from it would open a PR with base == head.
        ctx = task.context or {}
        if not ctx.get("base_branch"):
            ctx["base_branch"] = repo.current_branch()
            task.context = ctx
            await self.store.update_task(task)
        base_branch = ctx["base_branch"]

        # A human-confirmed, proven ProjectProfile (nh onboard) is the source of
        # truth for how to test/build this repo and which CI to drive — it
        # replaces the detect_command heuristic. Resolve it once per run: surface
        # the proven test command and, when CI wasn't explicitly injected, build
        # the profile's CI backend. An explicit injection always wins.
        prof = await self._usable_profile(repo.path)
        if prof:
            self.emit("profile",
                      f"using confirmed profile (test: {prof.test_cmd!r}"
                      + (f", ci: {prof.ci.get('backend')}" if prof.ci else "") + ")")
            if self.ci_runner is None and prof.ci:
                from ..ci import ci_from_config
                try:
                    built = ci_from_config({"ci": prof.ci})
                except Exception as exc:  # noqa: BLE001
                    built = None
                    log.warning("CI from profile failed: %s", exc)
                if built is not None:
                    self.ci_runner = built
                    self.emit("ci_backend", f"CI from profile: {built.name}")

        outcome = TaskOutcome(task, status=task.status, detail="")
        for attempt_n in range(1, self.bounds.max_attempts + 1):
            self.emit("attempt_start", f"attempt {attempt_n}/{self.bounds.max_attempts}")
            try:
                outcome = await self._run_attempt(task, repo, attempt_n, base_branch)
            except QuotaExhausted as exc:
                return await self._park_quota(task, exc)
            # Only a plain FAILED attempt is retried (bounded exploration, 22.3).
            # Any off-ramp (escalated / awaiting_input / blocked / paused_quota)
            # or a ready PR returns immediately — never retry blindly.
            if outcome.status != TaskStatus.FAILED:
                return outcome
            self.emit("attempt_failed", outcome.detail)

        # Bounds exhausted -> escalate with a diagnosis built from the attempts
        # (never fake done). 22.3: ≤2 distinct alternatives, then escalate.
        return await self._escalate_exhausted(task, repo, base_branch)

    async def _run_attempt(
        self, task: Task, repo: GitRepo, attempt_n: int, base: str | None = None
    ) -> TaskOutcome:
        attempt_id = await self.store.create_attempt(task.id, attempt_n)
        stuck = StuckDetector()

        # --- branch (deterministic git; agent never touches git) ---
        # Include attempt_n so each attempt uses a distinct branch. This avoids
        # non-fast-forward rejection when pushing attempt 2+ (the remote already
        # holds attempt 1's commit) without needing force-push.
        branch = (
            f"{self.config['git']['branch_prefix']}{task.id[:8]}"
            f"{f'-{attempt_n}' if attempt_n > 1 else ''}"
        )
        # base is passed from run_task (captured once before the attempt loop) so
        # we always branch off the original base, not a prior attempt's branch.
        if base is None:
            base = repo.current_branch()
        try:
            repo.create_branch(branch, base=base)
        except ProtectedBranch as exc:
            return await self._escalate(task, str(exc))
        await self.store.update_attempt(attempt_id, branch_name=branch)

        # --- implement (the SDK session) ---
        await self.store.set_status(task, TaskStatus.IMPLEMENTING)
        self.emit("state", "implementing", status="implementing")
        prompt = self._build_implement_prompt(task)
        result = await self.backend.run(
            prompt,
            cwd=repo.path,
            max_turns=self.bounds.max_turns_per_attempt,
            effort="high",
            on_event=self._agent_sink,
        )
        await self.store.update_attempt(
            attempt_id, turns_used=result.num_turns, tokens_used=result.tokens_used,
        )
        if result.is_error and _quota_signal(result.final_text or ""):
            raise QuotaExhausted()

        # A terminal agent error that isn't a quota signal (hit max_turns, SDK /
        # process error) is a FAILED attempt — never a crash, and never a silent
        # commit of half-finished work. Record it and let the bounded loop retry,
        # then escalate honestly once attempts are exhausted (constraint #5, 22.3).
        if result.is_error:
            reason = result.stop_reason or "error"
            is_stuck = stuck.record(result.final_text or reason)
            if is_stuck:
                self.emit("stuck", "same agent-error signature repeated; resetting context")
            detail = f"agent run did not complete ({reason})"
            await self.store.update_attempt(
                attempt_id, status="failed", failure_reason=detail,
            )
            self.emit("agent_error", detail)
            return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)

        # The agent may self-report a structural blocker (Part 22) instead of
        # lowering the bar. Honour it: checkpoint WIP and route by taxonomy.
        emitted = parse_blocker(result.final_text or "")
        if emitted is not None:
            emitted.goal = emitted.goal or task.title
            await self.store.update_attempt(
                attempt_id, status="failed",
                failure_reason=f"agent blocker: {emitted.category.value}",
            )
            return await self._raise_blocker(task, emitted, repo=repo, branch=branch)

        # --- commit (deterministic) ---
        if not repo.has_changes():
            detail = "agent produced no file changes"
            await self.store.update_attempt(attempt_id, status="failed", failure_reason=detail)
            return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)
        commit_msg = self._commit_message(task)
        commit = repo.commit_all(commit_msg)
        await self.store.update_attempt(attempt_id, commit_sha=commit.sha)
        self.emit("commit", f"{commit.sha[:8]} ({commit.files_changed} files)")

        # --- safety: change-size limits ---
        over = self._over_size_limits(commit)
        if over:
            # SCOPE_EXPLOSION (22.2): stop, escalate with a proposed smaller scope.
            blocker = Blocker(
                category=BlockerCategory.SCOPE_EXPLOSION,
                transient=False, confidence=0.9, goal=task.title,
                root_cause_hypothesis=over, evidence=over,
                question="This change exceeds the safety size limits. Approve a "
                         "larger scope, or split the task into smaller PRs?",
                options=["split into smaller tasks", "raise the limit for this task"],
            )
            return await self._raise_blocker(task, blocker, repo=repo, branch=branch)

        # --- review: tamper guard first (cheap, deterministic pre-filter),
        #     then adversarial reviewer (the real gate, §3.3) ---
        await self.store.set_status(task, TaskStatus.REVIEWING)
        self.emit("state", "reviewing", status="reviewing")

        # Tamper guard fires before spending reviewer tokens. A net reduction in
        # tests/assertions is reward hacking; escalate immediately.
        tamper = runner.tamper_check_between(repo.path)
        self.emit("tamper", tamper.summary, tampered=tamper.tampered)
        if tamper.tampered:
            await self.store.update_attempt(
                attempt_id, status="failed",
                test_results={"tamper_flag": True, "reasons": tamper.reasons},
            )
            return await self._escalate(
                task,
                "test-tampering detected — net reduction in tests/assertions: "
                + "; ".join(tamper.reasons),
                repo=repo, branch=branch,
            )

        decision = await self._run_review(task, repo, attempt_id)
        if not decision.passed:
            failed = decision.failed_items
            detail = "review failed: " + "; ".join(
                f"{i.label}: {i.evidence}" for i in failed[:3]
            )
            await self.store.update_attempt(
                attempt_id,
                review_checklist=decision.as_dict(),
                review_passed=0,
                status="failed",
                failure_reason=detail,
            )
            return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)
        await self.store.update_attempt(
            attempt_id,
            review_checklist=decision.as_dict(),
            review_passed=1,
        )

        # --- test: run local suite, record results ---
        await self.store.set_status(task, TaskStatus.TESTING)
        self.emit("state", "testing", status="testing")
        test_cmd = await self._resolve_test_cmd(repo)
        test_result = runner.run_tests(repo.path, test_cmd)
        self.emit("tests", test_result.summary, ok=test_result.ok)
        await self.store.update_attempt(
            attempt_id,
            test_results={
                "ran": test_result.ran, "ok": test_result.ok,
                "passed": test_result.passed, "failed": test_result.failed,
                "errors": test_result.errors, "tamper_flag": False,
            },
        )
        if test_result.ran and not test_result.ok:
            is_stuck = stuck.record(test_result.output)
            detail = f"tests failed: {test_result.summary}"
            if is_stuck:
                self.emit("stuck", "same failure signature repeated; resetting context")
            await self.store.update_attempt(attempt_id, status="failed", failure_reason=detail)
            return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)

        # --- CI (if configured): push branch first, then trigger pipeline ---
        if self.ci_runner is not None:
            # Push now (review passed, local tests pass) so CI can access the branch.
            # open_pr in _finalize will no-op push since branch is already up to date.
            try:
                repo.push(branch)
            except ProtectedBranch as exc:
                return await self._escalate(task, str(exc), repo=repo, branch=branch)
            except Exception as exc:  # noqa: BLE001
                return await self._escalate(
                    task, f"push for CI failed: {exc}", repo=repo, branch=branch)

            ci_result = await self._run_ci(task, branch, attempt_id, stuck)
            if ci_result is not None and not ci_result.passed:
                if ci_result.infra_failure:
                    # TRANSIENT_INFRA with retries exhausted → escalate (22.2).
                    blocker = Blocker(
                        category=BlockerCategory.TRANSIENT_INFRA,
                        transient=True, confidence=0.8, goal=task.title,
                        root_cause_hypothesis="CI infra failure persisted after "
                        f"{self.ci_runner.max_infra_retries} retries",
                        evidence=ci_result.summary,
                        question="CI infrastructure is failing (not the change). "
                                 "Retry later or investigate the runner?",
                    )
                    return await self._raise_blocker(
                        task, blocker, repo=repo, branch=branch, escalate_now=True)
                detail = f"CI failed: {ci_result.summary}"
                await self.store.update_attempt(attempt_id, status="failed", failure_reason=detail)
                return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)

        # --- finalize: push + open PR (NEVER merge) + notify ---
        return await self._finalize(task, repo, branch, base, commit, attempt_id, result)

    async def _finalize(self, task, repo, branch, base, commit, attempt_id, result) -> TaskOutcome:
        title = self._commit_message(task)
        body = self._pr_body(task, commit, result)
        try:
            pr = open_pr(repo, branch, title, body, base=base,
                         github_hosts=self.config.get("git", {}).get("github_hosts"))
        except ProtectedBranch as exc:
            return await self._escalate(task, str(exc), repo=repo, branch=branch)
        except Exception as exc:  # noqa: BLE001
            return await self._escalate(
                task, f"opening PR failed: {exc}", repo=repo, branch=branch)
        await self.store.update_attempt(
            attempt_id, pr_url=pr.url, status="succeeded", completed_at=_now(),
            review_passed=1,
        )
        await self.store.set_status(task, TaskStatus.AWAITING_APPROVAL)
        self.emit("pr_open", pr.url, pr_kind=pr.kind, status="awaiting_approval")
        self.notifier.notify(
            "needs_approval",
            f"{task.title} — PR ready ({pr.kind}): {pr.url}. `nh approve {task.id[:8]}`",
        )
        await self._propose_learning(
            task, TaskStatus.AWAITING_APPROVAL,
            summary=(result.final_text or "").strip()[:500],
        )
        return TaskOutcome(task, pr_url=pr.url, status=TaskStatus.AWAITING_APPROVAL,
                           detail="PR opened; awaiting human approval")

    # --------------------------- off-ramps --------------------------------- #

    async def _fail(self, task: Task, detail: str) -> TaskOutcome:
        await self.store.set_status(task, TaskStatus.FAILED)
        self.emit("failed", detail, status="failed")
        self.notifier.notify("task_failed", f"{task.title}: {detail}")
        return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)

    async def _escalate(
        self, task: Task, detail: str, *, repo: GitRepo | None = None,
        branch: str | None = None, goal: str = "",
    ) -> TaskOutcome:
        """Escalate a deterministic orchestrator-side failure with a structured
        NOVEL_UNKNOWN report (never bare prose; 22.4)."""
        blocker = fallback_blocker(detail, goal=goal or task.title)
        return await self._raise_blocker(task, blocker, repo=repo, branch=branch)

    async def _escalate_exhausted(
        self, task: Task, repo: GitRepo, branch: str | None
    ) -> TaskOutcome:
        """Bounds exhausted: build a blocker whose `tried` reflects each attempt's
        failure reason (22.3 verifiable-progress trail)."""
        attempts = await self.store.list_attempts(task.id)
        tried = [
            f"attempt {a['attempt_number']}: {a.get('failure_reason') or a.get('status')}"
            for a in attempts if a.get("failure_reason") or a.get("status") == "failed"
        ]
        blocker = Blocker(
            category=BlockerCategory.NOVEL_UNKNOWN,
            transient=False,
            confidence=0.4,
            goal=task.title,
            root_cause_hypothesis=(
                f"max_attempts ({self.bounds.max_attempts}) reached without a "
                f"passing, untampered change. Last: {tried[-1] if tried else 'n/a'}"
            ),
            tried=tried,
            evidence=tried[-1] if tried else "no successful attempt",
            question="The agent could not complete this within bounds. Refine the "
                     "task, split it, or advise an approach.",
        )
        return await self._raise_blocker(task, blocker, repo=repo, branch=branch)

    async def _raise_blocker(
        self, task: Task, blocker: Blocker, *, repo: GitRepo | None = None,
        branch: str | None = None, escalate_now: bool = False,
    ) -> TaskOutcome:
        """Checkpoint WIP, route by taxonomy (22.2), persist, and notify by
        severity (22.6). The single funnel for every off-ramp.

        ``escalate_now`` forces ESCALATED regardless of taxonomy — used when a
        normally-parkable category (e.g. TRANSIENT_INFRA) has already exhausted
        its bounded auto-retries and must now reach a human.
        """
        # 1. Checkpoint: never lose work (22.5). Commit WIP as [WIP-BLOCKED].
        if repo is not None:
            sha = self._checkpoint_wip(repo, task)
            if sha:
                blocker.resume_commit = sha
            if branch:
                blocker.resume_branch = branch

        # 2. Route (with the low-confidence override from config).
        if escalate_now:
            from ..blockers import Route
            route = Route(TaskStatus.ESCALATED, notify_now=True, parked=False)
        else:
            route = triage(blocker, escalate_below_confidence=self._escalate_below_conf())

        # 3. Parked routes get a wake_check_at so the watcher re-evaluates.
        if route.parked:
            task.wake_check_at = self._wake_check_at(blocker)
        else:
            task.wake_check_at = None

        # 4. Persist the structured report and transition.
        task.blocker = blocker.to_dict()
        await self.store.update_task(task)
        await self.store.set_status(task, route.target_status, validate=False)

        kind = {
            TaskStatus.ESCALATED: "escalated",
            TaskStatus.AWAITING_INPUT: "awaiting_input",
            TaskStatus.BLOCKED: "blocked",
            TaskStatus.PAUSED_QUOTA: "paused_quota",
        }.get(route.target_status, "escalated")
        report = render_report(blocker, task_title=task.title, task_id=task.id)
        self.emit(kind, report, status=route.target_status.value,
                  blocker=blocker.to_dict())

        # 5. Notify only when a human must act now (22.6). Parked = silent.
        if route.notify_now:
            self.notifier.notify(
                "stuck",
                notification_line(blocker, task_title=task.title, task_id=task.id),
            )
        # 6. Learning: propose an anti-pattern for escalations (not for parked
        #    tasks that may still resolve themselves; 22.8).
        if route.target_status == TaskStatus.ESCALATED:
            await self._propose_learning(
                task, TaskStatus.ESCALATED, blocker=blocker.to_dict())
        return TaskOutcome(
            task, status=route.target_status,
            detail=blocker.root_cause_hypothesis or blocker.question or "",
        )

    async def _propose_learning(
        self, task: Task, status: TaskStatus, *, blocker: dict | None = None,
        summary: str = "",
    ) -> None:
        """Queue a human-confirmed learning proposal (4.5). Best-effort: a
        learning failure must never affect the task outcome."""
        if self.learning_queue is None:
            return
        try:
            mem_id = await self.learning_queue.propose_from_outcome(
                task, status=status, blocker=blocker, summary=summary)
            if mem_id:
                self.emit("learning_proposed", f"queued proposal {mem_id[:8]}")
        except Exception as exc:  # noqa: BLE001
            log.warning("learning proposal failed: %s", exc)

    def _checkpoint_wip(self, repo: GitRepo, task: Task) -> str:
        """Commit uncommitted work as [WIP-BLOCKED]; return the resume commit sha."""
        try:
            if repo.has_changes():
                commit = repo.commit_all(f"[WIP-BLOCKED] {self._commit_message(task)}")
                self.emit("checkpoint", f"WIP-BLOCKED {commit.sha[:8]}")
                return commit.sha
            return repo.head_sha()
        except Exception as exc:  # noqa: BLE001 — checkpoint must never crash routing
            log.warning("WIP checkpoint failed: %s", exc)
            return ""

    def _escalate_below_conf(self) -> float:
        return float(
            self.config.get("blockers", {}).get("escalate_on_low_confidence_below", 0.6)
        )

    def _wake_check_at(self, blocker: Blocker) -> str:
        """Compute the next watcher re-check stamp for a parked task. Time-based
        conditions resolve against this; richer conditions just get re-polled."""
        from ..blockers.wake import parse_duration
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        cond = (blocker.wake_condition or "").lower()
        if cond.startswith("after:"):
            dur = parse_duration(cond.split(":", 1)[1]) or timedelta(hours=1)
            return (now + dur).isoformat()
        poll = parse_duration(
            str(self.config.get("blockers", {}).get("wake_poll_interval", "10m"))
        ) or timedelta(minutes=10)
        return (now + poll).isoformat()

    async def _park_quota(self, task: Task, exc: QuotaExhausted) -> TaskOutcome:
        task.wake_check_at = exc.resets_at
        await self.store.update_task(task)
        await self.store.set_status(task, TaskStatus.PAUSED_QUOTA)
        self.emit("paused_quota", str(exc), status="paused_quota")
        self.notifier.notify("paused_quota", f"{task.title} paused: subscription quota")
        return TaskOutcome(task, status=TaskStatus.PAUSED_QUOTA, detail=str(exc))

    # --------------------------- helpers ----------------------------------- #

    async def _run_ci(
        self, task: Task, branch: str, attempt_id: str, stuck: StuckDetector
    ) -> "CIResult | None":
        """Trigger CI, wait, record results. Returns None if CI not configured."""
        if self.ci_runner is None:
            return None
        self.emit("ci_start", f"triggering CI for branch {branch}")
        try:
            ci_result = await self.ci_runner.trigger(branch)
        except Exception as exc:  # noqa: BLE001
            self.emit("ci_error", str(exc))
            from ..ci.base import CIResult as _CIResult, PipelineStatus
            return _CIResult(
                pipeline_id="", pipeline_url="",
                status=PipelineStatus.FAILED,
                infra_failure=True,
                parsed_output=f"CI runner raised: {exc}",
            )
        await self.store.update_attempt(
            attempt_id,
            ci_pipeline_id=ci_result.pipeline_id,
            ci_pipeline_url=ci_result.pipeline_url,
            ci_status=ci_result.status.value,
        )
        self.emit("ci", ci_result.summary, passed=ci_result.passed,
                  infra=ci_result.infra_failure, url=ci_result.pipeline_url)
        if not ci_result.passed and not ci_result.infra_failure:
            stuck.record(ci_result.parsed_output)
        return ci_result

    async def _run_review(
        self, task: Task, repo: GitRepo, attempt_id: str
    ) -> ReviewDecision:
        """Run the adversarial reviewer; fall back to advisory pass if none configured."""
        if self.reviewer is None:
            from ..review.selfcheck import ChecklistItem
            return ReviewDecision(
                passed=True,
                checklist=[ChecklistItem(
                    "advisory (no reviewer configured)", True,
                    "reviewer not wired — advisory pass-through",
                )],
            )

        # Collect test output to give the reviewer evidence to work with.
        test_cmd = await self._resolve_test_cmd(repo)
        test_result = runner.run_tests(repo.path, test_cmd)
        held_result = runner.run_held_out_tests(repo.path)

        self.emit("review_start", "running independent adversarial reviewer")
        try:
            decision = await self.reviewer.review(
                task,
                repo_path=repo.path,
                test_output=test_result.output if test_result.ran else "",
                held_out_output=held_result.output if held_result else "",
            )
        except Exception as exc:  # noqa: BLE001
            # Reviewer crash → fail closed (never pass-through on error).
            from ..review.selfcheck import ChecklistItem
            self.emit("review_error", str(exc))
            return ReviewDecision(
                passed=False,
                checklist=[ChecklistItem("reviewer run", False, f"reviewer crashed: {exc}")],
            )

        verdict = "PASS" if decision.passed else "FAIL"
        self.emit("review", verdict, passed=decision.passed,
                  failed_count=len(decision.failed_items))
        return decision

    async def _gather_context(self, task: Task) -> None:
        if not self.context_gatherer:
            return
        self.emit("context_gather", "gathering context")
        try:
            ctx = await self.context_gatherer.gather(task)
        except Exception as exc:  # noqa: BLE001 — context is best-effort
            self.emit("context_gather", f"context gathering failed: {exc}")
            return
        task.context = {**(task.context or {}), "gathered": ctx.to_dict()}
        await self.store.update_task(task)
        comp = ctx.completeness
        detail = f"{len(ctx.chunks)} chunks from {len({c.source for c in ctx.chunks})} sources"
        if comp and comp.missing:
            detail += f"; missing: {', '.join(comp.missing)}"
        self.emit("context", detail, complete=bool(comp and comp.ok))

    def _context_digest(self, task: Task, limit: int = 8) -> str:
        gathered = (task.context or {}).get("gathered") or {}
        chunks = gathered.get("chunks") or []
        if not chunks:
            return ""
        lines = ["Gathered context (read-only, for reference):"]
        for c in chunks[:limit]:
            lines.append(f"  [{c['source']}] {c['title']}")
        return "\n".join(lines)

    async def _usable_profile(self, repo_path) -> Any | None:
        """Return the repo's ProjectProfile only if a human confirmed it AND its
        test command was proven to run (``is_usable``); else None. Prefer the
        SQLite mirror; fall back to the repo's ``.no_human/project.yml``."""
        from ..profile import ProjectProfile
        prof = None
        try:
            prof = await self.store.get_profile(str(repo_path))
        except Exception as exc:  # noqa: BLE001
            log.warning("profile lookup failed: %s", exc)
        if prof is None:
            try:
                prof = ProjectProfile.load(repo_path)
            except Exception:  # noqa: BLE001
                prof = None
        return prof if (prof and prof.is_usable) else None

    async def _resolve_test_cmd(self, repo: GitRepo) -> str | None:
        """Resolve the test command: an explicit config override wins; else a
        usable profile's proven ``test_cmd``; else None so ``run_tests`` falls
        back to ``detect_command`` (the heuristic of last resort)."""
        explicit = self.config.get("tests", {}).get("command")
        if explicit:
            return explicit
        prof = await self._usable_profile(repo.path)
        if prof and prof.test_cmd:
            return prof.test_cmd
        return None

    def _open_repo(self, task: Task) -> GitRepo | None:
        try:
            return GitRepo(
                Path(task.repo_path),
                identity_name=self.config["git"]["agent_identity_name"],
                identity_email=self.config["git"]["agent_identity_email"],
                never_push_to=self.config["git"]["never_push_to"],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("open repo failed: %s", exc)
            return None

    def _over_size_limits(self, commit) -> str | None:
        safety = self.config.get("safety", {})
        max_files = safety.get("max_files_changed", 20)
        max_lines = safety.get("max_lines_changed", 500)
        total_lines = commit.insertions + commit.deletions
        if commit.files_changed > max_files:
            return f"change exceeds max_files_changed ({commit.files_changed} > {max_files})"
        if total_lines > max_lines:
            return f"change exceeds max_lines_changed ({total_lines} > {max_lines})"
        return None

    def _commit_message(self, task: Task) -> str:
        prefix = self.config["git"].get("commit_prefix", "")
        ext = f"{task.external_id}: " if task.external_id else ""
        return f"{prefix}{ext}{task.title}"

    def _build_implement_prompt(self, task: Task) -> str:
        criteria = "\n".join(f"  - {c}" for c in task.acceptance_criteria) or "  (none stated)"
        rules = (
            "Rules:\n"
            "  - Verify with evidence: run commands, read their output; don't assert.\n"
            "  - Minimal, focused edits. No comments unless the WHY is non-obvious.\n"
            "  - Add or update tests for your change and run them.\n"
            "  - NEVER weaken, skip, or delete a test to make things pass.\n"
            "  - Do NOT run any git command — branching, committing, pushing and\n"
            "    opening the PR are handled for you. Just edit files and run tests.\n"
        )
        digest = self._context_digest(task)
        resume = self._resume_digest(task)
        return (
            f"You are implementing a software task on the repo at {task.repo_path}.\n\n"
            f"Task: {task.title}\n"
            f"{('Description: ' + task.description) if task.description else ''}\n\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"{(digest + chr(10) + chr(10)) if digest else ''}"
            f"{(resume + chr(10) + chr(10)) if resume else ''}"
            f"{rules}\n"
            + selfcheck.build_prompt(task.title, task.acceptance_criteria)
            + blocker_prompt_suffix()
        )

    def _resume_digest(self, task: Task) -> str:
        """Seed a resumed task's fresh session with the prior blocker report and
        any human reply (22.5) — not a stale, bloated context."""
        parts: list[str] = []
        if task.blocker:
            b = Blocker.from_dict(task.blocker)
            parts.append(
                "You are resuming a previously-blocked task. Prior diagnosis:\n"
                f"  category: {b.category.value}\n"
                f"  why: {b.root_cause_hypothesis}\n"
                f"  tried: {'; '.join(b.tried) if b.tried else '(none)'}"
            )
        ctx = task.context or {}
        replies = ctx.get("human_replies") or []
        if replies:
            latest = replies[-1]
            parts.append(
                "A human answered your blocking question:\n"
                f"  Q: {latest.get('question', '')}\n"
                f"  A: {latest.get('answer', '')}\n"
                "Use this answer; do NOT re-ask. Do not lower the bar."
            )
        feedback = ctx.get("send_back_feedback") or []
        if feedback:
            parts.append(
                "Reviewer/human send-back feedback to address:\n"
                + "\n".join(f"  - {f.get('message', '')}" for f in feedback[-3:])
            )
        return "\n\n".join(parts)

    def _pr_body(self, task: Task, commit, result) -> str:
        criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria) or "- (none stated)"
        return (
            f"Automated change by no_human for task `{task.id[:8]}`.\n\n"
            f"## Task\n{task.title}\n\n"
            f"## Acceptance criteria\n{criteria}\n\n"
            f"## Implementation summary\n{(result.final_text or '').strip()[:2000]}\n\n"
            f"## Stats\n{commit.files_changed} files, "
            f"+{commit.insertions}/-{commit.deletions}, {result.num_turns} turns.\n\n"
            "> The agent does not merge. A human reviews and merges via `nh approve`."
        )
