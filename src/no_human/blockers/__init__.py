"""Blocker handling (PLAN.md Part 22): taxonomy, structured escalation, and the
wake-condition watcher."""

from .report import (
    blocker_prompt_suffix,
    fallback_blocker,
    missing_access,
    notification_line,
    parse_blocker,
    render_report,
)
from .taxonomy import Blocker, BlockerCategory, Route, route_for, triage
from .wake import WakeWatcher, parse_duration

__all__ = [
    "Blocker",
    "BlockerCategory",
    "Route",
    "route_for",
    "triage",
    "parse_blocker",
    "render_report",
    "notification_line",
    "fallback_blocker",
    "missing_access",
    "blocker_prompt_suffix",
    "WakeWatcher",
    "parse_duration",
]
