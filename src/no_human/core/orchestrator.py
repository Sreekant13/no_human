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
    ):
        self.store = store
        self.config = config
        self.backend = backend
        self.notifier = notifier
        self.bounds = Bounds.from_config(config.get("bounds"))
        self._sink = event_sink or (lambda e: None)
        self.context_gatherer = context_gatherer
        self.reviewer = reviewer

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

        # Capture base branch once — never re-derive from current branch, which
        # may point at a previous attempt's feature branch after a failed attempt.
        base_branch = repo.current_branch()

        outcome = TaskOutcome(task, status=task.status, detail="")
        for attempt_n in range(1, self.bounds.max_attempts + 1):
            self.emit("attempt_start", f"attempt {attempt_n}/{self.bounds.max_attempts}")
            try:
                outcome = await self._run_attempt(task, repo, attempt_n, base_branch)
            except QuotaExhausted as exc:
                return await self._park_quota(task, exc)
            if outcome.status == TaskStatus.AWAITING_APPROVAL:
                return outcome
            if outcome.status in (TaskStatus.ESCALATED, TaskStatus.FAILED):
                # Tamper / structural blocker — do not retry blindly.
                if outcome.status == TaskStatus.ESCALATED:
                    return outcome
            self.emit("attempt_failed", outcome.detail)

        # Bounds exhausted -> escalate with a diagnosis (never fake done).
        return await self._escalate(
            task, f"max_attempts ({self.bounds.max_attempts}) reached without a "
            f"passing, untampered change. Last: {outcome.detail}"
        )

    async def _run_attempt(
        self, task: Task, repo: GitRepo, attempt_n: int, base: str | None = None
    ) -> TaskOutcome:
        attempt_id = await self.store.create_attempt(task.id, attempt_n)
        stuck = StuckDetector()

        # --- branch (deterministic git; agent never touches git) ---
        branch = f"{self.config['git']['branch_prefix']}{task.id[:8]}"
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
        if not repo.has_changes() and base == repo.current_branch():
            pass  # changes may already be staged/committed by tooling; check below

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
            return await self._escalate(task, over)

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
        test_cmd = self.config.get("tests", {}).get("command")
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

        # --- finalize: push + open PR (NEVER merge) + notify ---
        return await self._finalize(task, repo, branch, base, commit, attempt_id, result)

    async def _finalize(self, task, repo, branch, base, commit, attempt_id, result) -> TaskOutcome:
        title = self._commit_message(task)
        body = self._pr_body(task, commit, result)
        try:
            pr = open_pr(repo, branch, title, body, base=base)
        except ProtectedBranch as exc:
            return await self._escalate(task, str(exc))
        except Exception as exc:  # noqa: BLE001
            return await self._escalate(task, f"opening PR failed: {exc}")
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
        return TaskOutcome(task, pr_url=pr.url, status=TaskStatus.AWAITING_APPROVAL,
                           detail="PR opened; awaiting human approval")

    # --------------------------- off-ramps --------------------------------- #

    async def _fail(self, task: Task, detail: str) -> TaskOutcome:
        await self.store.set_status(task, TaskStatus.FAILED)
        self.emit("failed", detail, status="failed")
        self.notifier.notify("task_failed", f"{task.title}: {detail}")
        return TaskOutcome(task, status=TaskStatus.FAILED, detail=detail)

    async def _escalate(self, task: Task, detail: str) -> TaskOutcome:
        # Phase 0 stub of the Part 22 structured blocker report.
        task.blocker = {
            "category": "NOVEL_UNKNOWN",
            "transient": False,
            "question": "Review the blocker and advise.",
            "detail": detail,
            "raised_at": _now(),
        }
        await self.store.update_task(task)
        await self.store.set_status(task, TaskStatus.ESCALATED)
        self.emit("escalated", detail, status="escalated")
        self.notifier.notify("stuck", f"{task.title} escalated: {detail}")
        return TaskOutcome(task, status=TaskStatus.ESCALATED, detail=detail)

    async def _park_quota(self, task: Task, exc: QuotaExhausted) -> TaskOutcome:
        task.wake_check_at = exc.resets_at
        await self.store.update_task(task)
        await self.store.set_status(task, TaskStatus.PAUSED_QUOTA)
        self.emit("paused_quota", str(exc), status="paused_quota")
        self.notifier.notify("paused_quota", f"{task.title} paused: subscription quota")
        return TaskOutcome(task, status=TaskStatus.PAUSED_QUOTA, detail=str(exc))

    # --------------------------- helpers ----------------------------------- #

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
        test_cmd = self.config.get("tests", {}).get("command")
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
        return (
            f"You are implementing a software task on the repo at {task.repo_path}.\n\n"
            f"Task: {task.title}\n"
            f"{('Description: ' + task.description) if task.description else ''}\n\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"{(digest + chr(10) + chr(10)) if digest else ''}"
            f"{rules}\n"
            + selfcheck.build_prompt(task.title, task.acceptance_criteria)
        )

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
