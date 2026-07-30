"""Which board lane a task belongs in - the single source of truth.

This decision lived only in ``web/src/boardLanes.js``. Anything else that wanted
a lane had to reimplement it, and this repo already shipped that failure once
(PR-007: the counts were right and the lane LABEL lied). The board endpoint now
computes the lane here and ships it on the task payload; the JS keeps the
PRESENTATION half of ``LANES`` (labels, order, colours) and prefers the served
value, falling back to its own copy only when the field is absent.

``tests/test_lane_conformance.py`` and ``web/src/laneConformance.test.mjs`` run
the same fixture file (``testdata/lane_conformance.json``) through both
implementations, so they cannot drift apart without a suite going red.

Input shape: the FLATTENED board payload - a mapping or an object with
``status`` and ``blocker_wake_condition`` attributes (``TaskSummaryOut``). A raw
``core.task.Task`` keeps its wake condition nested under ``task.blocker``, which
this module does not read.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Status -> lane, in the order routing consults it. Mirrors the `statuses`
# arrays in web/src/boardLanes.js LANES; the labels/colours/order-on-screen stay
# in the JS because they are presentation.
LANE_STATUSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("answer", ("awaiting_input", "escalated")),
    (
        "working",
        (
            "pending",
            "context",
            "planning",
            "implementing",
            "reviewing",
            "testing",
            "compound_parent",
            "paused_quota",
        ),
    ),
    ("failed", ("failed",)),
    ("review", ("awaiting_approval",)),
    ("done", ("done",)),
)

LANE_KEYS: tuple[str, ...] = tuple(key for key, _ in LANE_STATUSES)

# An unrecognised status lands in Working rather than vanishing off the board.
FALLBACK_LANE = "working"

_BLOCKED = "blocked"
_PAUSED_QUOTA = "paused_quota"


def _field(task: Any, name: str) -> Any:
    if task is None:
        return None
    if isinstance(task, Mapping):
        return task.get(name)
    return getattr(task, name, None)


def _status_of(task: Any) -> str:
    raw = _field(task, "status")
    raw = getattr(raw, "value", raw)  # TaskStatus -> its string value
    return raw if isinstance(raw, str) else ""


def _wake_condition(task: Any) -> Any:
    return _field(task, "blocker_wake_condition")


def lane_for(task: Any) -> str:
    """Return the lane key for ``task``.

    ``blocked`` routes dynamically: WITH a wake condition it self-resolves and
    sits in Working (parked, see :func:`is_waiting`); WITHOUT one a human must
    act, so it goes to Needs Answer. Every other status routes off
    :data:`LANE_STATUSES`, and anything unrecognised falls back to Working.
    """
    status = _status_of(task)
    if status == _BLOCKED:
        return "working" if _wake_condition(task) else "answer"
    for key, statuses in LANE_STATUSES:
        if status in statuses:
            return key
    return FALLBACK_LANE


def is_waiting(task: Any) -> bool:
    """True when the task is parked on a signal it resolves by itself.

    Distinct from the lane: these sit in Working, but they are not live work,
    so the card says so instead of looking in-flight.
    """
    status = _status_of(task)
    if status == _PAUSED_QUOTA:
        return True
    return status == _BLOCKED and bool(_wake_condition(task))
