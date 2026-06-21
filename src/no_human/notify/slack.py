"""Slack notifications via a write-only webhook (PLAN.md 4.2 read/write split).

This token *only* posts to the alert channel — it never reads. No token both
reads context and posts. If no webhook is configured, notifications are logged
instead of sent, so Phase 0 runs without Slack set up.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("no_human.notify")

# Events the notifier understands (PLAN.md Part 5).
EVENTS = frozenset(
    {"task_complete", "task_failed", "needs_approval", "stuck", "paused_quota"}
)


class SlackNotifier:
    def __init__(self, webhook_url: str | None):
        self.webhook_url = webhook_url

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def notify(self, event: str, message: str) -> bool:
        """Post a message. Returns True if sent, False if logged-only.

        Failures are swallowed (logged) — a notification problem must never
        crash the orchestrator or fail a task.
        """
        line = f"[{event}] {message}"
        if not self.enabled:
            log.info("slack(disabled): %s", line)
            return False
        try:
            resp = httpx.post(self.webhook_url, json={"text": line}, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 — notifications are best-effort
            log.warning("slack notify failed: %s", exc)
            return False
