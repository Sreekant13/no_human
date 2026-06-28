"""Wake-condition parsing + the parked-task watcher (PLAN.md 22.7).

A lightweight poller re-evaluates every ``blocked`` / ``paused_quota`` task: it
checks the machine-checkable wake condition (PR merged? quota back? CI green?
time elapsed?) and on satisfaction flips the task back to its prior working
state. Each parked task has a **max park duration** → escalate on timeout so
nothing is silently abandoned.

The condition grammar is deliberately tiny and machine-checkable:
  - ``after:<duration>``        e.g. ``after:2h`` — relative to when parked
  - ``quota_refreshed``         time-based; satisfied once ``wake_check_at`` passes
  - ``ci_green_on:<branch>``    delegated to an injected CI checker
  - ``pr_merged:<ref>`` / ``PR <ref> merged`` — delegated to an injected PR checker
  - ``null`` / empty            never self-wakes (waits for a human or timeout)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..core.db import Store
from ..core.task import Task, TaskStatus
from .taxonomy import Blocker

log = logging.getLogger("no_human.wake")

# Async hooks the host wires in (live PR/CI lookups). Default: not satisfied.
PrMergedChecker = Callable[[str], Awaitable[bool]]
CiGreenChecker = Callable[[str], Awaitable[bool]]
# Returns (is_terminal, is_success) for a pipeline ID.
CiTerminalChecker = Callable[[str], Awaitable[tuple[bool, bool]]]
# Returns list of new PrComment objects for a PR ref.
PrCommentChecker = Callable[[str], Awaitable[list[Any]]]

_DURATION = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta | None:
    """Parse ``2h`` / ``30m`` / ``48h`` / ``1d`` into a timedelta, or None."""
    if not text:
        return None
    total = 0
    matched = False
    for num, unit in _DURATION.findall(text):
        total += int(num) * _UNIT_SECONDS[unit.lower()]
        matched = True
    return timedelta(seconds=total) if matched else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class WakeWatcher:
    """Polls parked tasks; resumes them when their wake condition fires, or
    escalates on max-park-duration timeout."""

    def __init__(
        self,
        store: Store,
        config: dict,
        *,
        pr_merged: PrMergedChecker | None = None,
        ci_green: CiGreenChecker | None = None,
        ci_terminal: CiTerminalChecker | None = None,
        pr_comment: PrCommentChecker | None = None,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.store = store
        blockers_cfg = (config or {}).get("blockers", {})
        self.max_park = parse_duration(
            str(blockers_cfg.get("max_park_duration", "48h"))
        ) or timedelta(hours=48)
        # Cap on autonomous PR-comment → revise cycles. A reviewer (or bot) can
        # post comments indefinitely; without this, each batch resets the full
        # attempt budget, so the agent could revise forever. After this many
        # rounds we escalate to the human instead of resuming (constraint §5,
        # bounded autonomy). Defaults to the same value as bounds.max_correction_rounds.
        self.max_revision_rounds = int(
            (config or {}).get("bounds", {}).get("max_correction_rounds", 2)
        )
        self._pr_merged = pr_merged
        self._ci_green = ci_green
        self._ci_terminal = ci_terminal
        self._pr_comment = pr_comment
        self._on_event = on_event or (lambda kind, text: None)

    # ----------------------------- condition ------------------------------- #

    async def condition_satisfied(
        self, condition: str | None, *, raised_at: datetime, now: datetime,
        wake_check_at: datetime | None,
    ) -> bool:
        """Evaluate one wake condition. Unknown / null conditions never self-fire
        (the timeout path is what eventually frees them)."""
        if not condition:
            return False
        cond = condition.strip()
        low = cond.lower()

        if low.startswith("after:"):
            dur = parse_duration(cond.split(":", 1)[1])
            return dur is not None and now - raised_at >= dur

        if low in ("quota_refreshed", "quota", "quota_reset"):
            # Quota parks set wake_check_at to the expected reset time.
            return wake_check_at is not None and now >= wake_check_at

        if low.startswith("ci_green_on:"):
            branch = cond.split(":", 1)[1].strip()
            if self._ci_green is None:
                return False
            try:
                return await self._ci_green(branch)
            except Exception as exc:  # noqa: BLE001 — checker must never crash watcher
                log.warning("ci_green checker failed: %s", exc)
                return False

        if low.startswith("pr_comment_on:"):
            pr_ref = cond.split(":", 1)[1].strip()
            if self._pr_comment is None:
                return False
            try:
                comments = await self._pr_comment(pr_ref)
                return len(comments) > 0
            except Exception as exc:  # noqa: BLE001
                log.warning("pr_comment checker failed: %s", exc)
                return False

        if low.startswith("ci_terminal_on:"):
            pipeline_ref = cond.split(":", 1)[1].strip()
            if self._ci_terminal is None:
                return False
            try:
                is_terminal, _is_success = await self._ci_terminal(pipeline_ref)
                return is_terminal
            except Exception as exc:  # noqa: BLE001
                log.warning("ci_terminal checker failed: %s", exc)
                return False

        ref = None
        if low.startswith("pr_merged:"):
            ref = cond.split(":", 1)[1].strip()
        else:
            m = re.match(r"pr\s+(\S+)\s+merged", low)
            if m:
                ref = m.group(1)
        if ref is not None:
            if self._pr_merged is None:
                return False
            try:
                return await self._pr_merged(ref)
            except Exception as exc:  # noqa: BLE001
                log.warning("pr_merged checker failed: %s", exc)
                return False

        # Time has passed the explicit re-check stamp, with no richer condition.
        return wake_check_at is not None and now >= wake_check_at

    # ------------------------------- tick ---------------------------------- #

    async def tick(self, *, now: datetime | None = None) -> list[tuple[str, str]]:
        """Re-evaluate all parked tasks once. Returns (task_id, action) tuples
        where action is 'resumed' or 'escalated_timeout'."""
        now = now or datetime.now(timezone.utc)
        actions: list[tuple[str, str]] = []
        for status in (TaskStatus.BLOCKED, TaskStatus.PAUSED_QUOTA,
                       TaskStatus.AWAITING_INPUT):
            for task in await self.store.list_tasks(status):
                action = await self._evaluate(task, now=now)
                if action:
                    actions.append((task.id, action))
        return actions

    async def _evaluate(self, task: Task, *, now: datetime) -> str | None:
        blocker = Blocker.from_dict(task.blocker) if task.blocker else None
        raised_at = _parse_iso(blocker.raised_at if blocker else None) \
            or _parse_iso(task.updated_at) or now
        wake_check_at = _parse_iso(task.wake_check_at)

        # AWAITING_INPUT only ever resumes on a human reply — but it still
        # times out so a forgotten question doesn't sit forever.
        condition = blocker.wake_condition if blocker else None
        if task.status != TaskStatus.AWAITING_INPUT:
            satisfied = await self.condition_satisfied(
                condition, raised_at=raised_at, now=now, wake_check_at=wake_check_at,
            )
            if satisfied:
                # If the condition is pr_comment_on, inject the comments as feedback.
                if condition and condition.strip().lower().startswith("pr_comment_on:"):
                    rounds = await self._inject_pr_feedback(task, condition)
                    # Bound the comment→revise loop: after max_revision_rounds
                    # autonomous rounds, escalate to the human rather than resume.
                    if rounds is not None and rounds > self.max_revision_rounds:
                        await self._escalate_revisions(task, rounds)
                        return "escalated_revisions"
                return await self._resume(task)

        # Timeout → escalate (never silently abandon).
        if now - raised_at >= self.max_park:
            await self._escalate_timeout(task, blocker)
            return "escalated_timeout"
        return None

    async def _resume(self, task: Task) -> str:
        """Flip a parked task back to its prior working state (IMPLEMENTING).

        Resume re-enters the loop in a fresh session seeded with the report
        (22.5) — the orchestrator picks it up from the [WIP-BLOCKED] checkpoint.
        """
        ctx = task.context or {}
        ctx["resumed_at"] = now_iso()
        ctx["resume_reason"] = "wake_condition_satisfied"
        task.context = ctx
        task.wake_check_at = None
        await self.store.update_task(task)
        await self.store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
        self._on_event("resumed", f"{task.id[:8]} wake condition satisfied")
        return "resumed"

    async def _inject_pr_feedback(self, task: Task, condition: str) -> int | None:
        """Fetch PR comments and thread them into send_back_feedback.

        Returns the task's running revision-round count after this batch (so the
        caller can enforce the cap), or None if there were no new comments.
        """
        if self._pr_comment is None:
            return None
        pr_ref = condition.split(":", 1)[1].strip()
        try:
            comments = await self._pr_comment(pr_ref)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to fetch PR comments for injection: %s", exc)
            return None
        if not comments:
            return None
        ctx = task.context or {}
        feedback = ctx.get("send_back_feedback") or []
        for c in comments:
            # Support both PrComment objects and plain dicts.
            if hasattr(c, "body"):
                msg = c.body
                author = c.author if hasattr(c, "author") else "reviewer"
                path = getattr(c, "path", None)
                line = getattr(c, "line", None)
                diff_hunk = getattr(c, "diff_hunk", None)
                created = getattr(c, "created_at", "") or now_iso()
            else:
                msg = str(c)
                author = "reviewer"
                path = line = diff_hunk = None
                created = now_iso()
            if path:
                loc = f"{path}"
                if line:
                    loc += f":{line}"
                msg = f"[{loc}] {msg}"
            if diff_hunk:
                msg += f"\n\nContext:\n```\n{str(diff_hunk)[:500]}\n```"
            feedback.append({
                "at": created,
                "message": msg,
                "author": author,
                "source": "pr_comment",
            })
        ctx["send_back_feedback"] = feedback
        ctx["pr_comment_ref"] = pr_ref
        rounds = int(ctx.get("revision_rounds", 0)) + 1
        ctx["revision_rounds"] = rounds
        task.context = ctx
        await self.store.update_task(task)
        self._on_event("pr_feedback", f"{task.id[:8]} got {len(comments)} PR comment(s)")
        return rounds

    async def _escalate_revisions(self, task: Task, rounds: int) -> None:
        """Stop the comment→revise loop after the cap and hand back to a human."""
        data = task.blocker or {}
        data["category"] = "AMBIGUITY"
        data["root_cause_hypothesis"] = (
            f"PR feedback revised {rounds} time(s), exceeding "
            f"max_revision_rounds={self.max_revision_rounds}; escalating so a "
            "human can decide rather than revising indefinitely."
        )
        task.blocker = data
        await self.store.update_task(task)
        if task.status != TaskStatus.ESCALATED:
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        self._on_event(
            "escalated_revisions",
            f"{task.id[:8]} exceeded {self.max_revision_rounds} PR-revision rounds",
        )

    async def _escalate_timeout(self, task: Task, blocker: Blocker | None) -> None:
        data = task.blocker or {}
        data["timed_out"] = True
        data["category"] = "NOVEL_UNKNOWN" if blocker is None else data.get("category")
        data["root_cause_hypothesis"] = (
            f"parked past max duration ({self.max_park}); "
            + data.get("root_cause_hypothesis", "")
        ).strip()
        task.blocker = data
        await self.store.update_task(task)
        if task.status != TaskStatus.ESCALATED:
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        self._on_event("escalated_timeout", f"{task.id[:8]} parked past max duration")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
