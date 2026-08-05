"""monday.com polling loop — mirrors ``intake/linear_poll.py``'s shape exactly.

Driven from ``nh serve``/``nh start``'s tick alongside the scheduler (one event
loop, no new daemon — lean-stack constraint). Two best-effort halves:

  - **Poll:** pull the operator's board, filtered to the configured
    ``todo_labels``, and create a ``no_human`` task per NEW item (deduped by
    ``(source="monday", external_id=<item id>)``).
  - **Write-back (opt-in):** as a task advances, post an update to its item AND
    move it to the operator's configured label — ``in_progress_label`` on first
    claim, ``done_label`` on completion. Escalated/failed/blocked tasks are
    commented but never transitioned (status must never lie).

A monday transport error logs and is retried next tick; it never crashes the
pool. Throttling is called out by name in the log because monday answers it as
HTTP 429 with an **HTML** body — a client that only reported the parse failure
would say "non-JSON response" and hide a plainly retryable condition.

A **config** error is the one exception to "log and retry": it is reported
through its own event kind, because no number of ticks fixes an unset
``board_id``, and a poller that quietly retried forever is exactly the silent
failure the adapter's :class:`~no_human.intake.monday.MondayConfigError` exists
to prevent.

WHY POLLING AND NOT WEBHOOKS
----------------------------
Same answer as Linear, for the same reason. monday does offer webhooks, and
they are better for latency — but they need a publicly reachable HTTPS
endpoint, and no_human is a local single-operator tool bound to 127.0.0.1. A
webhook receiver would mean inbound exposure this product does not otherwise
have. Polling one board costs a handful of requests per tick.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..core.db import Store
from ..core.task import Task, TaskStatus
from ..profile import apply_default_task_config
from .monday import MondayConfigError, MondayRateLimited

log = logging.getLogger("no_human.intake.monday_poll")

# nh statuses surfaced back to the item, and the work-note wording. Written
# only on a *change*, and never a closing action — DONE means no_human finished
# its part (a human merges), not "close the item".
_STATUS_NOTE: dict[TaskStatus, str] = {
    TaskStatus.IMPLEMENTING: "no_human started work on this item.",
    TaskStatus.REVIEWING: "no_human is running its independent staff-level review.",
    TaskStatus.AWAITING_APPROVAL: (
        "no_human opened a pull request and it is awaiting human approval. "
        "no_human never merges."
    ),
    TaskStatus.BLOCKED: "no_human is blocked on this item and parked it with a wake condition.",
    TaskStatus.AWAITING_INPUT: "no_human needs human input to proceed on this item.",
    TaskStatus.ESCALATED: "no_human escalated this item — it needs a human to look.",
    TaskStatus.FAILED: "no_human's attempts on this item failed — it needs a human to look.",
    TaskStatus.DONE: "no_human completed its work on this item.",
}

# Off-ramp "needs a human" statuses: commented, never transitioned. Before
# posting one of these notes, sync_statuses() checks whether a human already
# moved the item to the done label — if so, the note would lie.
_HUMAN_ATTENTION: set[TaskStatus] = {
    TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
    TaskStatus.ESCALATED, TaskStatus.FAILED,
}

# Status -> target state MEANING (never a monday label — labels are per-board
# and live in the operator's config; the adapter resolves them). Off-ramps
# (blocked, awaiting_input, escalated, failed, paused_quota) are deliberately
# absent: they comment but never transition.
_TARGET_MEANING: dict[TaskStatus, str] = {
    TaskStatus.CONTEXT: "in_progress",
    TaskStatus.PLANNING: "in_progress",
    TaskStatus.IMPLEMENTING: "in_progress",
    TaskStatus.REVIEWING: "in_progress",
    TaskStatus.TESTING: "in_progress",
    TaskStatus.AWAITING_APPROVAL: "in_progress",
    TaskStatus.DONE: "done",
}


@dataclass
class PollResult:
    created: int = 0
    skipped: int = 0          # already tracked (deduped)
    seen: int = 0             # total items the filter returned
    numbers: list[str] = field(default_factory=list)  # newly-created item ids


class MondayPoller:
    def __init__(self, adapter, store: Store, *, config: dict | None = None, on_event=None):
        self.adapter = adapter
        self.store = store
        cfg = ((config or {}).get("integrations") or {}).get("monday") or {}
        self.default_repo = cfg.get("default_repo") or getattr(adapter, "default_repo", None)
        self.write_back = bool(cfg.get("write_back", False))
        self._on_event = on_event or (lambda kind, text: None)

    async def _existing_ids(self) -> set[str]:
        return {
            t.external_id
            for t in await self.store.list_tasks()
            if t.source == "monday" and t.external_id
        }

    def _log_transport(self, what: str, exc: Exception) -> str:
        """One place that names throttling and misconfiguration explicitly.

        monday reports rate limiting as HTTP 429 with an HTML body, so a log
        line that only said "non-JSON response" would read as a broken API
        rather than a retryable throttle. A config error is named separately
        because it is the one failure ticking again will never fix.
        """
        if isinstance(exc, MondayRateLimited):
            detail = ("rate limited (monday reports this as HTTP 429 with an HTML "
                      "body, not JSON) — retrying next tick")
        elif isinstance(exc, MondayConfigError):
            detail = f"not configured: {exc}"
        else:
            detail = f"{type(exc).__name__}: {exc}"
        log.warning("monday %s failed: %s", what, detail)
        return detail

    def _event_kind(self, base: str, exc: Exception) -> str:
        """A config error gets its own event kind so the UI/console can say
        "fix your config", not "the network flaked"."""
        return "monday_config_error" if isinstance(exc, MondayConfigError) else base

    async def poll_once(self) -> PollResult:
        result = PollResult()
        try:
            items = await asyncio.to_thread(self.adapter.search)
        except Exception as exc:  # noqa: BLE001 — transport error retried next tick
            self._on_event(self._event_kind("monday_poll_error", exc),
                           self._log_transport("poll", exc))
            return result

        existing = await self._existing_ids()
        for item in items:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            result.seen += 1
            if item_id in existing:
                result.skipped += 1
                continue
            try:
                task: Task = self.adapter.normalize(item)
            except Exception as exc:  # noqa: BLE001
                log.warning("monday normalize %s failed: %s", item_id, exc)
                continue
            if self.default_repo:
                task.repo_path = self.default_repo
            if task.repo_path:
                profile = await self.store.get_profile(task.repo_path)
                task.config = apply_default_task_config(profile, task.config)
            await self.store.create_task(task)
            existing.add(item_id)
            result.created += 1
            result.numbers.append(item_id)
            self._on_event("monday_task_created", f"{item_id}: {task.title}")
        if result.created:
            self._on_event(
                "monday_poll",
                f"created {result.created} task(s) from {result.seen} item(s)",
            )
        return result

    async def _item_already_done(self, item_id: str) -> bool:
        """True only if the item is CURRENTLY sitting on the operator's
        ``done_label``.

        This is where monday differs from Linear once more: Linear can ask the
        API for a typed ``completed``, but "done" here is whatever label the
        operator named. With no ``done_label`` configured there is no such
        thing as done on this board, so the answer is False and the note is
        posted. A fetch error also degrades to False (proceed to comment) and
        leaves the marker unset so the next tick retries.
        """
        done_label = getattr(self.adapter, "done_label", "") or ""
        if not done_label:
            return False
        try:
            current = await asyncio.to_thread(self.adapter.item_status, item_id)
        except Exception as exc:  # noqa: BLE001 — transient; retry next tick
            self._log_transport("status check", exc)
            return False
        return current == done_label

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
        """Opt-in write-back: as each monday task advances, move its item to the
        operator's configured label and post a work-note update, once per
        change. These are independent idempotency markers — a transition
        no-op/failure never suppresses the comment, and vice versa.
        Escalated/failed/blocked tasks are commented but never transitioned."""
        if not self.write_back:
            return 0
        written = 0
        newest_by_key: dict[str, Task] = {}
        for task in await self.store.list_tasks():
            if task.source != "monday" or not task.external_id:
                continue
            cur = newest_by_key.get(task.external_id)
            if cur is None or (task.created_at, task.id) > (cur.created_at, cur.id):
                newest_by_key[task.external_id] = task
        for task in newest_by_key.values():
            monday = (task.context or {}).get("monday") or {}
            # Every mutation needs the item id. Without it we cannot write, and
            # guessing would be a fabricated call — skip loudly instead.
            item_id = monday.get("id")
            if not item_id:
                log.warning(
                    "monday %s: no item id on the task context — cannot write back",
                    task.external_id)
                continue
            changed = False

            # --- transition: once per target label, independent of comment ---
            done_meanings = monday.get("nh_monday_transitions") or []
            target = _TARGET_MEANING.get(task.status)
            if target and target not in done_meanings:
                try:
                    await asyncio.to_thread(self.adapter.transition, item_id, target)
                except Exception as exc:  # noqa: BLE001 — unset marker -> retry next tick
                    self._log_transport(f"transition {task.external_id}", exc)
                else:
                    # Marker set on True OR no-label(False) alike — both are a
                    # handled outcome; only a raised exception should retry.
                    monday["nh_monday_transitions"] = [*done_meanings, target]
                    changed = True
                    self._on_event("monday_transitioned", f"{task.external_id} → {target}")

            # --- comment ---
            note = _STATUS_NOTE.get(task.status)
            if note and monday.get("nh_synced_status") != task.status.value:
                if (task.status in _HUMAN_ATTENTION
                        and await self._item_already_done(item_id)):
                    # A human already moved this item to the done label; the
                    # local failed/escalated/blocked row is real but a "needs a
                    # human" note would lie. Skip + mark.
                    monday["nh_synced_status"] = task.status.value
                    changed = True
                    self._on_event(
                        "monday_status_synced",
                        f"{task.external_id} already done in monday → skip note")
                else:
                    if task.status in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE):
                        pr = await self._pr_url_for(task)
                        if pr:
                            note = f"{note}\nPR: {pr}"
                    try:
                        await asyncio.to_thread(self.adapter.comment, item_id, note)
                    except Exception as exc:  # noqa: BLE001
                        self._log_transport(f"comment {task.external_id}", exc)
                    else:
                        monday["nh_synced_status"] = task.status.value
                        changed = True
                        self._on_event("monday_status_synced",
                                       f"{task.external_id} → {task.status.value}")

            if changed:
                task.context = {**(task.context or {}), "monday": monday}
                await self.store.update_task(task)
                written += 1
        return written

    async def tick(self) -> PollResult:
        """One poll + write-back pass; errors in one half never block the other."""
        result = await self.poll_once()
        try:
            await self.sync_statuses()
        except Exception as exc:  # noqa: BLE001
            log.warning("monday status sync pass failed: %s", exc)
        return result
