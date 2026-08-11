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
from ..vcs.pr_outcome import observe_pr
from .taxonomy import Blocker, resume_checkpoint, resume_provenance

log = logging.getLogger("no_human.wake")

# Async hooks the host wires in (live PR/CI lookups). Default: not satisfied.
PrMergedChecker = Callable[[str], Awaitable[bool]]
CiGreenChecker = Callable[[str], Awaitable[bool]]
# Returns (is_terminal, is_success) for a pipeline ID.
CiTerminalChecker = Callable[[str], Awaitable[tuple[bool, bool]]]
# Returns list of new PrComment objects for a PR ref.
PrCommentChecker = Callable[[str], Awaitable[list[Any]]]
# (repo_path, branch, base) -> whether branch's content already landed on base
# (a local, content-based check — see default_branch_shipped for why a
# squash merge makes ancestry the wrong test).
PrShippedChecker = Callable[[str, str, str], Awaitable[bool]]

#: Sentinel returned by ``_complete_if_content_landed`` when the task went
#: TERMINAL while the (subprocess-heavy) content probe was running. It is not
#: an action: the caller must abandon its tick entirely rather than fall
#: through to its own escalate/send-back path, which is the SCRUM-68 guard the
#: probe's own callers had inline before the path was shared.
_TICK_ABORTED = "__tick_aborted__"

