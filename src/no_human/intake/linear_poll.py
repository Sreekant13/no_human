"""Linear polling loop — mirrors ``intake/jira_poll.py``'s shape exactly.

Driven from ``nh serve``/``nh start``'s tick alongside the scheduler (one event
loop, no new daemon — lean-stack constraint). Two best-effort halves:

  - **Poll:** run the operator's team/state filter, create a ``no_human`` task
    per NEW issue (deduped by ``(source="linear", external_id=<IDENTIFIER>)``).
  - **Write-back (opt-in):** as a task advances, post a work-note comment to
    its issue AND move it into the team's matching workflow-state *type* —
    ``started`` on first claim, ``completed`` on completion. Escalated/failed/
    blocked tasks are commented but never transitioned (state must never lie),
    and the agent never moves an issue to ``canceled``/``duplicate``.

A Linear transport error logs and is retried next tick; it never crashes the
pool. Throttling is called out by name in the log, because Linear reports it as
HTTP 400 rather than 429 and would otherwise read as a permanent client error.

WHY POLLING AND NOT WEBHOOKS
----------------------------
Linear does offer webhooks with HMAC-SHA256 ``Linear-Signature`` verification,
and they are strictly better for latency and for seeing deletions and
before/after transition values. They also require a publicly reachable HTTPS
endpoint. no_human is a local single-operator tool bound to 127.0.0.1, so a
webhook receiver would mean inbound exposure this product does not otherwise
have. Polling costs ~60 requests/hour at the 60s floor against a documented
2,500/hour personal-key budget, so the cheap option is also the safe one. The
filter uses ``updatedAt``/``orderBy: updatedAt``, so moving to webhooks later
does not change the adapter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..core.db import Store
from ..core.task import Task, TaskStatus
from ..profile import apply_default_task_config
from .linear import LinearRateLimited

log = logging.getLogger("no_human.intake.linear_poll")

# nh statuses surfaced back to the issue, and the work-note wording. Written
# only on a *change*, and never a terminal/closing state — DONE means no_human
# finished its part (a human merges), not "close the issue".
_STATUS_NOTE: dict[TaskStatus, str] = {
    TaskStatus.IMPLEMENTING: "no_human started work on this issue.",
    TaskStatus.REVIEWING: "no_human is running its independent staff-level review.",
    TaskStatus.AWAITING_APPROVAL: (
        "no_human opened a pull request and it is awaiting human approval. "
        "no_human never merges."
    ),
    TaskStatus.BLOCKED: "no_human is blocked on this issue and parked it with a wake condition.",
    TaskStatus.AWAITING_INPUT: "no_human needs human input to proceed on this issue.",
    TaskStatus.ESCALATED: "no_human escalated this issue — it needs a human to look.",
    TaskStatus.FAILED: "no_human's attempts on this issue failed — it needs a human to look.",
    TaskStatus.DONE: "no_human completed its work on this issue.",
}

# Off-ramp "needs a human" statuses: commented, never transitioned. Before
# posting one of these notes, sync_statuses() checks whether a human already
# completed the issue in Linear — if so, the note would lie.
_HUMAN_ATTENTION: set[TaskStatus] = {
    TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
    TaskStatus.ESCALATED, TaskStatus.FAILED,
}

# Status -> target Linear workflow-state TYPE (never a hardcoded state UUID —
# state ids are per-team). Off-ramps (blocked, awaiting_input, escalated,
# failed, paused_quota) are deliberately absent: they comment but never
# transition. `canceled`/`duplicate` never appear here and are refused by the
# adapter regardless.
_TARGET_STATE: dict[TaskStatus, str] = {
    TaskStatus.CONTEXT: "started",
    TaskStatus.PLANNING: "started",
    TaskStatus.IMPLEMENTING: "started",
    TaskStatus.REVIEWING: "started",
    TaskStatus.TESTING: "started",
    TaskStatus.AWAITING_APPROVAL: "started",
    TaskStatus.DONE: "completed",
}


@dataclass
class PollResult:
    created: int = 0
    skipped: int = 0          # already tracked (deduped)
    seen: int = 0             # total issues the filter returned
    numbers: list[str] = field(default_factory=list)  # newly-created identifiers


class LinearPoller:
    def __init__(self, adapter, store: Store, *, config: dict | None = None, on_event=None):
        self.adapter = adapter
        self.store = store
        cfg = ((config or {}).get("integrations") or {}).get("linear") or {}
        self.default_repo = cfg.get("default_repo") or getattr(adapter, "default_repo", None)
        self.write_back = bool(cfg.get("write_back", False))
        self._on_event = on_event or (lambda kind, text: None)

    async def _existing_ids(self) -> set[str]:
        return {
            t.external_id
            for t in await self.store.list_tasks()
            if t.source == "linear" and t.external_id
        }

    def _log_transport(self, what: str, exc: Exception) -> str:
        """One place that names throttling explicitly. Linear reports rate
        limiting as HTTP 400 + ``extensions.code == "RATELIMITED"``, so a log
        line that only said "HTTP 400" would read as a permanent bad request."""
        if isinstance(exc, LinearRateLimited):
            detail = "rate limited (Linear reports this as HTTP 400, not 429) — retrying next tick"
        else:
            detail = f"{type(exc).__name__}: {exc}"
        log.warning("Linear %s failed: %s", what, detail)
        return detail

    async def poll_once(self) -> PollResult:
        result = PollResult()
        try:
            issues = await asyncio.to_thread(self.adapter.search)
        except Exception as exc:  # noqa: BLE001 — transport error retried next tick
            self._on_event("linear_poll_error", self._log_transport("poll", exc))
            return result

        existing = await self._existing_ids()
        for issue in issues:
            identifier = issue.get("identifier")
            if not identifier:
                continue
            result.seen += 1
            if identifier in existing:
                result.skipped += 1
                continue
            try:
                task: Task = self.adapter.normalize(issue)
            except Exception as exc:  # noqa: BLE001
                log.warning("Linear normalize %s failed: %s", identifier, exc)
                continue
            if self.default_repo:
                task.repo_path = self.default_repo
            if task.repo_path:
                profile = await self.store.get_profile(task.repo_path)
                task.config = apply_default_task_config(profile, task.config)
            await self.store.create_task(task)
            existing.add(identifier)
            result.created += 1
            result.numbers.append(identifier)
            self._on_event("linear_task_created", f"{identifier}: {task.title}")
        if result.created:
            self._on_event(
                "linear_poll",
                f"created {result.created} task(s) from {result.seen} issue(s)",
            )
        return result

    async def _issue_already_complete(self, node_id: str) -> bool:
        """True only if the issue's current Linear state type is a closed one.
        A fetch error degrades to False (proceed to comment) and leaves the
        marker unset so the next tick retries."""
        try:
            state_type = await asyncio.to_thread(self.adapter.state_type, node_id)
        except Exception as exc:  # noqa: BLE001 — transient; retry next tick
            self._log_transport("state check", exc)
            return False
        return state_type in ("completed", "canceled", "duplicate")

    async def _pr_url_for(self, task: Task) -> str:
        try:
            attempts = await self.store.list_attempts(task.id)
        except Exception:  # noqa: BLE001
            return ""
        for a in reversed(attempts):
            if a.get("pr_url"):
                return a["pr_url"]
        return ""

    async def sync_statuses(self) -> int:
        """Opt-in write-back: as each linear task advances, move its issue into
        the matching workflow-state type and post a work-note comment, once per
        change. These are independent idempotency markers — a transition
        no-op/failure never suppresses the comment, and vice versa.
        Escalated/failed/blocked tasks are commented but never transitioned."""
        if not self.write_back:
            return 0
        written = 0
        newest_by_key: dict[str, Task] = {}
        for task in await self.store.list_tasks():
            if task.source != "linear" or not task.external_id:
                continue
            cur = newest_by_key.get(task.external_id)
            if cur is None or (task.created_at, task.id) > (cur.created_at, cur.id):
                newest_by_key[task.external_id] = task
        for task in newest_by_key.values():
            linear = (task.context or {}).get("linear") or {}
            # Every mutation needs the API's UUID, not the "ENG-123"
            # identifier. Without it we cannot write, and guessing would be a
            # fabricated call — skip loudly instead.
            node_id = linear.get("id")
            if not node_id:
                log.warning(
                    "Linear %s: no node id on the task context — cannot write back",
                    task.external_id)
                continue
            changed = False

            # --- transition: once per target state, independent of comment ---
            done_states = linear.get("nh_linear_transitions") or []
            target = _TARGET_STATE.get(task.status)
            if target and target not in done_states:
                try:
                    await asyncio.to_thread(self.adapter.transition, node_id, target)
                except Exception as exc:  # noqa: BLE001 — unset marker -> retry next tick
                    self._log_transport(f"transition {task.external_id}", exc)
                else:
                    # Marker set on True OR no-match(False) alike — both are a
                    # handled outcome; only a raised exception should retry.
                    linear["nh_linear_transitions"] = [*done_states, target]
                    changed = True
                    self._on_event("linear_transitioned", f"{task.external_id} → {target}")

            # --- comment ---
            note = _STATUS_NOTE.get(task.status)
            if note and linear.get("nh_synced_status") != task.status.value:
                if (task.status in _HUMAN_ATTENTION
                        and await self._issue_already_complete(node_id)):
                    # A human already closed this issue in Linear; the local
                    # failed/escalated/blocked row is real but a "needs a
                    # human" note would lie. Skip + mark.
                    linear["nh_synced_status"] = task.status.value
                    changed = True
                    self._on_event(
                        "linear_status_synced",
                        f"{task.external_id} already closed in Linear → skip note")
                else:
                    if task.status in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE):
                        pr = await self._pr_url_for(task)
                        if pr:
                            note = f"{note}\nPR: {pr}"
                    try:
                        await asyncio.to_thread(self.adapter.comment, node_id, note)
                    except Exception as exc:  # noqa: BLE001
                        self._log_transport(f"comment {task.external_id}", exc)
                    else:
                        linear["nh_synced_status"] = task.status.value
                        changed = True
                        self._on_event("linear_status_synced",
                                       f"{task.external_id} → {task.status.value}")

            if changed:
                task.context = {**(task.context or {}), "linear": linear}
                await self.store.update_task(task)
                written += 1
        return written

    async def tick(self) -> PollResult:
        """One poll + write-back pass; errors in one half never block the other."""
        result = await self.poll_once()
        try:
            await self.sync_statuses()
        except Exception as exc:  # noqa: BLE001
            log.warning("Linear status sync pass failed: %s", exc)
        return result
