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

import hashlib
import logging
import time
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..core.db import Store
from ..core.task import Task, TaskStatus
from .taxonomy import Blocker, resume_checkpoint

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
        pr_state: Callable[[str], Awaitable[str]] | None = None,
        pr_checks: Callable[[str], Awaitable[list[dict]]] | None = None,
        ci_log: Callable[[str], Awaitable[str]] | None = None,
        on_event: Callable[[str, str], None] | None = None,
        ci_gate_gate: Any = None,
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
        # Cap on autonomous CI-failure → fix cycles on an open PR (Jules /
        # Copilot pattern: bounded rounds, then hand the specific failure to a
        # human). Counted per distinct failure signature, so a re-run of the
        # same red check doesn't burn a round.
        self.max_ci_fix_rounds = int(blockers_cfg.get("max_ci_fix_rounds", 3))
        # Stuck-active watchdog threshold (minutes). Default 40 > the 30-min
        # run_tests timeout, so a long test never trips it; a genuinely hung
        # session does. 0 disables.
        self.stuck_active_minutes = float(
            blockers_cfg.get("stuck_active_minutes", 40))
        # Bounded CI_GATE-integration-failure → fix cycles (M6), same pattern.
        self.max_ci_gate_fix_rounds = int(
            blockers_cfg.get("max_ci_gate_fix_rounds", 3)
        )
        # Comment authors whose PR comments never trigger a revision. Live
        # incident: system-codeadmin posts a unit-test-results table on every
        # build, which the comment rung injected as human feedback and resumed
        # the task — one wasted attempt per PR, forever. "[bot]" logins are
        # always ignored on top of this list. In-code default rather than
        # config.py DEFAULTS because a user yaml `blockers:` section replaces
        # that map wholesale (the deep-merge shadowing trap).
        self.ignore_comment_authors = {
            str(a).lower()
            for a in blockers_cfg.get("ignore_comment_authors", ["system-codeadmin"])
        }
        self._pr_merged = pr_merged
        self._ci_green = ci_green
        self._ci_terminal = ci_terminal
        self._pr_comment = pr_comment
        self._pr_state = pr_state
        self._pr_checks = pr_checks
        self._ci_log = ci_log
        self._on_event = on_event or (lambda kind, text: None)
        # The post-PR CI_GATE integration gate (M6). Injectable for tests;
        # by default built here (the single wiring point for all three hosts)
        # and only when ci_gate.enabled — otherwise the rung is a no-op.
        if ci_gate_gate is None and (config or {}).get("ci_gate", {}).get("enabled"):
            ci_gate_gate = self._default_ci_gate_gate(config)
        self._ci_gate_gate = ci_gate_gate

    @staticmethod
    def _default_ci_gate_gate(config: dict):
        """Build the real gate (gh/glab/kubectl-backed). Lazy import so hosts
        that never enable CI_GATE pay nothing; returns None if wiring fails —
        the watcher must keep running without the rung, not crash."""
        try:
            from ..ci_gate.gate import CiGate
            from ..vcs.pr_watcher import (
                default_pr_checks, default_pr_files, default_pr_head,
                parse_pr_url, upsert_agent_comment,
            )

            async def _post_comment(url: str, body: str) -> bool:
                parsed = parse_pr_url(url)
                if not parsed or parsed[0] != "github":
                    return False
                _, host, slug, num = parsed
                # UPDATE the one CI_GATE comment instead of posting a new one every
                # attempt (PR #531 piled up 17 near-identical comments).
                return await upsert_agent_comment(f"{host}/{slug}#{num}", body, key="ci_gate")

            return CiGate(
                config,
                pr_head=default_pr_head,
                pr_files=default_pr_files,
                pr_checks=default_pr_checks,
                post_comment=_post_comment,
            )
        except Exception:  # noqa: BLE001
            log.warning("CI_GATE gate wiring failed — rung disabled", exc_info=True)
            return None

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
                       TaskStatus.AWAITING_INPUT, TaskStatus.AWAITING_APPROVAL):
            for task in await self.store.list_tasks(status):
                action = await self._evaluate(task, now=now)
                if action:
                    actions.append((task.id, action))
                else:
                    await self._heartbeat(task, now=now)
        # Stuck-active watchdog: a task frozen mid-run (e.g. a hung Agent-SDK
        # session that even the reviewer's own timeout can't cancel — observed
        # 2026-07-11) would otherwise sit in an active state forever, holding a
        # worker slot and never failing honestly. Escalate one with NO event
        # for longer than the threshold (set above the 30-min test timeout, so
        # a legitimately long test run never trips it).
        for status in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
                       TaskStatus.TESTING, TaskStatus.PLANNING,
                       TaskStatus.CONTEXT):
            for task in await self.store.list_tasks(status):
                if await self._escalate_if_stalled(task, now=now):
                    actions.append((task.id, "escalated_stalled"))
        return actions

    async def _escalate_if_stalled(self, task: Task, *, now: datetime) -> bool:
        """Escalate a task that has emitted no event for longer than the
        stuck-active threshold. Returns True iff it escalated."""
        if self.stuck_active_minutes <= 0:
            return False  # watchdog disabled
        if getattr(task, "cancel_requested", None):
            return False  # a pause is already in flight; let it land
        last_ts = await self.store.last_event_ts(task.id)
        if last_ts is None:
            return False  # never emitted — leave to the normal loop / startup
        age_min = (now.timestamp() - last_ts) / 60.0
        if age_min < self.stuck_active_minutes:
            return False
        data = task.blocker or {}
        data["category"] = "NOVEL_UNKNOWN"
        data["question"] = (
            f"This task stalled in {task.status.value} — no activity for "
            f"{age_min:.0f} min. The agent/reviewer session likely hung. "
            "Resume to retry, or take over?")
        data["root_cause_hypothesis"] = (
            f"no event for {age_min:.0f} min while {task.status.value}; "
            "probable hung Agent-SDK session")
        task.blocker = data
        await self.store.update_task_columns(task)
        await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        await self._emit(task, "escalated_stalled",
                         f"{task.id[:8]} stalled in {task.status.value} "
                         f"({age_min:.0f}m no activity) — escalated")
        return True

    # Throttled liveness proof. A healthy parked task produces no action
    # events (the watcher acts only on change), which is indistinguishable
    # from a dead watcher in the record — the server ran one for a full day.
    # One wake_tick per task per hour bounds the noise while making "the
    # watcher is checking this task" a queryable fact (`nh doctor` reads it).
    HEARTBEAT = timedelta(hours=1)

    async def _heartbeat(self, task: Task, *, now: datetime) -> None:
        last = _parse_iso((task.context or {}).get("last_wake_tick"))
        if last and now - last < self.HEARTBEAT:
            return
        try:
            # Atomic merge — the heartbeat must never clobber a concurrent
            # writer's context (it did: the watcher ticks every parked task
            # while the CLI and gate write the same rows).
            task.context = await self.store.merge_context(
                task.id, {"last_wake_tick": now.isoformat()})
            await self.store.save_events(task.id, [{
                "source": "watcher", "kind": "wake_tick",
                "text": f"watcher checked ({task.status.value}): nothing to do",
                "ts": time.time(),
            }])
        except Exception:  # noqa: BLE001 — a heartbeat must never break the tick
            log.warning("wake heartbeat failed for %s", task.id[:8], exc_info=True)

    async def _evaluate(self, task: Task, *, now: datetime) -> str | None:
        # An open PR: shepherd it. Merged → done; closed-unmerged → escalate;
        # new human comments → revise (B4); red CI on the PR head → bounded fix
        # loop (M1). It NEVER times out — a PR may wait for human approval
        # indefinitely.
        if task.status == TaskStatus.AWAITING_APPROVAL:
            return await self._check_open_pr(task)

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
        patch = {
            "resumed_at": now_iso(),
            "resume_reason": "wake_condition_satisfied",
        }
        # Same contract as `nh reply` / `nh task resume`: continue from the
        # checkpoint the blocker recorded, or the next attempt branches from a
        # stale sha and discards the parked attempt's committed work.
        checkpoint = resume_checkpoint(task.blocker)
        if checkpoint:
            patch["resume_from"] = checkpoint
        task.context = await self.store.merge_context(task.id, patch)
        task.wake_check_at = None
        await self.store.update_task_columns(task)
        await self.store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
        await self._emit(task, "resumed", f"{task.id[:8]} wake condition satisfied")
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
        comments = [c for c in comments if not self._is_self_or_bot(c)]
        if not comments:
            return None
        rounds = await self._append_comments_as_feedback(task, comments)
        if not (task.context or {}).get("pr_comment_ref"):
            task.context = await self.store.merge_context(
                task.id, {"pr_comment_ref": pr_ref})
        await self._emit(task, "pr_feedback", f"{task.id[:8]} got {len(comments)} PR comment(s)")
        return rounds

    async def _emit(self, task: Task, kind: str, text: str) -> None:
        """Persist a watcher action as a task event and mirror it to the host.

        Persistence is unconditional: the board and the DB record must show
        what the watcher did even when the host wires no callback — the server
        ran with a silent watcher for exactly that reason.
        """
        try:
            await self.store.save_events(task.id, [
                {"source": "watcher", "kind": kind, "text": text, "ts": time.time()},
            ])
        except Exception:  # noqa: BLE001 — visibility must never break the action
            log.warning("failed to persist watcher event %r", kind, exc_info=True)
        self._on_event(kind, text)

    def _is_bot_author(self, author: str) -> bool:
        """Comments from bots (CI result tables, status dashboards) are not
        operator feedback and must never trigger a revision attempt."""
        a = (author or "").lower()
        return a.endswith("[bot]") or a in self.ignore_comment_authors

    def _is_self_or_bot(self, comment) -> bool:
        """A comment that must never trigger a revision: bot chatter OR
        no_human's own output. Author identity can't catch the latter — the
        product posts under the operator's gh login (the 2026-07-10 incident:
        the CI_GATE results comment resumed its own task) — so bodies carry
        AGENT_COMMENT_MARKER and are filtered here."""
        from ..vcs.pr_watcher import is_agent_comment
        return (self._is_bot_author(getattr(comment, "author", ""))
                or is_agent_comment(getattr(comment, "body", None)))

    async def _append_comments_as_feedback(self, task: Task, comments: list) -> int:
        """Append PR comments to send_back_feedback; bump revision_rounds.

        Each entry lands via an atomic list append (concurrent writers both
        survive); the rounds counter is read-then-merge (worst case under two
        watchers: an off-by-one round count, never lost feedback). Refreshes
        ``task.context`` from the store. Returns the new round count.
        """
        entries = []
        for c in comments:
            # Support both PrComment objects and plain dicts/strings.
            if hasattr(c, "body"):
                msg = c.body
                author = getattr(c, "author", "reviewer")
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
                loc = f"{path}" + (f":{line}" if line else "")
                msg = f"[{loc}] {msg}"
            if diff_hunk:
                msg += f"\n\nContext:\n```\n{str(diff_hunk)[:500]}\n```"
            entries.append({
                "at": created, "message": msg, "author": author,
                "source": "pr_comment",
            })
        for entry in entries:
            await self.store.append_context_list(
                task.id, "send_back_feedback", entry)
        rounds = int((task.context or {}).get("revision_rounds", 0)) + 1
        task.context = await self.store.merge_context(
            task.id, {"revision_rounds": rounds})
        return rounds

    async def _check_open_pr(self, task: Task) -> str | None:
        """The awaiting-approval priority ladder, one rung per tick.

        1. **Merged** → DONE. The agent only ever *observes* merged-ness —
           the never-merge constraint is untouched. (Before this ladder, a
           merged PR left its task parked as awaiting_approval forever.)
        2. **Closed unmerged** → ESCALATED with a question (previously polled
           until the end of time).
        3. **New human comments** → inject + revise (existing B4 path).
        4. **Red CI on the PR head** → bounded fix loop: fetch the failing
           check's log, feed it back, resume onto the PR branch. Rounds are
           counted per distinct failure *signature* — a re-run of the same red
           check never burns a round — and past the cap the specific failing
           check is handed to the human. This is the gap PR #531 exposed: the
           Jenkinsfile died in Jenkins' CPS compiler while every local check
           passed, and nothing was watching.
        """
        ctx = task.context or {}
        url = ctx.get("pr_watch")
        if not url:
            return None

        state = ""
        if self._pr_state is not None:
            try:
                state = (await self._pr_state(url)) or ""
            except Exception as exc:  # noqa: BLE001 — a poll error must not crash the watcher
                log.warning("failed to poll PR state for %s: %s", task.id[:8], exc)
        if state == "MERGED":
            await self.store.set_status(task, TaskStatus.DONE, validate=False)
            await self._emit(task, "merged", f"{task.id[:8]} PR merged by a human: {url}")
            return "merged"
        if state == "CLOSED":
            data = task.blocker or {}
            data["category"] = "AMBIGUITY"
            data["question"] = (
                "The PR was closed without merging. Abandon the task, or rework "
                "and reopen?"
            )
            data["root_cause_hypothesis"] = f"PR closed unmerged: {url}"
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(task, "pr_closed", f"{task.id[:8]} PR closed unmerged: {url}")
            return "escalated_pr_closed"

        acted = await self._check_approval_pr_comments(task)
        if acted:
            return acted
        acted = await self._check_pr_ci(task, url)
        if acted:
            return acted
        # 5. CI_GATE integration gate (M6): PR CI is green (or unknown, which
        #    the gate re-checks explicitly) — run the integration validation
        #    once per PR head, bounded send-back on failure.
        return await self._check_ci_gate_integration(task, url)

    async def _check_pr_ci(self, task: Task, url: str) -> str | None:
        """Rung 4: react to a red check on the open PR's head, bounded."""
        if self._pr_checks is None:
            return None
        try:
            checks = await self._pr_checks(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to poll PR checks for %s: %s", task.id[:8], exc)
            return None
        failing = [c for c in checks if c.get("status") == "fail"]
        if not failing:
            return None
        # A distinct-failure signature. The link carries the build number, so
        # polling the same red build repeatedly while parked is free, but a NEW
        # build failing the same checks (the coder's fix didn't take) is a new
        # round. Names alone deadlocked here: after one fix push, the same
        # failing names read as "already handled" and the watcher went silent.
        signature = hashlib.sha256(
            "|".join(sorted(f"{c.get('name', '')}@{c.get('link', '')}" for c in failing)).encode()
        ).hexdigest()[:16]
        ctx = task.context or {}
        if ctx.get("pr_ci_last_sig") == signature:
            return None  # already acted on this exact run; wait for a new build
        excerpt = ""
        if self._ci_log is not None and failing[0].get("link"):
            try:
                excerpt = await self._ci_log(failing[0]["link"])
            except Exception:  # noqa: BLE001 — the log is a bonus, not a dependency
                excerpt = ""
        names = ", ".join(c.get("name", "?") for c in failing)
        rounds = int(ctx.get("pr_ci_rounds") or 0) + 1
        task.context = await self.store.merge_context(
            task.id, {"pr_ci_rounds": rounds, "pr_ci_last_sig": signature})

        if rounds > self.max_ci_fix_rounds:
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                f"CI on the PR is still red after {rounds - 1} autonomous fix "
                f"round(s). Failing: {names}. Advise, or take over?"
            )
            data["root_cause_hypothesis"] = f"PR CI failing: {names}"
            data["evidence"] = (excerpt or failing[0].get("link", ""))[:1500]
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "escalated_ci",
                f"{task.id[:8]} PR CI red past {self.max_ci_fix_rounds} rounds: {names}",
            )
            return "escalated_ci"

        message = (
            f"The PR's CI is failing. Check(s): {names}.\n"
            f"Link: {failing[0].get('link', '')}\n"
            + (f"Log excerpt:\n```\n{excerpt}\n```\n" if excerpt else "")
            + "Fix the cause on the same branch; the push updates the PR and "
              "re-runs the checks."
        )
        await self.store.append_context_list(task.id, "send_back_feedback", {
            "at": now_iso(), "message": message, "author": "ci", "source": "pr_ci",
        })
        task.context = await self.store.merge_context(task.id, {})
        await self._emit(
            task, "pr_ci_red",
            f"{task.id[:8]} CI failing ({names}) — fix round {rounds}/{self.max_ci_fix_rounds}",
        )
        return await self._resume(task)

    async def _check_ci_gate_integration(self, task: Task, url: str) -> str | None:
        """Rung 5 (M6): run the CI_GATE integration validation post-PR, gated.

        The gate object owns eligibility, the once-per-head + in-flight +
        namespace duplicate guards, triggering, polling one status call per
        tick, and posting the PR results comment. This method owns what the
        verdict DOES to the task: pass → stays awaiting_approval (a human
        still merges); fail → bounded send-back to the coder, then escalate;
        refused (code PR needing a PR-built image) → honest escalation.
        """
        _outcome, action = await self._ci_gate_step(task, url)
        return action

    async def _ci_gate_step(self, task: Task, url: str) -> tuple[Any, str | None]:
        """One CI_GATE gate step + its task-level consequence. Returns
        (gate outcome | None, watcher action | None) — `nh ci_gate run` drives
        this directly so the manual path IS the watcher path."""
        if self._ci_gate_gate is None:
            return None, None
        try:
            outcome = await self._ci_gate_gate.step(task, url)
        except Exception as exc:  # noqa: BLE001 — the gate must never kill the watcher
            log.warning("CI_GATE gate step failed for %s: %s", task.id[:8], exc)
            return None, None
        # The gate mutates task.context["ci_gate"] in memory (its state
        # machine) — persist that subtree atomically. RFC 7396: an empty dict
        # merges nothing, so a cleared state ({}) must become None (delete).
        state = (task.context or {}).get("ci_gate")
        task.context = await self.store.merge_context(
            task.id, {"ci_gate": state if state else None})

        if outcome.action == "skip":
            return outcome, None
        if outcome.action == "blocked":
            await self._emit(task, "ci_gate_blocked",
                             f"{task.id[:8]} CI_GATE: {outcome.reason}")
            return outcome, None
        if outcome.action == "triggered":
            await self._emit(task, "ci_gate_trigger",
                             f"{task.id[:8]} CI_GATE: {outcome.reason}")
            return outcome, "ci_gate_triggered"
        if outcome.action == "waiting":
            await self._emit(task, "ci_gate_poll",
                             f"{task.id[:8]} CI_GATE: {outcome.reason}")
            return outcome, None
        if outcome.action == "passed":
            await self._emit(
                task, "ci_gate_pass",
                f"{task.id[:8]} CI_GATE integration PASSED: {outcome.web_url}"
                + (" (PR comment posted)" if outcome.comment_posted else ""),
            )
            return outcome, "ci_gate_passed"
        if outcome.action == "refused":
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                "CI_GATE validation is required but cannot run honestly: "
                f"{outcome.reason} Proceed without it, or wire the PR-image build?"
            )
            data["root_cause_hypothesis"] = outcome.reason
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(task, "ci_gate_refused",
                             f"{task.id[:8]} CI_GATE cannot run: {outcome.reason}")
            return outcome, "escalated_ci_gate_refused"

        # failed — bounded send-back, counted per pipeline run (a new run only
        # ever starts on a new PR head, so each failure is a distinct signature).
        names = ", ".join(outcome.failing_jobs) or "pipeline"
        rounds = int((task.context or {}).get("ci_gate_fix_rounds") or 0) + 1
        task.context = await self.store.merge_context(
            task.id, {"ci_gate_fix_rounds": rounds})
        if rounds > self.max_ci_gate_fix_rounds:
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                f"CI_GATE integration still failing after {rounds - 1} autonomous "
                f"fix round(s). Failing: {names}. Advise, or take over?"
            )
            data["root_cause_hypothesis"] = f"CI_GATE integration failing: {names}"
            data["evidence"] = (outcome.log_excerpt or outcome.web_url)[:1500]
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "ci_gate_fail",
                f"{task.id[:8]} CI_GATE red past {self.max_ci_gate_fix_rounds} "
                f"rounds: {names} — escalated",
            )
            return outcome, "escalated_ci_gate"

        message = (
            f"The CI_GATE integration validation failed. Job(s): {names}.\n"
            f"Pipeline: {outcome.web_url}\n"
            + (f"Log tail:\n```\n{outcome.log_excerpt}\n```\n"
               if outcome.log_excerpt else "")
            + "Fix the cause on the same branch; the push updates the PR and "
              "the validation re-runs on the new head."
        )
        await self.store.append_context_list(task.id, "send_back_feedback", {
            "at": now_iso(), "message": message, "author": "ci_gate",
            "source": "ci_gate",
        })
        task.context = await self.store.merge_context(task.id, {})
        await self._emit(
            task, "ci_gate_fail",
            f"{task.id[:8]} CI_GATE failing ({names}) — fix round "
            f"{rounds}/{self.max_ci_gate_fix_rounds}",
        )
        return outcome, await self._resume(task)

    async def _check_approval_pr_comments(self, task: Task) -> str | None:
        """Poll an awaiting-approval PR for NEW human comments (B4).

        Uses a per-task ``pr_comment_since`` cursor so the same comment never
        triggers a second revision. On new comments: inject them, advance the
        cursor, and either resume the task to revise or — past the revision cap —
        escalate to the human. Never times out.
        """
        ctx = task.context or {}
        url = ctx.get("pr_watch")
        if not url or self._pr_comment is None:
            return None
        try:
            comments = await self._pr_comment(url)
        except Exception as exc:  # noqa: BLE001 — a poll error must not crash the watcher
            log.warning("failed to poll PR comments for %s: %s", task.id[:8], exc)
            return None

        since = ctx.get("pr_comment_since")
        fresh = [c for c in comments
                 if not since or (getattr(c, "created_at", "") or "") > since]
        if not fresh:
            return None

        # Advance the cursor past everything we've now seen (newest wins).
        newest = max((getattr(c, "created_at", "") or "") for c in comments)
        human = [c for c in fresh if not self._is_self_or_bot(c)]
        if not human:
            # Bot chatter only (CI result tables etc.): move the cursor so the
            # same comments are never reconsidered, but do not burn an attempt.
            if newest:
                task.context = await self.store.merge_context(
                    task.id, {"pr_comment_since": newest})
            await self._emit(
                task, "pr_feedback_skipped",
                f"{task.id[:8]} ignored {len(fresh)} bot comment(s) "
                f"({', '.join(sorted({getattr(c, 'author', '?') for c in fresh}))})",
            )
            return None
        rounds = await self._append_comments_as_feedback(task, human)
        if newest:
            task.context = await self.store.merge_context(
                task.id, {"pr_comment_since": newest})
        await self._emit(task, "pr_feedback", f"{task.id[:8]} got {len(human)} new PR comment(s)")

        if rounds > self.max_revision_rounds:
            await self._escalate_revisions(task, rounds)
            return "escalated_revisions"
        return await self._resume(task)

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
        await self.store.update_task_columns(task)
        if task.status != TaskStatus.ESCALATED:
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        await self._emit(
            task, "escalated_revisions",
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
        await self.store.update_task_columns(task)
        if task.status != TaskStatus.ESCALATED:
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        await self._emit(task, "escalated_timeout", f"{task.id[:8]} parked past max duration")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
