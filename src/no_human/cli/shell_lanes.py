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

import textwrap
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


def is_real_failure(task: Task | None) -> bool:
    """boardLanes.js `isRealFailure` — a cancelled task ends `failed` but is
    not a capability failure: it stays listed in the Failed lane (tagged
    "cancelled" by `_task_line`) but drops out of the lane's header count,
    the same split `nh status` and the board's Outcomes table already make.
    """
    if not isinstance(task, Mapping):
        return False
    return _status_of(task) == "failed" and not task.get("cancelled")


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


#: Cells before the title column: " " + marker + " " + 8-char id + "  ".
#: Continuation lines of a wrapped title and the meta line both start here,
#: so a row reads as one block under its title rather than bleeding back to
#: column 0 where it looks like a new task.
_TITLE_COLUMN = 13


def _status_style(task: Task) -> str:
    """The board's state vocabulary, in the terminal's four colours: green
    while an agent holds it, yellow while it waits on its own signal, red
    when it failed, dim for everything else (pending, done, in review...)."""
    if task.get("claimed"):
        return "green"
    if is_waiting(task):
        return "yellow"
    if _status_of(task) == "failed":
        return "red"
    return "dim"


def _wrap_title(title: str, width: int | None) -> list[str]:
    """Split the RAW title into chunks that fit the title column. Escaping
    happens per chunk afterwards, so a literal "[" costs one cell in the
    arithmetic here and one cell on screen."""
    if width is None:
        return [title]
    avail = width - _TITLE_COLUMN
    if avail < 10:  # a pane too narrow to wrap sensibly: let Textual wrap
        return [title]
    return textwrap.wrap(title, width=avail, break_long_words=True,
                         break_on_hyphens=False) or [title]


#: A meta tag is carried as (style, plain text) until it is packed into rows,
#: so its width is the plain text's length — never a guess made by stripping
#: markup back out of a string that may itself contain an escaped bracket.
Tag = tuple[str, str]


def _markup(tag: Tag) -> str:
    style, text = tag
    return f"[{style}]{_escape(text)}[/]"


def _pack_tags(tags: list[Tag], width: int | None) -> list[str]:
    """The meta line, wrapped tag by tag so no tag is split across rows and
    every overflow row keeps the title column's indent. A single tag wider
    than the column (a long server-supplied `subtask_progress`) is the one
    case that must break mid-tag: it is hard-wrapped into pieces that fit,
    because a row wider than the pane comes back from Textual at column 0 —
    the defect this whole block exists to remove. Without a width it is the
    single line it always was."""
    sep = " [dim]·[/] "
    if width is None or width - _TITLE_COLUMN < 10:
        return [sep.join(_markup(t) for t in tags)]
    avail = width - _TITLE_COLUMN
    fitted: list[Tag] = []
    for style, text in tags:
        if len(text) <= avail:
            fitted.append((style, text))
        else:
            fitted.extend((style, piece) for piece in textwrap.wrap(
                text, width=avail, break_long_words=True, break_on_hyphens=False))
    rows: list[str] = []
    current: list[Tag] = []
    used = 0
    for tag in fitted:
        cost = len(tag[1]) + (3 if current else 0)  # " · " between tags
        if current and used + cost > avail:
            rows.append(sep.join(_markup(t) for t in current))
            current, used = [tag], len(tag[1])
        else:
            current.append(tag)
            used += cost
    if current:
        rows.append(sep.join(_markup(t) for t in current))
    return rows


def _task_lines(task: Task, *, selected: bool, width: int | None = None) -> list[str]:
    """One task as a block: the title (wrapped with a hanging indent when the
    pane width is known), then one indented meta line with the status and
    every tag the row has always carried. Nothing that was visible on the
    old single-line row is dropped; it is only moved under the title."""
    marker = "[reverse]>[/reverse]" if selected else " "
    status = _status_of(task) or "unknown"
    live = task.get("live_status")
    detail = str(live) if isinstance(live, str) and live else status
    tags: list[Tag] = [(_status_style(task), detail)]
    if task.get("claimed"):
        tags.append(("green", "running"))
    if is_waiting(task):
        tags.append(("dim", "waits for its own signal"))
    if task.get("approved_at"):
        tags.append(("dim", "approved - merge pending"))
    if task.get("blocker_human_stopped"):
        tags.append(("dim", "you stopped it"))
    if task.get("cancelled"):
        tags.append(("dim", "cancelled"))
    if task.get("subtask_progress"):
        tags.append(("dim", str(task["subtask_progress"])))
    burn = task_burn(task)
    if burn:
        tags.append(("dim", f"{burn:,} tok"))
    raw_title = str(task.get("title") or "(untitled)")
    chunks = [_escape(c) for c in _wrap_title(raw_title, width)]
    if selected:
        chunks = [f"[reverse]{c}[/reverse]" for c in chunks]
    indent = " " * _TITLE_COLUMN
    lines = [f" {marker} [dim]{_short(task):<8}[/]  {chunks[0]}"]
    lines.extend(indent + c for c in chunks[1:])
    lines.extend(indent + row for row in _pack_tags(tags, width))
    return lines


def render_lanes(tasks: Iterable[Task], selected_id: str | None = None,
                 *, width: int | None = None) -> str:
    """The board's columns as stacked sections, top to bottom in board order.

    The gate lanes are the point of the whole surface, so they get a filled
    header bar; the rest get a plain one. `width` is the lanes pane's content
    width in cells when the caller knows it; with it, long titles wrap under
    their own column instead of back to the margin. Without it (tests, a pane
    that has not been laid out yet) nothing is wrapped here and Textual wraps
    as before.
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
            # The Failed lane's header count excludes operator cancels (still
            # listed below, tagged "cancelled") — every other outcome/working
            # lane counts every row it holds. Only the count is scoped; the
            # empty-hint below stays gated on row presence for every lane, so
            # a lane holding only cancelled rows still shows its rows instead
            # of an empty-hint sitting above them.
            count = (sum(1 for t in rows if is_real_failure(t))
                      if lane.key == "failed" else len(rows))
            head = f"[bold {lane.colour}]{lane.label}[/] [dim]{count}[/]"
        lines.append(head)
        if not rows:
            lines.append(f"   [dim]{lane.empty_hint}[/]")
        for task in rows:
            lines.extend(_task_lines(task, selected=str(task.get("id")) == selected_id,
                                     width=width))
        lines.append("")
    return "\n".join(lines).rstrip()
