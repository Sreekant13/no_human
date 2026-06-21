"""Context gathering: parallel, read-only, binary completeness check."""

from __future__ import annotations

from ..core.db import Store
from .base import (
    CompletenessReport,
    ContextChunk,
    ContextSource,
    TaskContext,
    check_completeness,
    keywords,
)
from .codebase import CodebaseSource
from .gatherer import ContextGatherer
from .outlook import OutlookSource
from .sessions import SessionsSource
from .teams import CommsClient, GraphTeamsClient, TeamsSource

__all__ = [
    "ContextGatherer", "TaskContext", "ContextChunk", "ContextSource",
    "CompletenessReport", "check_completeness", "keywords",
    "CodebaseSource", "SessionsSource", "TeamsSource", "OutlookSource",
    "CommsClient", "GraphTeamsClient", "build_default_sources",
]


def build_default_sources(store: Store, config: dict | None = None,
                          comms_client: CommsClient | None = None) -> list[ContextSource]:
    """Assemble the standard context sources. Comms sources only when a client
    is available (read-only); codebase + sessions always run."""
    sources: list[ContextSource] = [CodebaseSource(), SessionsSource(store)]
    if comms_client is not None:
        sources.append(TeamsSource(comms_client))
        sources.append(OutlookSource(comms_client))
    return sources