_DURATION = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Platform-layer CI failures: the job never ran the code, so a red check
# carrying this signature is INFRA, not a coder fix round (live incident
# 2026-08-11: a GitHub Actions billing outage turned every review-PASSED
# task into escalated_ci at the finish line). The match is ONE full
# sentence, deliberately: partial phrases ("the job was not started
# because…") also appear in unrelated failures and in pytest echoes of this
# very corpus, and a match here SUPPRESSES reporting — overmatching is the
# dangerous direction. An unrecognized failure is always treated as real.
# REACH (review finding, 2026-08-12): this classifier reads the ci_log
# excerpt, and the only production ci_log is the Jenkins consoleText
# fetcher — GitHub Actions exposes this text ONLY via the check-run
# annotation, which nothing fetches yet. So today this fires for
# log-yielding forges only; ticket 8c8b36b5 wires the annotation channel.
# Until it lands, blockers.pr_ci_policy=advisory is the operational cover
# for GitHub-hosted checks.
_CI_INFRA_RE = re.compile(
    r"(?i)recent account payments have failed or your "
    r"spending limit needs to be increased"
)


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
        pr_mergeable: Callable[[str], Awaitable[dict]] | None = None,
        ci_log: Callable[[str], Awaitable[str]] | None = None,
        pr_shipped: PrShippedChecker | None = None,
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
        # Copilot pattern: bounded rounds, then hand the specific failure to a  # term-ok: real behavior-pattern reference
        # human). Counted per distinct failure signature, so a re-run of the
        # same red check doesn't burn a round.
        self.max_ci_fix_rounds = int(blockers_cfg.get("max_ci_fix_rounds", 3))
        # TEMPORARY OPERATOR OVERRIDE (2026-08-12): "advisory" makes rung 5
        # record a red PR check without counting a fix round, resuming the
        # coder, or escalating — the operator's instruction while the private
        # repo's GitHub Actions quota is exhausted for the month ("DO NOT FAIL
        # TASKS ON IT FOR NOW"). The default is "enforce"; the override lives
        # in ~/.no_human/config.yaml and MUST be removed at go-public (public
        # repos have unlimited Actions minutes, so CI becomes meaningful
        # again). The billing-signature classifier below stays either way —
        # it is the permanent, self-reverting handling for platform-layer
        # failures; this knob exists because a quota-blocked job may expose
        # no log at all, which the fail-closed classifier cannot see.
        self.pr_ci_policy = str(
            blockers_cfg.get("pr_ci_policy", "enforce")
        ).strip().lower()
        if self.pr_ci_policy not in ("enforce", "advisory"):
            # Fail closed, loudly: a typo ("Advisory ") silently meaning
            # "enforce" would re-escalate every task mid-outage with no signal.
            log.warning(
                "blockers.pr_ci_policy %r is not 'enforce'/'advisory' — "
                "falling back to 'enforce'", self.pr_ci_policy,
            )
            self.pr_ci_policy = "enforce"
        # Bounded PR-conflict → rebase cycles (SCRUM-41), same pattern: a PR
        # that textually conflicts with main is invisible to CI (branch checks
        # stay green through it) — only `gh pr view --json mergeable` exposes
        # it. Counted per detected CONFLICTING state; resets only once GitHub
        # confirms MERGEABLE (a later conflict is then a fresh cycle).
        self.max_pr_conflict_rounds = int(
            blockers_cfg.get("max_pr_conflict_rounds", 3)
        )
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
        # incident: a CI service account posts a unit-test-results table on every
        # build, which the comment rung injected as human feedback and resumed
        # the task — one wasted attempt per PR, forever. "[bot]" logins are
        # always ignored on top of this list. In-code default rather than
        # config.py DEFAULTS because a user yaml `blockers:` section replaces
        # that map wholesale (the deep-merge shadowing trap).
        self.ignore_comment_authors = {
            str(a).lower()
            for a in blockers_cfg.get("ignore_comment_authors", [])
        }
        self._pr_merged = pr_merged
        self._ci_green = ci_green
        self._ci_terminal = ci_terminal
        self._pr_comment = pr_comment
        self._pr_state = pr_state
        self._pr_checks = pr_checks
        self._pr_mergeable = pr_mergeable
        self._ci_log = ci_log
        self._pr_shipped = pr_shipped
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
                # attempt (a PR once accumulated 17 near-identical comments).
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
            # Feedback the task could act on — NOT merely "a comment exists".
            # The rung used to satisfy on len(comments) > 0, so the task's own
            # marked comment (an abandoned-draft note, a verification receipt,
            # a CI_GATE table) woke the very task that posted it, which then
            # found nothing to revise — `_inject_pr_feedback` filters self and
            # bot chatter — and burned an attempt on an empty round.
            return bool(await self._human_pr_comments(pr_ref))

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

    async def tick(
        self, *, now: datetime | None = None,
        active_ids: set[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Re-evaluate all parked tasks once. Returns (task_id, action) tuples
        where action is 'resumed' or 'escalated_timeout'.

        ``active_ids`` is the caller's set of worker-CLAIMED task ids; the
        stuck-active sweep judges only those. A resumed task waiting in an
        active status for a free worker slot is silent because nothing is
        running it — parking it as "hung" re-created its escalation every 40
        minutes behind a deep queue (live, 2026-07-24). ``None`` means the
        caller cannot know what is claimed (standalone ``nh wake``), so the
        sweep — whose whole purpose is freeing hung worker slots — is skipped.
        """
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
        # a legitimately long test run never trips it). Scope: only tasks the
        # caller actually CLAIMED — see the docstring.
        if active_ids is not None:
            for status in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
                           TaskStatus.TESTING, TaskStatus.PLANNING,
                           TaskStatus.CONTEXT):
                for task in await self.store.list_tasks(status):
                    if task.id not in active_ids:
                        continue
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
        # Load-bearing terminal guard (SCRUM-68) — a task shipped/cancelled
        # between the caller's list fetch and this write must not be flipped
        # to ESCALATED by the stall watchdog.
        if await self._is_terminal(task):
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

    async def _is_terminal(self, task: Task) -> bool:
        """True once only an explicit human verb (never this watcher) may
        revive the task: done, or cancelled (FAILED + a cancel_reason —
        there is no separate 'cancelled' status; see api/app.py's cancel
        endpoint). Re-reads the store instead of trusting the possibly-stale
        `task` object: a concurrent POST /shipped or /cancel can land mid-tick,
        between a rung's own network poll (PR state/comments/checks/mergeable)
        and its write — live incident SCRUM-68, where a done task's PR got a
        post-merge comment and the pr_feedback rung resumed it to implementing."""
        current = await self.store.get_task(task.id)
        if current is None:
            return False
        if current.status == TaskStatus.DONE:
            return True
        return current.status == TaskStatus.FAILED and bool(
            (current.context or {}).get("cancel_reason"))

    async def _evaluate(self, task: Task, *, now: datetime) -> str | None:
        # Terminal is terminal — checked first, before any rung does any work.
        if await self._is_terminal(task):
            return None

        # An open PR: shepherd it. Merged → done; closed-unmerged → escalate;
        # new human comments → revise (B4); red CI on the PR head → bounded fix
        # loop (M1). It NEVER times out — a PR may wait for human approval
        # indefinitely.
        if task.status == TaskStatus.AWAITING_APPROVAL:
            return await self._check_open_pr(task)

        # A human chose "stop — keep the work parked as-is" (SCRUM-22's
        # terminal park). Review 2026-07-25: without this skip the sweep
        # undid the stop — max_park re-escalated the task within 48h and any
        # wake_condition on the blocker resumed it. Human decisions outrank
        # every automatic branch below; only another human reply changes it.
        if (task.blocker or {}).get("human_stopped"):
            return None

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
                # condition_satisfied may have awaited a live checker (network);
                # re-verify terminal-ness right before acting on it (wake_condition
                # rung, and the pr_feedback rung it can trigger below).
                if await self._is_terminal(task):
                    return None
                # If the condition is pr_comment_on, inject the comments as feedback.
                # `rounds` stays 0 for every other condition — only None means
                # "the injection delivered nothing", and only that falls through.
                rounds: int | None = 0
                if condition and condition.strip().lower().startswith("pr_comment_on:"):
                    rounds = await self._inject_pr_feedback(task, condition)
                    # Bound the comment→revise loop: after max_revision_rounds
                    # autonomous rounds, escalate to the human rather than resume.
                    if rounds is not None and rounds > self.max_revision_rounds:
                        await self._escalate_revisions(task, rounds)
                        return "escalated_revisions"
                if rounds is not None:
                    return await self._resume(task)
                # The rung and the injection each fetch, so they can see
                # different data however well they agree on the predicate: a 502
                # between the two calls, a comment deleted or edited, or the task
                # going terminal mid-await all end here. Resuming anyway is what
                # burned an attempt on an empty round — the very failure the
                # marker was added to stop. So: do NOT resume, but FALL THROUGH
                # to the max_park check below rather than returning. An early
                # return here stranded the task forever on an ALTERNATING forge
                # (answers the rung, fails the injection — gh secondary rate
                # limits, which this design meets twice as often because it
                # fetches twice per tick): no resume, no escalation, the human's
                # review never delivered. Inside max_park the next tick still
                # re-decides on a fresh read; past it, the timeout escalates.

        # Timeout → escalate (never silently abandon). Re-verify: max_park
        # re-escalation must not revive a task a human already closed out.
        if now - raised_at >= self.max_park:
            if await self._is_terminal(task):
                return None
            await self._escalate_timeout(task, blocker)
            return "escalated_timeout"
        return None

    async def _resume(self, task: Task) -> str:
        """Flip a parked task back to its prior working state (IMPLEMENTING).

        Resume re-enters the loop in a fresh session seeded with the report
        (22.5) — the orchestrator picks it up from the [WIP-BLOCKED] checkpoint.
        """
        # LOAD-BEARING terminal guard (SCRUM-68). The rung-level rechecks above
        # this call are cheap early-outs; THIS one is the invariant — every
        # resume path, present or future, funnels through here, and any await
        # a rung did since its own recheck reopens the race this closes.
        if await self._is_terminal(task):
            return "skipped_terminal"
        patch = {
            "resumed_at": now_iso(),
            "resume_reason": "wake_condition_satisfied",
        }
        # Same contract as `nh reply` / `nh task resume`: continue from the
        # checkpoint the blocker recorded, or the next attempt branches from a
        # stale sha and discards the parked attempt's committed work.
        checkpoint = resume_checkpoint(task.blocker)
        # Stamp the provenance UNCONDITIONALLY: the zero-diff honesty gate must
        # be able to tell a MACHINE resume (a timer, a CI rung, an auto-rebase)
        # from a human answering a blocker, and crediting a machine resume opens
        # a PR on work no attempt produced.
        #
        # 🔴 This is deliberately NOT inside `if checkpoint:`. Gating the stamp
        # on the checkpoint is what made `by` a ONE-WAY LATCH through five review
        # rounds: `resume_from` is merged with RFC 7396, so when this resume had
        # no checkpoint of its own the write was skipped entirely and a `by`
        # written by the PREVIOUS actor survived to describe THIS one. Whichever
        # order the reader then used, it was wrong in one direction — a stale
        # "human" credited a timer's re-entry, a stale "wake" failed a human's
        # answer as fabrication. `by` must always describe the resume that is
        # actually happening, so it is written every time, checkpoint or not.
        # `resume_reason` beside it says the same thing and is kept for rows
        # written before provenance existed.
        patch["resume_from"] = resume_provenance(checkpoint, "wake")
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
        pr_ref = condition.split(":", 1)[1].strip()
        comments = await self._human_pr_comments(pr_ref)
        if not comments:
            return None
        # The comment fetch above is a network await — a POST /shipped landing
        # during it must not have its own merge-notice comment injected as
        # feedback into a finished task (the SCRUM-68 incident, one await
        # deeper than the rung's own recheck).
        if await self._is_terminal(task):
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

    async def _human_pr_comments(self, pr_ref: str) -> list:
        """Comments on *pr_ref* that are actual feedback: no bot chatter, and
        none of no_human's own marked output.

        The one place the two rungs that ask "is there feedback on this PR?"
        route through — the wake condition and the injection that follows it.
        Filtering at the FETCH is what keeps them from disagreeing: they used
        to, and the rung's unfiltered `len(comments) > 0` woke tasks on their
        own comments that the injection then discarded.

        A missing checker or a fetch error yields no feedback (never an
        exception): a forge blip must not crash the watcher, and "we could not
        look" is not "a human replied".

        (The `_check_approval_pr_comments` poll rung deliberately does NOT use
        this: it needs the unfiltered list to advance its `pr_comment_since`
        cursor past bot comments, or it re-reads them forever. It applies the
        same `_is_self_or_bot` predicate after that.)
        """
        if self._pr_comment is None:
            return []
        try:
            comments = await self._pr_comment(pr_ref)
        except Exception as exc:  # noqa: BLE001 — checker must never crash watcher
            log.warning("pr_comment checker failed for %s: %s", pr_ref, exc)
            return []
        return [c for c in comments if not self._is_self_or_bot(c)]

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

    async def _complete_if_content_landed(
        self, task: Task, url: str, *, forge_state: str, action: str,
        situation: str,
    ) -> str | None:
        """THE completion path for "this branch's content is already on base".

        ONE path, two callers — the CLOSED rung (which has always had it,
        inline) and the CONFLICTING rung (which used to start a rebase round
        without ever asking). Returns *action* once it has recorded the
        outcome and written DONE; ``_TICK_ABORTED`` if the task went terminal
        while the probe ran — on ANY answer the probe gave, see the guard
        below, and both callers must abandon the tick on it; ``None`` for
        every "no" — hook not wired, no branch or no base recorded, probe
        error, or content genuinely absent — all of which mean the caller
        keeps its existing behaviour unchanged.

        The question is CONTENT, not ancestry, and that is not a preference:
        this repo lands every PR as an identity-normalized LOCAL squash, so the
        landing commit has no lineage back to the branch and
        ``git merge-base --is-ancestor`` is False for every PR we ever merged.
        See ``default_branch_shipped``.

        WHAT A ``True`` HERE MEANS, precisely — it is stronger than "some of
        this shipped". ``default_branch_shipped`` merges the branch into the
        base tip (both directions) and demands the result be EXACTLY the tip's
        tree, i.e. the branch has nothing left to contribute. A PARTIALLY
        landed branch — half a rename, one of two files, a follow-up commit
        still outstanding — writes a different tree and reads False, so it can
        never complete here. That is pinned by test (the half-landed rename).

        A ``False`` is deliberately overloaded ("absent" and "could not run"
        collapse into it, per that function's contract), which is why it is
        only ever read as "keep going": the caller's existing path — escalate
        to a human, or run the rebase round — is the safe side of the
        ambiguity in both callers.

        REVIEW PRECONDITION. Neither caller may complete a task whose review
        never passed, and neither needs its own check for that: both are
        reached only through ``_check_open_pr``, which ``_evaluate`` calls only
        for ``AWAITING_APPROVAL`` — the status a task reaches only after its
        review passed and its PR was opened. That, plus a recorded
        ``pr_branch``, is the whole precondition set of the CLOSED rung this
        was extracted from, and it is preserved exactly. Pinned by the test
        that a BLOCKED task with landed content is still never completed.
        """
        if self._pr_shipped is None:
            return None
        ctx = task.context or {}
        branch = ctx.get("pr_branch")
        base = ctx.get("base_branch")
        if not task.repo_path or not branch:
            return None
        if not base:
            # NEVER default to "main". The orchestrator persists the resolved
            # base before any attempt runs (`orchestrator.py`: `if not
            # ctx.get("base_branch")` → `_implicit_base_branch`, then
            # `update_task`), so every task that can reach AWAITING_APPROVAL
            # has one and this is unreachable for them. If it is ever reached —
            # a legacy row, a hand-built context — the honest answer is "I do
            # not know what this PR targets", not a guess: a backport opened
            # against `release/2.3` would be asked about `main`, where the
            # content usually IS present, and would complete a task whose work
            # never reached its actual base. Falling back costs a spurious
            # escalation or one wasted rebase round, with a human on the other
            # end of both.
            log.warning("no base_branch recorded for %s — skipping the "
                        "content check rather than guessing", task.id[:8])
            return None
        try:
            shipped = await self._pr_shipped(task.repo_path, branch, base)
        except Exception as exc:  # noqa: BLE001 — a checker error must not crash the watcher
            log.warning("pr_shipped check failed for %s: %s", task.id[:8], exc)
            shipped = False
        # The shipped check just ran several local git subprocesses (rev-parse,
        # and up to two merge-trees per candidate base tip) — easily a few
        # seconds on a large repo — so re-verify terminal-ness before ANY
        # caller acts, same SCRUM-68 guard as every other rung.
        #
        # 🔴 THIS SITS ABOVE THE `not shipped` EARLY-OUT AND MUST STAY THERE.
        # The guard is about the AWAIT, not about the answer: a `POST /shipped`
        # or `/cancel` landing while the probe ran leaves the caller's own
        # recheck stale on EVERY path out of here. Review 2026-08-11 caught it
        # narrowed to the positive path alone, and reproduced the cost — a
        # phantom "abandon or rework?" blocker, a `pr_closed` event and an
        # outcome row on a task that was already DONE, plus a round counter and
        # a send-back on the conflict rung. The store's CAS refuses only the
        # STATUS flip; nothing else here is CAS-guarded, so a "harmless"
        # refused transition still leaves a record of a state change that never
        # happened — the shape `cli/commands.py`'s restore-approval path
        # already documents as ruled wrong. The probe raising is treated as
        # `shipped=False` (not as an early return) for exactly this reason.
        if await self._is_terminal(task):
            return _TICK_ABORTED
        if not shipped:
            return None
        await observe_pr(self.store, task.id, url, forge_state=forge_state,
                         shipped=True)
        await self.store.set_status(task, TaskStatus.DONE, validate=False)
        await self._emit(
            task, "shipped",
            f"{task.id[:8]} {situation} but its content is already on "
            f"{base} (squash-merged): {url}",
        )
        return action

    async def _check_open_pr(self, task: Task) -> str | None:
        """The awaiting-approval priority ladder, one rung per tick.

        1. **Merged** → DONE. The agent only ever *observes* merged-ness —
           the never-merge constraint is untouched. (Before this ladder, a
           merged PR left its task parked as awaiting_approval forever.)
        2. **Closed unmerged** → ESCALATED with a question (previously polled
           until the end of time).
        3. **Textual conflict with main** (SCRUM-41) → bounded rebase loop:
           CI stays green through a conflict (it only runs the PR's own
           branch), so this is the one rung that polls `mergeable` directly.
           UNKNOWN is GitHub still computing (notably right after the rebase
           push this rung itself asks for) — never acted on. Definite
           CONFLICTING sends the rebase instruction back, bounded like the
           CI-fix rounds; past the cap the conflict is handed to the human.
           Live occurrence: PR #26 conflicted with #25 and sat invisible
           until a human tried to merge it.
        4. **New human comments** → inject + revise (existing B4 path).
        5. **Red CI on the PR head** → bounded fix loop: fetch the failing
           check's log, feed it back, resume onto the PR branch. Rounds are
           counted per distinct failure *signature* — a re-run of the same red
           check never burns a round — and past the cap the specific failing
           check is handed to the human. This is the gap a real run exposed:
           the CI pipeline definition failed to compile on the server while
           every local check passed, and nothing was watching.
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
        # The poll above just awaited a network call; re-verify terminal-ness
        # before acting on MERGED/CLOSED (state rung, SCRUM-68) — a concurrent
        # POST /cancel landing mid-poll must not still write DONE/ESCALATED.
        if await self._is_terminal(task):
            return None
        if state == "MERGED":
            await observe_pr(self.store, task.id, url, forge_state=state)
            await self.store.set_status(task, TaskStatus.DONE, validate=False)
            await self._emit(task, "merged", f"{task.id[:8]} PR merged by a human: {url}")
            return "merged"
        if state == "CLOSED":
            # GitHub's merged flag is never true for our PRs: the operator's
            # hard rule is a LOCAL, identity-normalized squash merge (never
            # `gh pr merge`), so a squash commit lands on base with a fresh
            # SHA that has no commit-graph lineage back to the branch — every
            # shipped PR still reports CLOSED here. Trusting that flag alone
            # escalated every successful task (SCRUM-68 follow-up). Before
            # escalating, ask git (not GitHub) whether the branch's content is
            # actually present on its base — that's true regardless of how
            # the commit graph got there.
            # FOR THE PR-OUTCOME RECORD ONLY — the escalation behaviour below is
            # deliberately unchanged.
            #
            # The `shipped` this rung RECORDS is True or None, NEVER False,
            # and that is not an oversight (the helper records the True itself;
            # the fall-through below records the None).
            # `default_branch_shipped` is documented to return
            # False for BOTH "the content is not on base" and "the check could
            # not run" (missing repo, deleted branch, unrelated histories) —
            # "callers must treat False as 'can't tell'". That collapse is
            # correct for the escalation decision, which only needs to never
            # see a false "shipped" and has a human on the other end of it.
            #
            # It is wrong for a RECORD. `closed_unmerged` is a SETTLED outcome,
            # so it is never re-polled; writing one from an ambiguous False
            # would permanently file a PR as "closed without merging" on the
            # strength of a git command that failed. The likeliest cause of
            # that failure is the branch being gone — which is what happens
            # AFTER a successful squash merge, and after the task's temporary
            # worktree is cleaned up. The mistake would therefore land hardest
            # on exactly the PRs that did merge, i.e. it would invert the
            # number this table exists to produce.
            #
            # So the watcher records only what it is certain of. A False leaves
            # the row `unknown`, which is UNSETTLED, so `nh pr-outcomes refresh`
            # re-polls it later with a probe that can tell the two cases apart
            # (`pr_outcome.probe_shipped`). Nothing is lost, and nothing is
            # asserted that was not observed.
            landed = await self._complete_if_content_landed(
                task, url, forge_state=state, action="shipped_pr_closed",
                situation="PR closed")
            if landed == _TICK_ABORTED:
                return None
            if landed:
                return landed
            # Reaching here is exactly the `True`-or-`None` rule above: the
            # helper writes DONE (and returns an action) on its ONLY `True`,
            # so every path that falls through is one of the ambiguous cases —
            # no probe wired, no branch recorded, the probe raised, or it said
            # False, which is itself "absent OR could not run". None of those
            # is evidence of absence, so the RECORD gets `None` and stays
            # `unknown` (unsettled, re-polled) while the ESCALATION below —
            # deliberately unchanged — proceeds and puts a human on it.
            await observe_pr(self.store, task.id, url, forge_state=state,
                             shipped=None)
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

        # Neither merged nor closed: the PR is OPEN, or its state could not be
        # read at all. Record that too — an `open` that is genuinely open and an
        # `unknown` the poll could not resolve are different facts, and the
        # whole point of this table is that the second one never counts as the
        # first. `checks=None`: this rung did not fetch CI, so whatever a
        # previous refresh measured stays.
        await observe_pr(self.store, task.id, url, forge_state=state)

        acted = await self._check_pr_conflict(task, url, state)
        if acted:
            return acted
        acted = await self._check_approval_pr_comments(task)
        if acted:
            return acted
        acted = await self._check_pr_ci(task, url)
        if acted:
            return acted
        # 6. CI_GATE integration gate (M6): PR CI is green (or unknown, which
        #    the gate re-checks explicitly) — run the integration validation
        #    once per PR head, bounded send-back on failure.
        return await self._check_ci_gate_integration(task, url)

    async def _check_pr_conflict(self, task: Task, url: str,
                                 forge_state: str = "") -> str | None:
        """Rung 3 (SCRUM-41): a textual conflict with main is invisible to CI
        (branch checks only run the PR's own branch) — this rung is the only
        one that polls `gh pr view --json mergeable,mergeStateStatus` directly.

        GitHub computes ``mergeable`` asynchronously after every push,
        including the rebase push this rung itself asks for — so "UNKNOWN" is
        the normal state for a few seconds after every round, not a real
        signal. It must never be treated as resolved (would leave a real
        conflict unhandled) NOR reset the round counter (would let a
        CONFLICTING → UNKNOWN → CONFLICTING cycle — the normal shape of a
        rebase-and-repoll — reset every round and never reach the bound). The
        counter only resets on a *definite* MERGEABLE: that is a genuinely new
        failure cycle, not a continuation.

        SHIPPED-FIRST (2026-08-11). Before starting OR continuing a round, ask
        git whether the branch's content is already on the base; if it is, the
        round has nothing to do and the task completes through the shared
        ``_complete_if_content_landed`` path instead. Measured live twice that
        day: a round is an ENTIRE coder attempt (session + tests + review +
        delivery — millions of tokens, ~1h wall), and task 5ef97879's attempt 9
        was moot at birth because PR #183's content was mid-landing through a
        supervised local squash. It had to be paused by hand.

        WHY THE TWO SIGNALS CAN HONESTLY DISAGREE, since "CONFLICTING yet
        already contained" reads like a contradiction: they are computed at
        different TIMES against different BASE TIPS. GitHub's ``mergeable`` is
        asynchronous and cached — it reports the verdict it last computed,
        against the base tip it last saw — while the content check runs now,
        against the tip in the local checkout (and its upstream, per
        ``_base_tips``). A squash landing pushed straight to ``origin/main``
        resolves the conflict without touching the PR, so the stale
        CONFLICTING survives it. That is precisely the live shape, and it is
        why ancestry cannot be used instead: the squash has no lineage back to
        the branch.
        """
        if self._pr_mergeable is None:
            return None
        try:
            info = await self._pr_mergeable(url)
        except Exception as exc:  # noqa: BLE001 — a poll error must not crash the watcher
            log.warning("failed to poll PR mergeability for %s: %s", task.id[:8], exc)
            return None
        # The poll above just awaited a network call; re-verify terminal-ness
        # before writing anything (conflict rung, SCRUM-68).
        if await self._is_terminal(task):
            return None
        mergeable = str((info or {}).get("mergeable") or "").upper()

        if mergeable == "MERGEABLE":
            ctx = task.context or {}
            if ctx.get("pr_conflict_rounds"):
                task.context = await self.store.merge_context(
                    task.id, {"pr_conflict_rounds": 0})
            return None
        if mergeable != "CONFLICTING":
            # UNKNOWN, "", or anything else GitHub hasn't settled yet: no-op,
            # no state change — see the docstring above.
            return None

        # A definite CONFLICTING, and the ONLY place the content check is paid
        # for. Cost, stated because it is a local subprocess burst on a poll
        # loop: a handful of `git rev-parse` / `merge-tree` calls, gated behind
        # a verdict that (a) GitHub reports for a small minority of ticks —
        # every other tick returns above without touching git — and (b) is
        # about to authorise an entire coder attempt if it stands. Seconds of
        # local git against millions of tokens is not a trade that needs a
        # cache; a *negative* answer is paid at most once per round, since the
        # round that follows it changes the task's status and the rung does not
        # re-run until the coder is done.
        landed = await self._complete_if_content_landed(
            task, url, forge_state=forge_state, action="shipped_pr_conflict",
            situation="PR CONFLICTING (no rebase round needed)")
        if landed == _TICK_ABORTED:
            return None
        if landed:
            return landed
        # Inconclusive or negative — including every host that never wired the
        # checker — falls through to exactly the behaviour that shipped before
        # this guard existed.

        merge_state = str((info or {}).get("mergeStateStatus") or "").upper()
        ctx = task.context or {}
        rounds = int(ctx.get("pr_conflict_rounds") or 0) + 1
        task.context = await self.store.merge_context(
            task.id, {"pr_conflict_rounds": rounds})

        if rounds > self.max_pr_conflict_rounds:
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                f"PR {url} is still CONFLICTING with main after {rounds - 1} "
                f"autonomous rebase round(s) (mergeStateStatus="
                f"{merge_state or 'UNKNOWN'}). Advise, or take over?"
            )
            data["root_cause_hypothesis"] = (
                f"PR conflicts with main: {url} "
                f"(mergeable=CONFLICTING, mergeStateStatus={merge_state or 'UNKNOWN'})"
            )
            data["evidence"] = (
                f"gh pr view --json mergeable,mergeStateStatus -> "
                f"CONFLICTING / {merge_state or 'UNKNOWN'} on "
                f"{rounds - 1} consecutive detection(s) after send-back rounds"
            )
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "escalated_pr_conflict",
                f"{task.id[:8]} PR {url} CONFLICTING past "
                f"{self.max_pr_conflict_rounds} rounds "
                f"(mergeStateStatus={merge_state or 'UNKNOWN'})",
            )
            return "escalated_pr_conflict"

        message = (
            "The PR has a textual conflict with main (mergeable=CONFLICTING"
            + (f", mergeStateStatus={merge_state}" if merge_state else "")
            + ").\nRebase onto origin/main, resolve conflicts, push — the PR "
              "updates itself."
        )
        await self.store.append_context_list(task.id, "send_back_feedback", {
            "at": now_iso(), "message": message, "author": "pr_conflict",
            "source": "pr_conflict",
        })
        task.context = await self.store.merge_context(task.id, {})
        await self._emit(
            task, "pr_conflict",
            f"{task.id[:8]} PR CONFLICTING — rebase round "
            f"{rounds}/{self.max_pr_conflict_rounds}",
        )
        return await self._resume(task)

    async def _check_pr_ci(self, task: Task, url: str) -> str | None:
        """Rung 5: react to a red check on the open PR's head, bounded."""
        if self._pr_checks is None:
            return None
        try:
            checks = await self._pr_checks(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to poll PR checks for %s: %s", task.id[:8], exc)
            return None
        # The poll above just awaited a network call; re-verify terminal-ness
        # before writing anything (CI-rounds rung, SCRUM-68).
        if await self._is_terminal(task):
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
        if self.pr_ci_policy == "advisory":
            # Operator override (Actions quota exhausted): record the red so
            # the board can show it, but never count a round, resume, or
            # escalate. Throttled once per build via its OWN key —
            # pr_ci_last_sig must stay untouched, or a build recorded under
            # advisory would read as "already acted on" forever after the
            # operator flips back to enforce (re-run failed jobs keeps the
            # same link, so only a new push would clear it).
            if ctx.get("pr_ci_advisory_sig") == signature:
                return None
            task.context = await self.store.merge_context(
                task.id, {"pr_ci_advisory_sig": signature})
            await self._emit(
                task, "pr_ci_advisory",
                f"{task.id[:8]} PR CI red — pr_ci_policy=advisory (Actions "
                "quota exhausted): recorded, not acted on",
            )
            return None
        if ctx.get("pr_ci_infra_sig") == signature:
            # This exact build was already classified platform-layer INFRA:
            # polling it again while parked must be free (no log refetch, no
            # event row) — the same invariant the pr_ci_last_sig dedup states
            # above, kept on a separate key so infra classification never
            # suppresses a later REAL failure's round-counting.
            return None
        excerpt = ""
        if self._ci_log is not None and failing[0].get("link"):
            try:
                excerpt = await self._ci_log(failing[0]["link"])
            except Exception:  # noqa: BLE001 — the log is a bonus, not a dependency
                excerpt = ""
        # The log fetch above is a network await — re-verify terminal-ness
        # before ANY write in this rung (SCRUM-68; the round counter, the
        # escalation, and the resume below all mutate the task).
        if await self._is_terminal(task):
            return None
        if excerpt and _CI_INFRA_RE.search(excerpt):
            # The run failed at the platform layer (billing / runner
            # provisioning), so it says nothing about the code: no fix round,
            # no send-back, no escalation. pr_ci_last_sig stays unset so a
            # later healthy build is evaluated fresh; pr_ci_infra_sig (its own
            # key, checked above) makes re-polling this build free. An EMPTY
            # or unreadable excerpt falls through to the real-failure path —
            # infra must be positively identified, never assumed (fail closed).
            task.context = await self.store.merge_context(
                task.id, {"pr_ci_infra_sig": signature})
            await self._emit(
                task, "pr_ci_infra",
                f"{task.id[:8]} PR CI red is billing/provisioning INFRA — "
                "no fix round counted, not escalating",
            )
            return None
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
        """Rung 6 (M6): run the CI_GATE integration validation post-PR, gated.

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
        # gate.step just triggered pipelines / posted PR comments over the
        # network; re-verify terminal-ness before acting on the outcome
        # (CI_GATE rung 6, SCRUM-68) — same race window as the other rungs.
        if await self._is_terminal(task):
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
        # The poll above just awaited a network call; re-verify terminal-ness
        # before writing anything (pr_feedback rung — the exact live incident,
        # SCRUM-68: a done task's PR got a post-merge comment and this rung
        # counted it as new human feedback and resumed the task).
        if await self._is_terminal(task):
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
        # Load-bearing terminal guard (SCRUM-68) — see _resume.
        if await self._is_terminal(task):
            return
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
        # Load-bearing terminal guard (SCRUM-68) — see _resume.
        if await self._is_terminal(task):
            return
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
