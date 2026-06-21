"""Intake: classify a URL/id and ingest it into a Task."""

from __future__ import annotations

from typing import Any

from ..core.task import Task
from .base import SourceRef, parse_source
from .tracker import TrackerAdapter
from .github_issues import GitHubAdapter
from .gitlab_issues import GitLabAdapter
from .jira import JiraAdapter

__all__ = ["SourceRef", "parse_source", "get_adapter", "ingest_from_url"]


def get_adapter(kind: str, config: dict[str, Any] | None = None):
    if kind == "tracker":
        return TrackerAdapter(config)
    if kind == "github":
        return GitHubAdapter()
    if kind == "gitlab":
        return GitLabAdapter()
    if kind == "jira":
        return JiraAdapter(config)
    raise ValueError(f"no intake adapter for source: {kind}")


def ingest_from_url(text: str, config: dict[str, Any] | None = None) -> Task:
    """Detect the source, fetch the record, return a normalized Task."""
    ref = parse_source(text)
    if ref.kind == "freeform":
        raise ValueError(f"not a recognized task URL/id: {text!r}")
    adapter = get_adapter(ref.kind, config)
    raw = adapter.fetch_raw(ref.ref)
    return adapter.normalize(raw)
