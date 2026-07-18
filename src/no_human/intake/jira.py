"""Jira Cloud issue intake — search by operator-authored JQL, normalize → Task.

The successor to the (removed) TRACKER adapter for issue-tracker intake. Config lives
in ``integrations.jira`` (site / project_key / jql / email); the API token is
``JIRA_API_TOKEN`` in ``~/.no_human/.env`` (never config, never logged).

Auth is HTTP Basic ``email:token`` (Atlassian Cloud's API-token scheme). Search
uses ``/rest/api/3/search/jql`` — the successor endpoint; the older
``/rest/api/3/search`` is on Atlassian's deprecation path.

Write-back is a **comment only** and opt-in (``integrations.jira.write_back``,
default false): the agent never transitions or closes an issue (constraint #2 —
closing/merging is a human action).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from ..core.task import Task

log = logging.getLogger("no_human.intake.jira")


def _adf_text(desc: Any) -> str:
    """Flatten a Jira description (plain string or Atlassian Document Format) to
    text. ADF is a nested ``{type, content:[...]}`` tree of ``text`` nodes."""
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc
    if not isinstance(desc, dict):
        return str(desc)
    out: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "text" and "text" in node:
            out.append(node["text"])
        for child in node.get("content") or []:
            walk(child)
        if node.get("type") in ("paragraph", "heading", "taskItem", "listItem"):
            out.append("\n")

    walk(desc)
    return "".join(out).strip()


def _text_to_adf(text: str) -> dict[str, Any]:
    """Wrap plain text as a minimal ADF document for the comment API."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _checklist_items(text: str) -> list[str]:
    """Markdown task-list checkboxes (`- [ ] ...`) as acceptance criteria."""
    return [m.strip() for m in re.findall(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$", text, re.M)]


class JiraAdapter:
    kind = "jira"

    def __init__(self, config: dict | None = None):
        j = ((config or {}).get("integrations") or {}).get("jira") or {}
        self.site = (j.get("site") or "").rstrip("/")
        self.project_key = j.get("project_key") or ""
        self.jql = j.get("jql") or ""            # operator-authored; never task text
        self.email = j.get("email") or ""
        self.default_repo = j.get("default_repo") or None
        self.write_back = bool(j.get("write_back", False))
        # Token from the process env only (loaded from ~/.no_human/.env at the
        # CLI/API boundary). Never read from config, never logged.
        self.token = os.environ.get("JIRA_API_TOKEN")

    @property
    def configured(self) -> bool:
        return bool(self.site and self.project_key and self.email and self.token)

    def _auth(self) -> tuple[str, str]:
        return (self.email, self.token or "")

    def _search_jql(self) -> str:
        # Operator-authored JQL wins; otherwise a safe default scoped to the
        # configured project (never constructed from any task's text).
        if self.jql:
            return self.jql
        return (f'project = "{self.project_key}" AND statusCategory != Done '
                "ORDER BY updated DESC")

    def search(self) -> list[dict[str, Any]]:
        """Run the configured JQL and return the matching issues (with fields)."""
        url = f"{self.site}/rest/api/3/search/jql"
        params = {
            "jql": self._search_jql(),
            "maxResults": 50,
            "fields": "summary,description,status,labels,issuetype",
        }
        r = httpx.get(url, params=params, auth=self._auth(), timeout=30.0,
                      headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json().get("issues", []) or []

    def normalize(self, issue: dict[str, Any]) -> Task:
        key = issue.get("key") or ""
        fields = issue.get("fields") or {}
        summary = fields.get("summary") or key or "Jira issue"
        description = _adf_text(fields.get("description"))
        task = Task.new(summary, source="jira", external_id=key, description=description)
        task.acceptance_criteria = _checklist_items(description)
        task.context = {
            "jira": {
                "url": f"{self.site}/browse/{key}" if key else self.site,
                "status": (fields.get("status") or {}).get("name"),
                "labels": fields.get("labels") or [],
                "issue_type": (fields.get("issuetype") or {}).get("name"),
            }
        }
        return task

    def comment(self, key: str, body: str) -> bool:
        """Post a work-note comment on an issue. Opt-in (``write_back``); a
        comment ONLY — never a status transition or close. Returns False when
        write-back is disabled (a no-op, not an error)."""
        if not self.write_back:
            return False
        url = f"{self.site}/rest/api/3/issue/{key}/comment"
        r = httpx.post(url, auth=self._auth(), json={"body": _text_to_adf(body)},
                       timeout=30.0, headers={"Accept": "application/json"})
        r.raise_for_status()
        return True
