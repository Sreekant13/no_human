"""Blocker handling (PLAN.md Part 22): taxonomy, structured escalation, and the
wake-condition watcher."""

from .answers import (
    answer_record,
    find_stored_answer,
    normalize_question,
    question_hash,
    reuse_record,
)
from .report import (
    blocker_prompt_suffix,
    ci_misconfigured,
    fallback_blocker,
    missing_access,
    notification_line,
    parse_blocker,
    render_report,
)
from .actions import (
    ActionError,
    apply_action,
    is_plan_approval_action,
    is_terminal_action,
)
from .taxonomy import (
    Blocker,
    BlockerCategory,
    BlockerOption,
    Route,
    resume_checkpoint,
    resume_provenance,
    route_for,
    triage,
)
from .wake import WakeWatcher, parse_duration

__all__ = [
    "ActionError",
    "apply_action",
    "is_plan_approval_action",
    "is_terminal_action",
    "answer_record",
    "find_stored_answer",
    "normalize_question",
    "question_hash",
    "reuse_record",
    "Blocker",
    "BlockerCategory",
    "BlockerOption",
    "Route",
    "resume_checkpoint",
    "resume_provenance",
    "route_for",
    "triage",
    "parse_blocker",
    "render_report",
    "notification_line",
    "fallback_blocker",
    "missing_access",
    "ci_misconfigured",
    "blocker_prompt_suffix",
    "WakeWatcher",
    "parse_duration",
]
