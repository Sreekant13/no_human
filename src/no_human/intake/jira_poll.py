"""Jira polling loop — mirrors the (removed) TRACKER poller's shape.

Driven from ``nh serve``'s tick alongside the scheduler (one event loop, no new
daemon — lean-stack constraint). Two best-effort halves:

  - **Poll:** run the operator's JQL, create a ``no_human`` task per NEW issue
    (deduped by ``(source="jira", external_id=<KEY>)``).
  - **Write-back (opt-in):** on a task's status change, post a work-note comment
    to its issue. A comment ONLY — never a transition or close (constraint #2).

A Jira transport error logs and is retried next tick; it never crashes the pool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..core.db import Store
from ..core.task import Task, TaskStatus

log = logging.getLogger("no_human.intake.jira_poll")

# nh statuses surfaced back to the issue, and the work-note wording. Written
# only on a *change*, and never a terminal/closing state — DONE means no_human
# finished its part (a human merges), not "close the issue".
_STATUS_NOTE: dict[TaskStatus, str] = {
    TaskStatus.IMPLEMENTING: "no_human started work on this issue (In Progress).",
    TaskStatus.REVIEWING: "no_human is running its independent staff-level review.",
    TaskStatus.AWAITING_APPROVAL: (
        "no_human opened a pull request and it is awaiting human approval. "
        "no_human never merges."
    ),
    TaskStatus.BLOCKED: "no_human is blocked on this issue and parked it with a wake condition.",
    TaskStatus.AWAITING_INPUT: "no_human needs human input to proceed on this issue.",
    TaskStatus.ESCALATED: "no_human escalated this issue — it needs a human to look.",
    TaskStatus.DONE: "no_human completed its work on this issue.",
}


@dataclass
class PollResult:
    created: int = 0
    skipped: int = 0          # already tracked (deduped)
    seen: int = 0             # total issues the JQL returned
    numbers: list[str] = field(default_factory=list)  # newly-created issue keys


class JiraPoller:
    def __init__(self, adapter, store: Store, *, config: dict | None = None, on_event=None):
        self.adapter = adapter
        self.store = store
        j = ((config or {}).get("integrations") or {}).get("jira") or {}
        self.default_repo = j.get("default_repo") or getattr(adapter, "default_repo", None)
        self.write_back = bool(j.get("write_back", False))
        self._on_event = on_event or (lambda kind, text: None)

    async def _existing_ids(self) -> set[str]:
        return {
            t.external_id
            for t in await self.store.list_tasks()
            if t.source == "jira" and t.external_id
        }

    async def poll_once(self) -> PollResult:
        result = PollResult()
        try:
            issues = self.adapter.search()
        except Exception as exc:  # noqa: BLE001 — transport error retried next tick
            log.warning("Jira poll failed: %s", exc)
            self._on_event("jira_poll_error", str(exc))
            return result

        existing = await self._existing_ids()
        for issue in issues:
            key = issue.get("key")
            if not key:
                continue
            result.seen += 1
            if key in existing:
                result.skipped += 1
                continue
            try:
                task: Task = self.adapter.normalize(issue)
            except Exception as exc:  # noqa: BLE001
                log.warning("Jira normalize %s failed: %s", key, exc)
                continue
            if self.default_repo:
                task.repo_path = self.default_repo
            await self.store.create_task(task)
            existing.add(key)
            result.created += 1
            result.numbers.append(key)
            self._on_event("jira_task_created", f"{key}: {task.title}")
        if result.created:
            self._on_event(
                "jira_poll",
                f"created {result.created} task(s) from {result.seen} issue(s)",
            )
        return result

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
        """Opt-in write-back: post a work-note on each jira task's status change,
        once per change. A comment only — never a transition/close."""
        if not self.write_back:
            return 0
        written = 0
        for task in await self.store.list_tasks():
            if task.source != "jira" or not task.external_id:
                continue
            note = _STATUS_NOTE.get(task.status)
            if not note:
                continue
            jira = (task.context or {}).get("jira") or {}
            if jira.get("nh_synced_status") == task.status.value:
                continue  # already synced this state
            if task.status == TaskStatus.AWAITING_APPROVAL:
                pr = await self._pr_url_for(task)
                if pr:
                    note = f"{note}\nPR: {pr}"
            try:
                self.adapter.comment(task.external_id, note)
            except Exception as exc:  # noqa: BLE001 — retried next tick
                log.warning("Jira comment %s failed: %s", task.external_id, exc)
                continue
            jira["nh_synced_status"] = task.status.value
            task.context = {**(task.context or {}), "jira": jira}
            await self.store.update_task(task)
            written += 1
            self._on_event("jira_status_synced", f"{task.external_id} → {task.status.value}")
        return written

    async def tick(self) -> PollResult:
        """One poll + write-back pass; errors in one half never block the other."""
        result = await self.poll_once()
        try:
            await self.sync_statuses()
        except Exception as exc:  # noqa: BLE001
            log.warning("Jira status sync pass failed: %s", exc)
        return result
