"""Intake: classify a URL/id and ingest it into a Task."""

from __future__ import annotations

from typing import Any

from ..core.task import Task
from .base import SourceRef, parse_source
from .classify import (
    KindVerdict,
    TaskKind,
    classify,
    classify_kind,
    kind_criteria_mismatch,
)
from .grill import GrillQuestion, GrillResult, grill_step
from .github_issues import GitHubAdapter
from .gitlab_issues import GitLabAdapter

__all__ = [
    "SourceRef", "parse_source", "get_adapter", "ingest_from_url",
    "TaskKind", "KindVerdict", "classify", "classify_kind",
    "kind_criteria_mismatch",
    "GrillQuestion", "GrillResult", "grill_step",
]


def get_adapter(kind: str, config: dict[str, Any] | None = None):
    if kind == "github":
        return GitHubAdapter()
    if kind == "gitlab":
        return GitLabAdapter()
    raise ValueError(f"no intake adapter for source: {kind}")


def ingest_from_url(text: str, config: dict[str, Any] | None = None) -> Task:
    """Detect the source, fetch the record, return a normalized Task."""
    ref = parse_source(text)
    if ref.kind == "freeform":
        # The message names the next action. It used to name only the input,
        # which is exactly no help to the person hitting it: the docs told
        # users to pass a bare ticket key for months after the tracker adapter
        # was removed, so the single most likely reader of this line is
        # someone who did what the documentation said.
        raise ValueError(
            f"not a recognized task URL/id: {text!r}\n"
            "Intake accepts a GitHub/GitLab issue URL (one containing /issues/ "
            "or /-/issues/).\n"
            "A bare ticket key like PROJ-42 is not a source — the standalone "
            "tracker adapter was removed.\n"
            "  file it directly:  nh task add --title \"...\" --repo <path> "
            "--criteria \"...\"\n"
            "  from Jira:         configure integrations.jira and let `nh serve` "
            "poll it (docs/adapters.md#jira)"
        )
    adapter = get_adapter(ref.kind, config)
    raw = adapter.fetch_raw(ref.ref)
    return adapter.normalize(raw)
