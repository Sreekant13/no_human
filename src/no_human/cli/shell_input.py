"""What the human typed, turned into something the shell can act on.

Two shapes, and the split is a single character: a line starting with `/` is a
slash command, anything else is natural language for the intake grill. Natural
language is the primary affordance; the slash set exists for people who would
rather type the verb, and every one of them is an endpoint the server already
serves - this module invents no server behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: A task id as the lanes pane prints it. Only a token this long and this
#: hex-shaped is read as an id in a free-text command, so `/reply added the
#: missing case` keeps its first word instead of losing it to an id slot.
#: `\Z`, not `$`: `$` also matches just before a trailing newline, and the head
#: word is split on a literal space, so "1234abcd\n" would have passed as an id.
#: A backstop — the value cannot arrive here and would not reach the wire if it
#: did; both guards are named in tests/test_cli_shell_model.py.
_TASK_ID = re.compile(r"^[0-9a-f]{8,}\Z")

MAX_TITLE = 120


@dataclass(frozen=True)
class SlashSpec:
    """`method` and `path` are None for the two commands that never leave the
    terminal (`/help`, `/quit`)."""
    method: str | None
    path: str | None
    needs_task: bool
    free_text: bool = False
    usage: str = ""
    summary: str = ""


SLASH_COMMANDS: dict[str, SlashSpec] = {
    "approve": SlashSpec("POST", "/api/tasks/{task_id}/approve", True,
                         usage="/approve [task]",
                         summary="approve the PR (the merge stays a human action)"),
    "diff": SlashSpec("GET", "/api/tasks/{task_id}/diff", True,
                      usage="/diff [task]", summary="show the diff"),
    "logs": SlashSpec("GET", "/api/tasks/{task_id}/events", True,
                      usage="/logs [task]", summary="replay the event log"),
    "pause": SlashSpec("POST", "/api/tasks/{task_id}/pause", True,
                       usage="/pause [task]", summary="park the task"),
    "resume": SlashSpec("POST", "/api/tasks/{task_id}/resume", True,
                        usage="/resume [task]", summary="unpark the task"),
    "cancel": SlashSpec("POST", "/api/tasks/{task_id}/cancel", True,
                        usage="/cancel [task]", summary="stop the task"),
    "reply": SlashSpec("POST", "/api/tasks/{task_id}/reply", True, free_text=True,
                       usage="/reply [task] <message>",
                       summary="answer the blocker the task is parked on"),
    "retry": SlashSpec("POST", "/api/tasks/{task_id}/retry", True,
                       usage="/retry [task]", summary="run the task again"),
    "help": SlashSpec(None, None, False, usage="/help", summary="this list"),
    "quit": SlashSpec(None, None, False, usage="/quit", summary="leave the shell"),
}


@dataclass(frozen=True)
class Slash:
    name: str
    task_id: str | None
    arg: str = ""

    @property
    def spec(self) -> SlashSpec:
        return SLASH_COMMANDS[self.name]


@dataclass(frozen=True)
class SlashError:
    message: str


def is_slash(text: str) -> bool:
    return text.lstrip().startswith("/")


def _known() -> str:
    return " ".join(f"/{name}" for name in SLASH_COMMANDS)


def parse_slash(text: str, selected_id: str | None) -> Slash | SlashError:
    """Parse one slash line. Returns a :class:`SlashError` rather than raising:
    a typo is a normal thing for a human to do and must read as a sentence."""
    body = text.lstrip()[1:].strip()
    if not body:
        return SlashError(f"type a command. Known: {_known()}")
    name, _, rest = body.partition(" ")
    name = name.lower()
    spec = SLASH_COMMANDS.get(name)
    if spec is None:
        return SlashError(f"/{name} is not a command. Known: {_known()}")

    rest = rest.strip()
    task_id: str | None = None
    arg = ""
    if spec.free_text:
        head, _, tail = rest.partition(" ")
        if _TASK_ID.match(head) and tail.strip():
            task_id, arg = head, tail.strip()
        else:
            arg = rest
    elif rest:
        task_id = rest.split()[0]

    if spec.needs_task:
        task_id = task_id or selected_id
        if not task_id:
            return SlashError(
                f"{spec.usage} needs a task - select one with ctrl+n / ctrl+p, "
                "or pass the id"
            )
    if spec.free_text and not arg:
        return SlashError(f"{spec.usage} needs a message")
    return Slash(name=name, task_id=task_id, arg=arg)


def help_text() -> str:
    lines = [
        "Type what you want in plain English - it goes to the same intake "
        "grill the web composer uses, and an accepted spec becomes a task.",
        "",
        "Slash commands (each one is an existing endpoint):",
    ]
    width = max(len(s.usage) for s in SLASH_COMMANDS.values())
    for spec in SLASH_COMMANDS.values():
        lines.append(f"  {spec.usage:<{width}}  {spec.summary}")
    lines += [
        "",
        "  ctrl+n / ctrl+p   move the selection down / up the lanes",
        "  ctrl+r            refresh the board now",
        "  ctrl+q            leave the shell",
        "",
        "A command with no task id acts on the selected task.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Intake                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class IntakeSession:
    """One run through POST /api/grill/stream, question by question.

    The endpoint is stateless - the caller carries the Q&A history - so this
    holds exactly what the web composer's `_grillParams` holds, in the shape
    the server's `GrillStepRequest` declares.
    """

    title: str
    description: str
    repo_path: str | None = None
    qa_history: list[dict[str, str]] = field(default_factory=list)
    pending_question: str | None = None
    suggestions: list[str] = field(default_factory=list)
    round: int = 0
    result: dict[str, Any] | None = None

    @classmethod
    def start(cls, text: str, repo_path: str | None = None) -> IntakeSession:
        body = text.strip()
        title = body.splitlines()[0].strip() if body else ""
        if len(title) > MAX_TITLE:
            title = title[:MAX_TITLE].rstrip()
        return cls(title=title, description=body, repo_path=repo_path)

    def payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "repo_path": self.repo_path,
            "qa_history": list(self.qa_history),
        }

    def take_question(self, frame: dict[str, Any]) -> None:
        self.pending_question = str(frame.get("question") or "")
        raw = frame.get("suggestions")
        self.suggestions = [str(s) for s in raw] if isinstance(raw, list) else []
        try:
            self.round = int(frame.get("round") or 0)
        except (TypeError, ValueError):
            self.round = 0

    def take_answer(self, answer: str) -> None:
        if not self.pending_question:
            raise ValueError("no grill question is pending")
        self.qa_history.append(
            {"question": self.pending_question, "answer": answer.strip()})
        self.pending_question = None
        self.suggestions = []

    def take_result(self, frame: dict[str, Any]) -> None:
        self.result = dict(frame)
        self.pending_question = None
        self.suggestions = []

    def task_payload(self) -> dict[str, Any]:
        """What POST /api/tasks gets - the composer's create call, minus the
        fields a terminal has no picker for."""
        if self.result is None:
            raise ValueError("the grill has not produced a spec yet")
        criteria = self.result.get("acceptance_criteria")
        return {
            "title": str(self.result.get("title") or self.title),
            "description": self.result.get("description") or self.description,
            "repo_path": self.repo_path,
            "acceptance_criteria": [str(c) for c in criteria] if isinstance(criteria, list) else [],
        }
