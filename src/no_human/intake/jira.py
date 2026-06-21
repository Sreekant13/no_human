"""Minimal Jira issue intake (REST v2). Token from config; stub until set."""

from __future__ import annotations

from typing import Any

import httpx

from ..core.task import Task


class JiraAdapter:
    kind = "jira"

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = (config or {}).get("intake", {}).get("jira", {})
        self.base_url = cfg.get("url")
        self.email = cfg.get("email")
        self.token = cfg.get("token")

    def fetch_raw(self, ref: str) -> dict[str, Any]:
        if not (self.base_url and self.token):
            raise RuntimeError("Jira not configured (intake.jira.url/email/token).")
        key = ref.rsplit("/", 1)[-1]
        resp = httpx.get(
            f"{self.base_url}/rest/api/2/issue/{key}",
            auth=(self.email, self.token) if self.email else None,
            headers=None if self.email else {"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def normalize(self, raw: dict[str, Any]) -> Task:
        fields = raw.get("fields", {})
        title = fields.get("summary", "") or raw.get("key", "")
        task = Task.new(title, source="jira", external_id=raw.get("key"),
                        description=fields.get("description") or "")
        task.priority = (fields.get("priority") or {}).get("name", "medium").lower()
        task.context = {"jira": {"status": (fields.get("status") or {}).get("name")}}
        return task
