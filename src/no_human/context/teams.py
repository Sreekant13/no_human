"""Microsoft Teams context via the M365 connector (read-only).

The gatherer depends only on a small ``CommsClient`` interface: ``search(query,
limit) -> list[dict]``. In production a Graph-backed client (config token) is
injected; in tests/demo a client wrapping data pulled via the M365 MCP connector
is injected. This keeps comms read-only and the gatherer source-agnostic.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from ..core.task import Task
from .base import ContextChunk, keywords


class CommsClient(Protocol):
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        ...


class GraphTeamsClient:
    """Production comms client: read-only Microsoft Graph search.

    Comms-IN for no_human is Teams + Outlook over the Microsoft 365 Graph (the
    notify-OUT channel is the separate Slack webhook). One client serves both
    sources; its ``entity_types`` decide what it searches — a deployment can bind
    one client to ``chatMessage`` (Teams) and another to ``message`` (Outlook),
    or share one over both (the default). The token is read-only and lives in
    config, never the repo.
    """

    GRAPH_SEARCH_URL = "https://graph.microsoft.com/v1.0/search/query"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        entity_types: tuple[str, ...] = ("chatMessage", "message"),
        transport: Any | None = None,
    ):
        cfg = (config or {}).get("context", {}).get("m365", {})
        self.token = cfg.get("token")
        self.entity_types = tuple(cfg.get("entity_types") or entity_types)
        self._transport = transport  # httpx.MockTransport in tests

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.token:
            raise RuntimeError(
                "M365 Graph token not configured (context.m365.token). Provide a "
                "read-only token, or inject an MCP-backed client."
            )
        body = {
            "requests": [{
                "entityTypes": list(self.entity_types),
                "query": {"queryString": query},
                "from": 0,
                "size": limit,
            }]
        }
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        with httpx.Client(transport=self._transport, timeout=20) as client:
            resp = client.post(self.GRAPH_SEARCH_URL, json=body, headers=headers)
        resp.raise_for_status()
        return self._parse(resp.json())

    @staticmethod
    def _parse(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten the Graph /search/query response into the gatherer's record
        shape: ``{title, subject, body, from, url}``."""
        out: list[dict[str, Any]] = []
        for req in data.get("value", []) or []:
            for container in req.get("hitsContainers", []) or []:
                for hit in container.get("hits", []) or []:
                    out.append(GraphTeamsClient._normalize(hit))
        return out

    @staticmethod
    def _normalize(hit: dict[str, Any]) -> dict[str, Any]:
        res = hit.get("resource", {}) or {}
        frm = res.get("from", {}) or {}
        sender = ((frm.get("user") or {}).get("displayName")
                  or (frm.get("emailAddress") or {}).get("address") or "")
        body = (res.get("body", {}) or {}).get("content") \
            or res.get("bodyPreview") or hit.get("summary") or ""
        return {
            "title": (res.get("subject") or sender or hit.get("summary") or "message"),
            "subject": res.get("subject", ""),
            "body": body,
            "from": sender,
            "url": res.get("webUrl") or res.get("webLink") or "",
        }


class TeamsSource:
    name = "teams"

    def __init__(self, client: CommsClient, limit: int = 5):
        self.client = client
        self.limit = limit

    async def gather(self, task: Task) -> list[ContextChunk]:
        query = self._query(task)
        if not query:
            return []
        results = self.client.search(query, limit=self.limit)
        chunks = []
        for m in results:
            who = m.get("from") or m.get("sender") or ""
            chunks.append(ContextChunk(
                source="teams",
                title=(m.get("title") or m.get("subject") or who or "message")[:120],
                content=(m.get("body") or m.get("content") or "")[:2000],
                ref=m.get("url") or m.get("webUrl") or "",
                score=1.0,
            ))
        return chunks

    def _query(self, task: Task) -> str:
        terms = keywords(task, limit=4)
        if task.external_id:
            return task.external_id
        return " ".join(terms[:3])
