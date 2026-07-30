"""Lanes for the conversational shell — the board's columns, in a terminal.

The lane a task belongs to is decided by the SERVER when the payload carries a
`lane` field, and by this module otherwise. The fallback is a port of
`routeTask` in web/src/boardLanes.js, kept semantically identical so the CLI
and the board can never disagree about where a task sits; if the two ever
diverge, the operator's mental model of "what needs me" divides in half.

Presentation (colour, empty-state icons) is local to this file. The routing
and the counts are not — they mirror the JS line for line.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

Task = Mapping[str, Any]


@dataclass(frozen=True)
class Lane:
    key: str
    label: str
    statuses: tuple[str, ...]
    #: This lane is a GATE — something the human owes. boardLanes.js `needsYou`.
    needs_you: bool = False
    #: Terminal. The board hides these behind buttons; a terminal has the rows.
    outcome: bool = False
    colour: str = "white"
    empty_hint: str = ""
    extras: dict = field(default_factory=dict)


# boardLanes.js:19-25, same keys, same order, same status membership.
LANES: tuple[Lane, ...] = (
    Lane("answer", "Needs Answer",
         ("awaiting_input", "escalated"),
         needs_you=True, colour="bright_yellow",
         empty_hint="All caught up - nothing needs your input"),
    Lane("working", "Working",
         ("pending", "context", "planning", "implementing", "reviewing",
          "testing", "compound_parent", "paused_quota"),
         colour="bright_blue", empty_hint="No tasks in flight"),
    Lane("failed", "Failed", ("failed",),
         outcome=True, colour="red", empty_hint="No failures"),
    Lane("review", "Review PR", ("awaiting_approval",),
         needs_you=True, colour="magenta",
         empty_hint="No PRs waiting for review"),
    Lane("done", "Done", ("done",),
         outcome=True, colour="green", empty_hint="Nothing shipped yet"),
)

LANE_KEYS: frozenset[str] = frozenset(lane.key for lane in LANES)
LANE_BY_KEY: dict[str, Lane] = {lane.key: lane for lane in LANES}
_NEEDS_YOU_LANES: frozenset[str] = frozenset(
    lane.key for lane in LANES if lane.needs_you)


def _status_of(task: Task | None) -> str:
    if not isinstance(task, Mapping):
        return ""
    status = task.get("status")
    return status if isinstance(status, str) else ""


def _local_lane(task: Task | None) -> str:
    """boardLanes.js `routeTask` — the fallback when the payload has no lane."""
    status = _status_of(task)
    if status == "blocked":
        # Truthiness, not "is not None": an empty wake condition is no wake
        # condition, and must route to the human like an absent one.
        return "working" if (task or {}).get("blocker_wake_condition") else "answer"
    for lane in LANES:
        if status in lane.statuses:
            return lane.key
    return "working"


def lane_for(task: Task | None) -> str:
    """The lane this task belongs to, server field first.

    A `lane` value that is not one of the five keys is ignored rather than
    rendered: a payload saying "waiting" would otherwise open a phantom column
    that claims a state the API does not report.
    """
    if isinstance(task, Mapping):
        server = task.get("lane")
        # `True` is an int in Python; a bool must never index the lane table.
        if isinstance(server, str) and not isinstance(server, bool) and server in LANE_KEYS:
            return server
    return _local_lane(task)


def is_waiting(task: Task | None) -> bool:
    """Parked on its own signal rather than actively worked (boardLanes.js:53)."""
    status = _status_of(task)
    if status == "paused_quota":
        return True
    return status == "blocked" and bool((task or {}).get("blocker_wake_condition"))


def needs_you(task: Task | None) -> bool:
    """The single definition of "this one is on the human" (boardLanes.js:66).

    Two tasks sit in a gate lane but have already had their answer: an
    approved PR waiting on the merge, and a blocker the human explicitly
    stopped. Both keep their lane and stop being counted.
    """
    if not isinstance(task, Mapping):
        return False
    if task.get("approved_at"):
        return False
    if task.get("blocker_human_stopped"):
        return False
    return lane_for(task) in _NEEDS_YOU_LANES


def needs_you_count(tasks: Iterable[Task]) -> int:
    return sum(1 for t in tasks or () if needs_you(t))


def group_by_lane(tasks: Iterable[Task]) -> dict[str, list[Task]]:
    """Every lane key, in board order, present even when empty."""
    groups: dict[str, list[Task]] = {lane.key: [] for lane in LANES}
    for task in tasks or ():
        groups[lane_for(task)].append(task)
    return groups


def flat_order(tasks: Iterable[Task]) -> list[str]:
    """Task ids top to bottom as the lanes pane draws them — the order the
    selection keys walk."""
    groups = group_by_lane(tasks)
    return [str(t.get("id")) for lane in LANES for t in groups[lane.key]]


# --------------------------------------------------------------------------- #
# Burn                                                                         #
# --------------------------------------------------------------------------- #

#: The nine buckets web/src/cost.js `totalBurn` prices: coder, reviewer and
#: aux (planning + utility), each fresh + cache-creation + cache-read.
BURN_FIELDS: tuple[str, ...] = (
    "total_tokens", "total_cache_read", "total_cache_creation",
    "total_review_tokens", "total_review_cache_read", "total_review_cache_creation",
    "total_aux_tokens", "total_aux_cache_read", "total_aux_cache_creation",
)


def task_burn(task: Task | None) -> int:
    """Every token this task cost. Showing `total_tokens` alone under-reports
    by the cache reads, which are the bulk of the burn - `nh logs` printed
    "tokens=731" for an attempt that spent four million."""
    if not isinstance(task, Mapping):
        return 0
    total = 0
    for name in BURN_FIELDS:
        try:
            total += int(task.get(name) or 0)
        except (TypeError, ValueError):
            continue
    return total


def total_burn(tasks: Iterable[Task]) -> int:
    return sum(task_burn(t) for t in tasks or ())


# --------------------------------------------------------------------------- #
# Rendering (Rich markup)                                                      #
# --------------------------------------------------------------------------- #

def _short(task: Task) -> str:
    return str(task.get("id") or "")[:8]


def _escape(text: str) -> str:
    """Rich markup is not HTML: a task title containing [bold] must render as
    text, not as a style."""
    return str(text).replace("[", r"\[")


def render_header(tasks: Iterable[Task]) -> str:
    tasks = list(tasks or ())
    pending = needs_you_count(tasks)
    burn = total_burn(tasks)
    if pending:
        gate = f"[black on bright_yellow] ! NEEDS YOU: {pending} [/]"
    else:
        gate = "[bold green]all clear[/]"
    return (f"{gate}  [dim]|[/]  {len(tasks)} tasks  [dim]|[/]  "
            f"burn [b]{burn:,}[/] tok [dim](incl. cache reads)[/]")


def _task_line(task: Task, *, selected: bool) -> str:
    marker = "[reverse]>[/reverse]" if selected else " "
    status = _status_of(task) or "unknown"
    live = task.get("live_status")
    detail = str(live) if isinstance(live, str) and live else status
    tags = []
    if task.get("claimed"):
        tags.append("[green]running[/]")
    if is_waiting(task):
        tags.append("[dim]waits for its own signal[/]")
    if task.get("approved_at"):
        tags.append("[dim]approved - merge pending[/]")
    if task.get("blocker_human_stopped"):
        tags.append("[dim]you stopped it[/]")
    if task.get("subtask_progress"):
        tags.append(f"[dim]{_escape(task['subtask_progress'])}[/]")
    burn = task_burn(task)
    if burn:
        tags.append(f"[dim]{burn:,} tok[/]")
    suffix = ("  " + " ".join(tags)) if tags else ""
    title = _escape(task.get("title") or "(untitled)")
    if selected:
        title = f"[reverse]{title}[/reverse]"
    return f" {marker} [dim]{_short(task)}[/] {title}  [dim]{_escape(detail)}[/]{suffix}"


def render_lanes(tasks: Iterable[Task], selected_id: str | None = None) -> str:
    """The board's columns as stacked sections, top to bottom in board order.

    The gate lanes are the point of the whole surface, so they get a filled
    header bar; the rest get a plain one.
    """
    tasks = list(tasks or ())
    groups = group_by_lane(tasks)
    lines: list[str] = []
    for lane in LANES:
        rows = groups[lane.key]
        live = sum(1 for t in rows if needs_you(t)) if lane.needs_you else len(rows)
        if lane.needs_you:
            head = (f"[black on {lane.colour}] ! {lane.label}  {live} [/]"
                    if live else f"[{lane.colour}]{lane.label}  0[/]")
        else:
            head = f"[bold {lane.colour}]{lane.label}[/] [dim]{len(rows)}[/]"
        lines.append(head)
        if not rows:
            lines.append(f"   [dim]{lane.empty_hint}[/]")
        for task in rows:
            lines.append(_task_line(task, selected=str(task.get("id")) == selected_id))
        lines.append("")
    return "\n".join(lines).rstrip()
