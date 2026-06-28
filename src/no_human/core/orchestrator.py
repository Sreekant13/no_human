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

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..agent.claude_backend import AgentEvent, ClaudeBackend
from ..agent.supervisor import SupervisorHook
from ..blockers import (
    Blocker,
    BlockerCategory,
    blocker_prompt_suffix,
    fallback_blocker,
    missing_access,
    notification_line,
    parse_blocker,
    render_report,
    triage,
)
from ..ci.base import CIResult, HumanGatedCI
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


def _ci_failure_unrelated(ci_result: "CIResult", changed_files: list[str]) -> str | None:
    """Relatedness triage (Phase 6.3, evidence-based — never numeric).

    Return cited evidence iff EVERY failing test maps to a file this change never
    touched (a pre-existing / monorepo-wide failure, not this PR). Return None
    when attribution is unclear (no failing-test names, or no diff info, or any
    overlap) — None routes into the bounded fix loop, so we never silently skip a
    failure that might be ours. Matching is by class/file stem, since CI reports
    test classes (``com.acme.analytics-export.AnalyticsExportE2EIT``) while the diff lists paths
    (``.../AnalyticsExportE2EIT.java``)."""
    failing = [j for j in ci_result.jobs if j.status == "failed"]
    if not failing or not changed_files:
        return None  # not enough evidence to attribute — fix-loop, don't skip
    # File stems from the diff (basename without extension), e.g.
    # ".../AnalyticsExportE2EIT.java" -> "analyticsexporte2eit".
    changed_stems = {
        s for s in (
            f.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() for f in changed_files if f
        ) if len(s) >= 3
    }
    unrelated_tests: list[str] = []
    for j in failing:
        # A CI test name is a dotted path whose segments include the class
        # (matching a file stem) and the method, e.g.
        # "com.acme.analytics-export.AnalyticsExportE2EIT.testExport". Split into segments and call
        # the test "related" if ANY changed file stem matches ANY segment
        # (exact or containment, to catch Foo vs FooTest). Conservative by
        # design: a test is only "unrelated" when NOTHING in the diff matches,
        # so we never falsely skip a failure that could be ours.
        segments = [
            seg for seg in re.split(r"[.#\[\]()]", j.name.lower()) if seg
        ]
        related = any(
            cs == seg or cs in seg or seg in cs
            for cs in changed_stems for seg in segments
        )
        if not related:
            unrelated_tests.append(j.name)
    if unrelated_tests and len(unrelated_tests) == len(failing):
        return (
            "failing tests not in any changed file: "
            + ", ".join(unrelated_tests[:10])
            + f" | changed files: {', '.join(sorted(changed_files)[:10])}"
        )
    return None


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
        # Feed assistant prose to the supervisor so it sees what the agent SAYS
        # (where "I can't access X" / unverified assumptions surface), not just
        # the tools it runs. Best-effort; the hook only acts on its check cadence.
        sv = getattr(self, "_active_supervisor", None)
        if sv is not None and event.text and event.kind in ("text", "assistant", "result"):
            sv.note_text(event.text)
        # Track files the agent intentionally modified so we only commit those
        # (not test side-effects like state files updated during test runs).
        if event.kind == "tool_use" and event.tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            inp = event.tool_input or {}
            path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
            if path:
                if not hasattr(self, "_agent_edited_files"):
                    self._agent_edited_files: set[str] = set()
                self._agent_edited_files.add(str(path))

    # ------------------------------ driver --------------------------------- #

    async def run_task(self, task: Task) -> TaskOutcome:
        """Drive a task to a terminal/parked outcome.

        Serial mode (default) operates on the repo's primary checkout — today's
        behaviour. When ``concurrency.enabled``, the task runs in its OWN git
        worktree (Phase 7) so many tasks — even in the same repo — never clobber
        each other's working tree/index/branch. All task state lives on committed
        branches in the shared object store, so the worktree is disposable: we
        remove it on return and recreate it on resume."""
        if not task.repo_path:
            return await self._fail(task, "no repo_path set on task")

        main_repo = self._open_repo(task)
        if main_repo is None:
            return await self._fail(task, f"not a git repo: {task.repo_path}")

        # Ensure remote refs are current before deriving the base branch —
        # avoids branching off stale state when the remote moved (e.g. a PR
        # was merged or another task pushed since we last fetched).
        main_repo.fetch()

        if not self._concurrency_enabled():
            return await self._drive(task, main_repo)

        # Worktree-isolated mode. Derive + persist the base from the PRIMARY
        # checkout before detaching a worktree (a detached worktree's
        # current_branch() is not the base).
        ctx = task.context or {}
        base = ctx.get("base_branch") or main_repo.current_branch()
        ctx["base_branch"] = base
        task.context = ctx
        await self.store.update_task(task)

        wt_path = self._worktree_path(task)
        try:
            repo = self._acquire_worktree(main_repo, wt_path, base)
        except Exception as exc:  # noqa: BLE001
            return await self._fail(task, f"could not create worktree: {exc}")
        try:
            return await self._drive(task, repo)
        finally:
            try:
                main_repo.remove_worktree(wt_path)
            except Exception as exc:  # noqa: BLE001 — cleanup must never mask outcome
                log.warning("worktree cleanup failed for %s: %s", task.id[:8], exc)

    async def _drive(self, task: Task, repo: GitRepo) -> TaskOutcome:
        """The per-task loop, operating on whichever checkout (primary or
        worktree) ``run_task`` hands it."""
        # Resume fast-path: a task parked on a human-gated CI step that is now
        # being resumed (status moved off PENDING by nh reply / wake) goes
        # straight to the PR — the gate is cleared and the change was already
        # verified before parking. Re-running the agent would only find nothing
        # to change and fail the attempt.
        hg = (task.context or {}).get("human_gated_ci")
        if hg and task.status != TaskStatus.PENDING:
            return await self._resume_human_gated(task, repo, hg)

        # Walk the pre-implementation spine. Context/planning are minimal in
        # Phase 0 (real gathering = Phase 1); the states are honoured so the
        # transition map and the board reflect the true lifecycle.
        if task.status == TaskStatus.PENDING:
            self.emit("kind", f"task kind: {task.kind}", task_kind=task.kind)

            # Code review tasks use a completely different pipeline: read-only
            # review of an external PR — no implementation, no branch, no push.
            if task.kind == "code_review":
                await self.store.set_status(task, TaskStatus.CONTEXT)
                self.emit("state", "context", status="context")
                await self._gather_context(task)
                return await self._run_code_review(task, repo)

            # Investigation tasks get wider bounds for deep debugging.
            if task.kind == "investigation":
                from .bounds import Bounds
                inv = self.config.get("bounds_investigation", {})
                self.bounds = Bounds(
                    max_attempts=inv.get("max_attempts", 8),
                    max_turns_per_attempt=inv.get("max_turns_per_attempt", 80),
                    escalate_after=inv.get("escalate_after", 6),
                    max_correction_rounds=inv.get("max_correction_rounds", 4),
                )
                self.emit("bounds", f"investigation bounds: {self.bounds.max_attempts}×{self.bounds.max_turns_per_attempt}")
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
        self._active_profile = prof
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

        # Pre-fetch confirmed rules + skills for prompt injection (Phase G).
        # Scope to this task's repo plus globals, so a rule learned for one
        # project never leaks into (or pollutes the context of) another.
        self._active_memories = await self.store.list_memories(
            confirmed=True, project=task.repo_path
        )

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
        self._agent_edited_files: set[str] = set()  # reset per attempt

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
        prompt = self._build_implement_prompt(task, str(repo.path))

        # Supervisor hook: a PostToolUse evaluator that course-corrects the
        # working agent in real time (replaces the human-in-the-loop).
        supervisor = self._build_supervisor(task, str(repo.path))
        self._active_supervisor = supervisor  # so _agent_sink can feed it agent prose
        if supervisor is not None:
            self.emit("supervisor", "supervisor active")

        # Pre-flight plan check (EVOLUTION_PLAN §1.2 #1): one cheap evaluation of
        # the agent's plan BEFORE it edits. Config-gated (default off) since it
        # spends an extra short planning turn; when a gap is found, the correction
        # rides into the implement prompt so the agent closes it from turn one.
        prompt = await self._maybe_preflight(task, repo, supervisor, prompt)

        # Per-edit lint feedback (B1): deterministic, runs alongside the
        # supervisor. Config-gated (default off) until validated; only fires when
        # a lint command is known for the repo.
        lint_hook = await self._build_lint_hook(repo)
        # Only pass lint_hook when active, so backends that predate the param
        # (e.g. test doubles) are unaffected while it stays default-off.
        extra = {"lint_hook": lint_hook} if lint_hook is not None else {}

        result = await self.backend.run(
            prompt,
            cwd=repo.path,
            max_turns=self.bounds.max_turns_per_attempt,
            effort="high",
            on_event=self._agent_sink,
            supervisor_hook=supervisor,
            **extra,
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
        # Only commit files the agent intentionally wrote/edited — not test
        # side-effects (e.g. state files updated by running vitest).
        edited = getattr(self, "_agent_edited_files", None)
        if edited:
            commit = repo.commit_paths(list(edited), commit_msg)
        else:
            commit = repo.commit_all(commit_msg)
        await self.store.update_attempt(attempt_id, commit_sha=commit.sha)
        self.emit("commit", f"{commit.sha[:8]} ({commit.files_changed} files)")

        # --- lint gate (cheap, deterministic — catches mechanical issues like
        #     import placement before spending reviewer tokens) ---
        lint_cmd = await self._resolve_lint_cmd(repo)
        if lint_cmd:
            changed = repo.changed_files()
            lint_result = await asyncio.to_thread(
                runner.run_lint_on_changed, repo.path, lint_cmd, changed,
            )
            self.emit("lint", lint_result.summary, ok=lint_result.ok)
            if lint_result.ran and not lint_result.ok:
                detail = f"lint failed: {lint_result.output[:500]}"
                await self.store.update_attempt(
                    attempt_id, status="failed", failure_reason=detail,
                )
                return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)

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
            # Feedback loop (EVOLUTION_PLAN §2.2): persist the reviewer's specific,
            # cited findings so the NEXT attempt's prompt targets them, instead of
            # blindly re-implementing. This reuses the bounded attempt loop
            # (max_attempts) — the tamper guard still fires first on every round,
            # so the worker cannot weaken tests to satisfy the reviewer.
            await self._record_review_feedback(task, failed)
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
        # Offload the (blocking) test subprocess to a thread so concurrent tasks'
        # agent phases keep progressing on the event loop (Phase 7).
        test_result = await asyncio.to_thread(runner.run_tests, repo.path, test_cmd)
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

            try:
                ci_result = await self._run_ci(task, branch, attempt_id, stuck)
            except HumanGatedCI as gated:
                # CI is human-gated (e.g. a Jenkins image build): park with a
                # wake condition and tell the human what to do — never mock/skip
                # the step. Review/tamper/local tests already passed (CI is last),
                # so on resume we go straight to the PR.
                return await self._park_human_gated_ci(task, gated, repo, branch, base)
            if ci_result is not None and not ci_result.passed:
                if getattr(ci_result, "access_failure", False):
                    # Access/permission wall (no token, 403) — not a code problem
                    # and not transient. Only a human can grant access: park with a
                    # MISSING_ACCESS ask naming the EXACT .env key when the backend
                    # surfaced one (WS-F), then `nh reply` resumes.
                    env_key = getattr(ci_result, "access_env_key", "") or ""
                    if env_key:
                        blocker = missing_access(
                            env_key, system=f"remote CI ({self.ci_runner.name})",
                            goal=task.title,
                            evidence=ci_result.parsed_output or ci_result.summary,
                        )
                    else:
                        blocker = Blocker(
                            category=BlockerCategory.MISSING_ACCESS,
                            transient=False, confidence=0.9, goal=task.title,
                            root_cause_hypothesis="Remote CI is unreachable due to "
                            "missing or insufficient credentials — not a code failure.",
                            evidence=ci_result.parsed_output or ci_result.summary,
                            question="no_human needs access to reach this pipeline. "
                                     "Provide the credential (e.g. a CI API token in "
                                     "~/.no_human/.env) or tell me how to reach it, then "
                                     "`nh reply` to resume.",
                        )
                    return await self._raise_blocker(
                        task, blocker, repo=repo, branch=branch)
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

                # Relatedness triage (Phase 6.3, evidence-based — never numeric):
                # if every failing test is in a file this change never touched,
                # this is a pre-existing / monorepo-wide failure, not ours.
                # Escalate with cited evidence rather than burn fix attempts on
                # code we didn't write.
                changed = self._safe_changed_files(repo, base)
                unrelated = _ci_failure_unrelated(ci_result, changed)
                if unrelated is not None:
                    blocker = Blocker(
                        category=BlockerCategory.NOVEL_UNKNOWN,
                        transient=False, confidence=0.7, goal=task.title,
                        root_cause_hypothesis="Remote CI is red, but the failing "
                        "tests are not in any file this change touched — likely a "
                        "pre-existing or monorepo-wide failure, not this PR.",
                        evidence=unrelated,
                        question="The remote build failed on tests unrelated to "
                                 "this change. Is this a known-flaky/pre-existing "
                                 "monorepo failure (retry/ignore), or should the "
                                 "agent investigate further?",
                    )
                    return await self._raise_blocker(
                        task, blocker, repo=repo, branch=branch, escalate_now=True)

                # Related (or attribution unknown): feed the real failure into the
                # next attempt's prompt so the agent fixes THIS, bounded by
                # max_attempts (never weaken tests to go green).
                await self._record_ci_failure(task, ci_result)
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
        # B4: mark the PR for comment-watching so the wake watcher polls it for
        # new human comments and auto-revises. The cursor starts at PR-open time
        # so only comments posted afterwards trigger a revision.
        ctx = task.context or {}
        ctx["pr_watch"] = pr.url
        ctx.setdefault("pr_comment_since", _now())
        task.context = ctx
        await self.store.update_task(task)

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
        notify_override: bool | None = None,
    ) -> TaskOutcome:
        """Checkpoint WIP, route by taxonomy (22.2), persist, and notify by
        severity (22.6). The single funnel for every off-ramp.

        ``escalate_now`` forces ESCALATED regardless of taxonomy — used when a
        normally-parkable category (e.g. TRANSIENT_INFRA) has already exhausted
        its bounded auto-retries and must now reach a human.

        ``notify_override`` forces the notification on/off regardless of the
        route's default — used to give the human a heads-up on a *parked* task
        they must still act on (e.g. a human-gated CI build), which otherwise
        parks silently.
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

        # 5. Notify only when a human must act now (22.6). Parked = silent,
        #    unless a notify_override says this parked task still needs a person.
        should_notify = route.notify_now if notify_override is None else notify_override
        if should_notify:
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

    async def _park_human_gated_ci(
        self, task: Task, gated: HumanGatedCI, repo: GitRepo, branch: str, base: str | None
    ) -> TaskOutcome:
        """Park on a human-gated CI step (DEPENDENCY_WAIT) with a wake condition
        and a heads-up notification. The branch is already pushed (push precedes
        CI), review/tamper/local tests already passed, so resuming opens the PR.
        """
        ctx = task.context or {}
        ctx["human_gated_ci"] = {"branch": branch, "base": base, "hint": gated.wake_hint}
        task.context = ctx
        blocker = Blocker(
            category=BlockerCategory.DEPENDENCY_WAIT,
            transient=True, confidence=0.9, goal=task.title,
            wake_condition=f"ci_green_on:{branch}",
            root_cause_hypothesis="CI for this backend is human-gated; a person "
            "must start the build/pipeline before it can verify the change.",
            evidence=str(gated),
            question=(gated.wake_hint or
                      "Start the gated CI pipeline; the task resumes when it is green."),
        )
        return await self._raise_blocker(
            task, blocker, repo=repo, branch=branch, notify_override=True)

    async def _resume_human_gated(self, task: Task, repo: GitRepo, hg: dict) -> TaskOutcome:
        """Resume a task parked on a human-gated CI: the gate is cleared (wake
        fired green, or a human resumed), the change was already reviewed/tested
        before parking, so go straight to the PR — no agent re-run (it would have
        nothing to change), no faked CI (a real human ran it)."""
        from types import SimpleNamespace

        branch = hg["branch"]
        base = hg.get("base") or (task.context or {}).get("base_branch")
        try:
            repo.checkout(branch)
        except Exception as exc:  # noqa: BLE001
            return await self._escalate(
                task, f"could not check out parked branch {branch}: {exc}", repo=repo)

        ctx = task.context or {}
        ctx.pop("human_gated_ci", None)
        task.context = ctx
        task.blocker = None
        task.wake_check_at = None
        await self.store.update_task(task)

        self.emit("ci", "human-gated CI cleared on resume — opening PR", passed=True)
        # The change was already reviewed + tested before parking; advance to the
        # post-verification state so _finalize's transition to awaiting_approval
        # is legal (verification is not re-run — nothing changed).
        await self.store.set_status(task, TaskStatus.TESTING, validate=False)
        attempt_n = len(await self.store.list_attempts(task.id)) + 1
        attempt_id = await self.store.create_attempt(task.id, attempt_n)
        await self.store.update_attempt(attempt_id, branch_name=branch,
                                        commit_sha=repo.head_sha())
        commit = SimpleNamespace(files_changed=0, insertions=0, deletions=0,
                                 sha=repo.head_sha())
        result = SimpleNamespace(
            final_text="Resumed after the human-gated CI step was cleared.",
            num_turns=0)
        return await self._finalize(task, repo, branch, base, commit, attempt_id, result)

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
        except HumanGatedCI:
            # A human must start this pipeline — not an infra failure. Let it
            # propagate so _run_attempt parks the task with a wake condition.
            raise
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

    def _safe_changed_files(self, repo: GitRepo, base: str | None) -> list[str]:
        """Files this change touched vs its base — for CI relatedness triage.
        Best-effort: an error returns [] (→ attribution unknown → fix loop, never
        a false 'unrelated' that would skip a real failure)."""
        try:
            ref = base or "HEAD~1"
            return repo.changed_files(ref=ref)
        except Exception as exc:  # noqa: BLE001
            log.warning("changed_files for CI triage failed: %s", exc)
            return []

    async def _record_ci_failure(self, task: Task, ci_result: "CIResult") -> None:
        """Persist the remote CI failure so the NEXT attempt's prompt can target
        it (Phase 6.2). Stored on task.context; surfaced by _build_implement_prompt."""
        ctx = task.context or {}
        ctx["ci_failure"] = {
            "summary": ci_result.summary,
            "url": ci_result.pipeline_url,
            "failing_tests": [j.name for j in ci_result.jobs if j.status == "failed"],
            "detail": (ci_result.parsed_output or "")[:4000],
        }
        task.context = ctx
        await self.store.update_task(task)

    async def _record_review_feedback(self, task: Task, failed_items: list) -> None:
        """Persist the reviewer's failed checklist items so the NEXT attempt's
        prompt targets them (EVOLUTION_PLAN §2.2). Cited evidence (file:line) and
        the actionable comment are kept; the worker re-implements against the named
        gaps rather than blindly retrying. Bounded by max_attempts — never an
        unbounded loop; the tamper guard still gates every round."""
        ctx = task.context or {}
        ctx["review_feedback"] = [
            {
                "label": i.label,
                "evidence": i.evidence,
                "comment": i.comment,
                "file": i.file,
                "line": i.line,
            }
            for i in (failed_items or [])[:6]
        ]
        task.context = ctx
        await self.store.update_task(task)

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
        test_result = await asyncio.to_thread(runner.run_tests, repo.path, test_cmd)
        held_result = await asyncio.to_thread(runner.run_held_out_tests, repo.path)

        # Build profile + rules context for the staff-level reviewer.
        prof = getattr(self, "_active_profile", None)
        profile_ctx = ""
        if prof:
            parts = [f"Ecosystem: {prof.ecosystem}" if prof.ecosystem else ""]
            if prof.test_cmd:
                parts.append(f"Test command: {prof.test_cmd}")
            if prof.lint_cmd:
                parts.append(f"Lint command: {prof.lint_cmd}")
            profile_ctx = "\n".join(f"  {p}" for p in parts if p)
        confirmed_rules = self._format_active_memories() or ""

        self.emit("review_start", "running independent staff-level reviewer")
        try:
            decision = await self.reviewer.review(
                task,
                repo_path=repo.path,
                test_output=test_result.output if test_result.ran else "",
                held_out_output=held_result.output if held_result else "",
                profile_context=profile_ctx,
                confirmed_rules=confirmed_rules,
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

    # ─────────────────── code review pipeline ────────────────────────── #

    async def _run_code_review(self, task: Task, repo: GitRepo) -> TaskOutcome:
        """Review an external PR — read-only, no implementation, no branch.

        Extracts the PR URL from the task title/description, fetches the diff
        via the working agent (read-only mode), runs the staff-level reviewer,
        and stores the review checklist as the task result.
        """
        await self.store.set_status(task, TaskStatus.REVIEWING)
        self.emit("state", "reviewing (code review)", status="reviewing")

        pr_url = self._extract_pr_url(task)
        if not pr_url:
            return await self._fail(
                task, "code_review task requires a PR URL in the title or description"
            )

        # Fetch the diff using git. Try common forge patterns.
        diff = ""
        try:
            diff = await asyncio.to_thread(self._fetch_pr_diff, repo, pr_url)
        except Exception as exc:  # noqa: BLE001
            self.emit("review_error", f"could not fetch PR diff: {exc}")

        if not diff:
            # Fallback: ask the working agent to fetch and summarise the diff
            # using its tools (read-only mode is enforced by the guard).
            self.emit("review_start", "fetching PR diff via agent")
            prompt = (
                f"Fetch the diff for PR {pr_url} and output the complete diff. "
                f"Do NOT make any changes. Read-only."
            )
            result = await self.backend.run(
                prompt,
                cwd=repo.path,
                max_turns=10,
                effort="low",
                on_event=self._agent_sink,
            )
            diff = result.final_text or ""

        if not diff.strip():
            return await self._fail(
                task, f"could not fetch diff for {pr_url}"
            )

        # Persist the diff + PR URL so the UI diff tab and PR commenting work.
        task.context = {**(task.context or {}), "pr_diff": diff, "pr_url": pr_url}
        await self.store.update_task(task)

        # Create a review attempt to store the checklist.
        attempt_id = await self.store.create_attempt(task.id, 1)

        # Build profile + rules context for the staff-level reviewer.
        prof = await self._usable_profile(repo.path)
        self._active_profile = prof
        self._active_memories = await self.store.list_memories(
            confirmed=True, project=task.repo_path
        )

        profile_ctx = ""
        if prof:
            parts = [f"Ecosystem: {prof.ecosystem}" if prof.ecosystem else ""]
            if prof.test_cmd:
                parts.append(f"Test command: {prof.test_cmd}")
            if prof.lint_cmd:
                parts.append(f"Lint command: {prof.lint_cmd}")
            profile_ctx = "\n".join(f"  {p}" for p in parts if p)
        confirmed_rules = self._format_active_memories() or ""

        self.emit("review_start", f"running staff-level code review on {pr_url}")
        if self.reviewer is None:
            return await self._fail(task, "no reviewer configured for code_review tasks")

        try:
            decision = await self.reviewer.review(
                task,
                repo_path=repo.path,
                test_output="",
                held_out_output="",
                diff_override=diff,
                profile_context=profile_ctx,
                confirmed_rules=confirmed_rules,
            )
        except Exception as exc:  # noqa: BLE001
            self.emit("review_error", str(exc))
            return await self._fail(task, f"reviewer crashed: {exc}")

        # Store the review result.
        import json as _json
        checklist_data = {
            "passed": decision.passed,
            "items": [
                {
                    "label": it.label, "passed": it.passed,
                    "evidence": it.evidence,
                    "file": it.file, "line": it.line,
                    "comment": it.comment,
                }
                for it in (decision.checklist or [])
            ],
        }
        await self.store.update_attempt(
            attempt_id,
            review_passed=1 if decision.passed else 0,
            review_checklist=_json.dumps(checklist_data),
            status="succeeded",
        )

        verdict = "PASS" if decision.passed else "FAIL"
        n_failed = len(decision.failed_items)
        detail = f"code review {verdict}: {n_failed} issue(s) found" if not decision.passed \
            else f"code review {verdict}: all checks passed"
        self.emit("review", detail, passed=decision.passed, failed_count=n_failed)

        # Mark done — code reviews don't need approval.
        await self.store.set_status(task, TaskStatus.DONE, validate=False)
        self.emit("state", "done", status="done")
        return TaskOutcome(task, status=TaskStatus.DONE, detail=detail)

    def _extract_pr_url(self, task: Task) -> str | None:
        """Extract a PR/MR URL from the task title or description."""
        import re
        text = f"{task.title or ''} {task.description or ''}"
        # Match GitHub/GitLab/GHE PR/MR URLs
        m = re.search(
            r'https?://[^\s]+/(?:pull|merge_requests)/\d+',
            text,
        )
        return m.group(0) if m else None

    def _fetch_pr_diff(self, repo: GitRepo, pr_url: str) -> str:
        """Fetch the PR diff via git fetch + diff. Supports GitHub and GitLab."""
        import re
        import subprocess

        # GitHub pattern: .../pull/123
        gh = re.search(r'/pull/(\d+)', pr_url)
        if gh:
            pr_num = gh.group(1)
            repo._run("fetch", "origin", f"pull/{pr_num}/head:_nh_review_pr")
            base = repo._run("merge-base", "origin/HEAD", "_nh_review_pr").strip()
            diff = repo._run("diff", base, "_nh_review_pr", "--no-color")
            try:
                repo._run("branch", "-D", "_nh_review_pr")
            except Exception:  # noqa: BLE001
                pass
            return diff

        # GitLab pattern: .../merge_requests/123
        gl = re.search(r'/merge_requests/(\d+)', pr_url)
        if gl:
            mr_num = gl.group(1)
            repo._run("fetch", "origin",
                       f"merge-requests/{mr_num}/head:_nh_review_mr")
            base = repo._run("merge-base", "origin/HEAD", "_nh_review_mr").strip()
            diff = repo._run("diff", base, "_nh_review_mr", "--no-color")
            try:
                repo._run("branch", "-D", "_nh_review_mr")
            except Exception:  # noqa: BLE001
                pass
            return diff

        raise ValueError(f"cannot parse PR number from URL: {pr_url}")

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

    async def _resolve_lint_cmd(self, repo: GitRepo) -> str | None:
        """Resolve the lint command: explicit config wins, then profile, then None.

        When None, the lint gate is skipped (no lint = no gate). This is
        intentional: we only lint when the repo has a confirmed lint command.
        """
        explicit = self.config.get("lint", {}).get("command")
        if explicit:
            return explicit
        prof = await self._usable_profile(repo.path)
        if prof and getattr(prof, "lint_cmd", None):
            return prof.lint_cmd
        return None

    async def _build_lint_hook(self, repo: GitRepo):
        """Build the per-edit lint feedback hook (B1), or None if disabled.

        Gated by ``hooks.per_edit_lint`` (default off) so it can be validated
        before becoming the default. No-op when the repo has no lint command.
        """
        if not self.config.get("hooks", {}).get("per_edit_lint", False):
            return None
        lint_cmd = await self._resolve_lint_cmd(repo)
        if not lint_cmd:
            return None
        from ..agent.lint_hook import LintFeedbackHook
        return LintFeedbackHook(
            repo_path=repo.path, lint_cmd=lint_cmd, on_event=self.emit,
        )

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

    def _concurrency_enabled(self) -> bool:
        return bool(self.config.get("concurrency", {}).get("enabled", False))

    def _worktree_path(self, task: Task) -> Path:
        """Stable per-task worktree location outside the repo tree."""
        from ..config import NO_HUMAN_HOME
        root = self.config.get("concurrency", {}).get("worktree_root")
        base = Path(root).expanduser() if root else (NO_HUMAN_HOME / "worktrees")
        return base / task.id

    def _acquire_worktree(self, main_repo: GitRepo, wt_path: Path, base: str) -> GitRepo:
        """Detached worktree at ``base`` for one task. A stale worktree at the
        path (e.g. from a crashed prior run) is pruned first so re-acquire on
        resume is idempotent. The attempt loop creates the feature branch inside."""
        try:
            main_repo.remove_worktree(wt_path)
        except Exception:  # noqa: BLE001 — best-effort prune of a stale path
            pass
        import shutil
        if Path(wt_path).exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        return main_repo.add_worktree(wt_path, base=base, detach=True)

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

    # WS-A: a per-kind directive steers the same implement→review→test loop at
    # the task type the classifier tagged. The pipeline shape (gate, tamper guard,
    # never-merge) is unchanged; only the agent's framing differs.
    _KIND_DIRECTIVES: dict[str, str] = {
        "bugfix": (
            "This is a BUGFIX. Reproduce the defect with a failing test first, "
            "then fix the root cause (not the symptom), and confirm the test passes."
        ),
        "ci_fix": (
            "This task is to make a failing remote CI build GREEN. Fix the actual "
            "cause of the failing tests/build — never weaken, skip, or delete a "
            "test to go green. If the failing tests are not in code this change "
            "owns, say so rather than editing code you didn't break."
        ),
        "traceability": (
            "This is a TEST-AUTOMATION TRACEABILITY task. Author the missing "
            "automated test for the linked work item. Do NOT fabricate an "
            "execution result or test-automation count — the count is "
            "execution-backed and only populates after the test really runs in CI."
        ),
        "test_gap": (
            "This task is to ADD missing test coverage for existing behaviour. "
            "Do not change production behaviour except minimally to make the code "
            "testable; the new tests must genuinely exercise the code."
        ),
        "investigation": (
            "This is an INVESTIGATION / ROOT-CAUSE ANALYSIS task. You have wider "
            "bounds (more attempts and turns) because debugging is exploratory. "
            "Systematically narrow down the problem: read logs, run diagnostic "
            "commands, form hypotheses and verify them with evidence. Do NOT guess "
            "or speculate — prove each step. Document your findings as you go. "
            "If you identify the root cause, propose a fix with evidence that it "
            "addresses the actual problem, not just the symptom."
        ),
    }

    def _kind_directive(self, task: Task) -> str:
        return self._KIND_DIRECTIVES.get(task.kind, "")

    def _build_supervisor(self, task: Task, work_dir: str | None = None) -> SupervisorHook | None:
        """Construct a SupervisorHook for the current task, or None if disabled.

        The supervisor uses a lightweight LLM call (low effort, short prompt) to
        periodically evaluate the working agent's progress and inject corrections.
        """
        sv_cfg = self.config.get("supervisor", {})
        if not sv_cfg.get("enabled", True):
            return None
        check_every = int(sv_cfg.get("check_every", 5))

        # Build rules text for the supervisor (same as the implementer sees).
        rules = self._format_active_memories() or ""

        # Build profile context for the supervisor.
        prof = getattr(self, "_active_profile", None)
        profile_ctx = ""
        if prof:
            parts = [f"Ecosystem: {prof.ecosystem}" if prof.ecosystem else ""]
            if prof.test_cmd:
                parts.append(f"Test command: {prof.test_cmd}")
            if prof.lint_cmd:
                parts.append(f"Lint command: {prof.lint_cmd}")
            profile_ctx = "Project profile:\n" + "\n".join(
                f"  {p}" for p in parts if p
            )

        # The supervisor LLM call: a simple prompt-in, text-out function.
        # Uses the same backend model but with low effort to keep costs down.
        async def sv_llm_call(prompt: str) -> str:
            sv_backend = ClaudeBackend(model=self.backend.model, readonly=True)
            result = await sv_backend.run(
                prompt, cwd=Path(work_dir or task.repo_path or "."),
                max_turns=1, effort="low",
            )
            return result.final_text or ""

        def on_decision(decision):
            self.emit(
                "supervisor_decision", decision.action,
                message=decision.message[:200] if decision.message else "",
            )

        # Skills the supervisor must check the agent uses (EVOLUTION_PLAN §1.2 #2,
        # §1.3 row 1): confirmed skill-type memories. This is what lets the
        # supervisor convert "I can't access X" into "use skill Y" when Y exists.
        skills = [
            m.get("title", "")
            for m in (getattr(self, "_active_memories", None) or [])
            if m.get("type") == "skill" and m.get("title")
        ]

        return SupervisorHook(
            task_title=task.title,
            acceptance_criteria=task.acceptance_criteria,
            rules=rules,
            profile_context=profile_ctx,
            skills=skills,
            llm_call=sv_llm_call,
            check_every=check_every,
            on_decision=on_decision,
        )

    async def _maybe_preflight(
        self, task: Task, repo: GitRepo, supervisor: SupervisorHook | None, prompt: str
    ) -> str:
        """Run the supervisor's pre-flight plan check if enabled. Returns the
        (possibly augmented) implement prompt. Best-effort: any failure returns
        the original prompt unchanged — pre-flight never blocks a task."""
        sv_cfg = self.config.get("supervisor", {})
        if supervisor is None or not sv_cfg.get("preflight", False):
            return prompt
        try:
            plan_prompt = (
                f"Before writing any code, produce a SHORT numbered plan for this "
                f"task (no edits, read files if needed):\n\nTask: {task.title}\n"
                f"Acceptance criteria:\n"
                + "\n".join(f"  - {c}" for c in task.acceptance_criteria)
            )
            plan_result = await self.backend.run(
                plan_prompt, cwd=repo.path, max_turns=6, effort="low",
                on_event=self._agent_sink,
            )
            plan = (plan_result.final_text or "").strip()
            if not plan:
                return prompt
            decision = await supervisor.preflight(plan)
            self.emit("supervisor_preflight", decision.action,
                      message=decision.message[:200] if decision.message else "")
            if decision.action == "correct" and decision.message:
                return (
                    prompt
                    + "\n\nPRE-FLIGHT PLAN REVIEW (address before you start):\n"
                    + decision.message
                    + "\n"
                )
        except Exception as exc:  # noqa: BLE001 — pre-flight is best-effort
            log.warning("pre-flight plan check failed: %s", exc)
        return prompt

    def _build_implement_prompt(self, task: Task, work_dir: str | None = None) -> str:
        criteria = "\n".join(f"  - {c}" for c in task.acceptance_criteria) or "  (none stated)"
        kind_directive = self._kind_directive(task)
        rules = (
            "Rules:\n"
            "  - Verify with evidence: run commands, read their output; don't assert.\n"
            "    'I think it works' is NOT evidence. Run the command and show the output.\n"
            "  - Minimal, focused edits. No comments unless the WHY is non-obvious.\n"
            "  - Add or update tests for your change and run them.\n"
            "  - NEVER weaken, skip, or delete a test to make things pass.\n"
            "  - Do NOT run any git command — branching, committing, pushing and\n"
            "    opening the PR are handled for you. Just edit files and run tests.\n"
            "  - All imports MUST be at the top of the file. Never add imports in the\n"
            "    middle of a file — if you need to import, make a separate edit at the top.\n"
            "  - Before writing code for a CI or remote environment, verify what tools and\n"
            "    runtimes are available there. Never assume python3, jq, or specific versions.\n"
            "  - READ the existing code BEFORE making changes. Understand what is already\n"
            "    there; do not guess or speculate about the codebase.\n"
            "  - If you are stuck after 2 attempts at the same approach, STOP and rethink.\n"
            "    Try a fundamentally different approach, not a minor tweak.\n"
            "  - Fix root causes, not symptoms. If a test fails, understand WHY before\n"
            "    changing code. Chasing the error message leads to cascading wrong fixes.\n"
        )
        # Append confirmed rules + skills from the learning queue (Phase G).
        extra = self._format_active_memories()
        if extra:
            rules += extra
        digest = self._context_digest(task)
        resume = self._resume_digest(task)
        # Multi-repo context (Phase D / WS-E).
        from .multi_repo import cross_repo_context
        multi_ctx = cross_repo_context(task, task.repo_path or "")
        multi_block = (multi_ctx + "\n\n") if multi_ctx else ""

        # Profile context: tell the agent about the repo's ecosystem so it
        # doesn't waste turns discovering the tech stack.
        prof = getattr(self, "_active_profile", None)
        profile_block = ""
        if prof:
            parts = [f"Ecosystem: {prof.ecosystem}" if prof.ecosystem else ""]
            if prof.test_cmd:
                parts.append(f"Test command: {prof.test_cmd}")
            if prof.install_cmd:
                parts.append(f"Install command: {prof.install_cmd}")
            if prof.lint_cmd:
                parts.append(f"Lint command: {prof.lint_cmd}")
            profile_block = "Project profile (confirmed):\n" + "\n".join(f"  {p}" for p in parts if p) + "\n\n"

        # CRITICAL: the agent must operate in its ACTUAL working directory, which
        # in concurrency mode is a per-task git worktree — NOT task.repo_path (the
        # primary checkout). If we hand it task.repo_path, absolute-path edits land
        # in the wrong tree and the worktree shows "no file changes" (the attempt
        # then fails spuriously). work_dir is the cwd the SDK session runs in.
        repo_dir = work_dir or task.repo_path
        return (
            f"You are implementing a software task in the repo at {repo_dir}.\n"
            f"This is your working directory — make ALL edits here (use paths under "
            f"{repo_dir}); do not touch any other checkout of this repo.\n\n"
            f"{profile_block}"
            f"{multi_block}"
            f"Task: {task.title}\n"
            f"{('Description: ' + task.description) if task.description else ''}\n\n"
            f"{(kind_directive + chr(10) + chr(10)) if kind_directive else ''}"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"{(digest + chr(10) + chr(10)) if digest else ''}"
            f"{(resume + chr(10) + chr(10)) if resume else ''}"
            f"{rules}\n"
            + selfcheck.build_prompt(task.title, task.acceptance_criteria)
            + blocker_prompt_suffix()
        )

    def _format_active_memories(self) -> str:
        """Format confirmed rules + skills for prompt injection.

        Returns an empty string if there are none, or a block like:
          Confirmed rules from past experience:
            - [rule] Title: content
            - [skill] Title: content
        Bounded to 20 entries (≈4k tokens) to avoid blowing the context window.
        """
        memories = getattr(self, "_active_memories", None)
        if not memories:
            return ""
        lines: list[str] = []
        for m in memories[:20]:
            mem_type = m.get("type", "rule")
            title = m.get("title", "")
            content = m.get("content", "")
            # Compact format: one line per memory.
            short = content.replace("\n", " ").strip()[:200]
            lines.append(f"  - [{mem_type}] {title}: {short}")
        if not lines:
            return ""
        return (
            "\nConfirmed rules/skills from past experience:\n"
            + "\n".join(lines)
            + "\n"
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
        review_fb = ctx.get("review_feedback") or []
        if review_fb:
            lines = []
            for f in review_fb:
                loc = f"{f.get('file', '')}:{f.get('line', 0)}" if f.get("file") else ""
                detail = f.get("comment") or f.get("evidence") or ""
                lines.append(f"  - {f.get('label', '')}{f' ({loc})' if loc else ''}: {detail}")
            parts.append(
                "The independent staff reviewer FAILED your previous attempt on "
                "these specific, cited findings. Fix each one — do NOT weaken, "
                "skip, or delete any test to satisfy the reviewer:\n"
                + "\n".join(lines)
            )
        ci_fail = ctx.get("ci_failure")
        if ci_fail:
            tests = ci_fail.get("failing_tests") or []
            parts.append(
                "The remote CI build for your previous attempt FAILED. Fix the "
                "actual failure — do NOT weaken, skip, or delete tests to go green.\n"
                f"  pipeline: {ci_fail.get('url', '')}\n"
                + (f"  failing tests: {', '.join(tests[:10])}\n" if tests else "")
                + "  details:\n"
                + "\n".join(f"    {ln}" for ln in
                            (ci_fail.get("detail", "")).splitlines()[:30])
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
