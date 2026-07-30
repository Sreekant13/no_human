"""`nh` with no arguments - the conversational shell.

Three panes and a prompt. The LANES pane is the board's columns, the
CONVERSATION pane is the intake grill talking to you, and the DETAIL pane
tails the focused task's events with the same RichLog treatment `nh watch`
uses. Typed text is natural language by default; a leading `/` is the escape
hatch for people who would rather name the verb.

Two things this deliberately does NOT do:

* It never opens the database. `nh watch`'s TUI constructs
  `Store(config.db_path)` while the server holds the same SQLite file open,
  and races it. Everything here goes over http://127.0.0.1:8420, which also
  means the day the server is not local, only the URL changes.
* It never re-implements the grill. POST /api/grill/stream is the same
  endpoint the web composer drives, frame kind for frame kind, so the intake
  cannot drift between the two surfaces.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, RichLog, Static

from .api_client import (
    DEFAULT_BASE_URL,
    NhApiError,
    NhClient,
    NhServerUnreachable,
    classify_grill_frame,
    unreachable_message,
)
from .shell_input import (
    IntakeSession,
    Slash,
    SlashError,
    help_text,
    is_slash,
    parse_slash,
)
from .shell_lanes import flat_order, lane_for, needs_you, render_header, render_lanes

ACCEPT_WORDS = {"yes", "y", "ok", "okay", "go", "create", "ship it", "do it"}
DECLINE_WORDS = {"no", "n", "cancel", "stop", "drop", "nevermind", "never mind"}


def _escape(text: object) -> str:
    """A tool name or a server message may contain square brackets; Rich would
    read them as markup and eat them."""
    return str(text).replace("[", r"\[")


class ShellApp(App):
    """The whole surface. `client` is injected so tests never need a server."""

    CSS = """
    Screen { background: $surface; layers: base; }
    #header { height: 1; padding: 0 1; background: $panel; color: $text; }
    #body { height: 1fr; }
    #lanes-scroll { width: 45%; border: round $primary; }
    #lanes { padding: 0 1; }
    #right { width: 1fr; }
    #conversation { height: 1fr; border: round $accent; padding: 0 1; }
    #detail { height: 40%; border: round $primary; padding: 0 1; }
    #prompt { dock: bottom; }
    """

    # ctrl+p is the readline "previous" every terminal user already has in
    # their fingers, and Textual binds it to the command palette with
    # priority=True — a priority binding wins over ours, so the palette has to
    # go rather than be re-keyed. Nothing is registered in it here anyway: the
    # slash set is this app's command surface, and a second empty one is a
    # decoy.
    ENABLE_COMMAND_PALETTE = False

    # ctrl+c is the reflex every terminal user has for "get me out", and in
    # Textual 8 it does NOT quit: App binds it to `help_quit`, whose own
    # docstring says so, and a focused Input binds it to `copy` — and #prompt
    # holds focus for this app's whole life, so the reflex reached the copy
    # binding and looked like nothing happened. Only a PRIORITY binding is
    # checked ahead of the focused widget, hence Binding() rather than a tuple.
    # show=False keeps the footer showing one way out, not two.
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+n", "next_task", "Next task"),
        ("ctrl+p", "prev_task", "Prev task"),
        ("ctrl+r", "refresh_board", "Refresh"),
    ]

    def __init__(
        self,
        client: Any,
        *,
        repo_path: str | None = None,
        poll_interval: float = 3.0,
        follow_reconnect: float = 3.0,
    ) -> None:
        super().__init__()
        self.client = client
        self.repo_path = repo_path
        self.poll_interval = poll_interval
        #: Seconds to wait before re-opening an event stream the server closed.
        #: 0 disables reconnection (tests that assert on one pass of a fake).
        self.follow_reconnect = follow_reconnect
        self.tasks: list[dict[str, Any]] = []
        self.selected_id: str | None = None
        self.intake: IntakeSession | None = None
        self._grill_busy = False

    # -- layout ------------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        with Horizontal(id="body"):
            with VerticalScroll(id="lanes-scroll"):
                yield Static("", id="lanes")
            with Vertical(id="right"):
                yield RichLog(id="conversation", markup=True, wrap=True, highlight=False)
                yield RichLog(id="detail", markup=True, wrap=True, highlight=False)
        yield Input(placeholder="Say what you want done - or /help", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._say("[b]no_human[/] - say what you want done, in plain English.")
        self._say("[dim]It goes to the same intake grill the board uses. "
                  "/help for the slash commands.[/]")
        self.query_one("#prompt", Input).focus()
        self.run_worker(self._refresh(), group="board")
        if self.poll_interval:
            self.set_interval(self.poll_interval, self.action_refresh_board)

    # -- output helpers ------------------------------------------------------ #

    def _say(self, markup: str) -> None:
        self.query_one("#conversation", RichLog).write(markup)

    def _say_error(self, exc: BaseException) -> None:
        """Every failure path lands here. A stack trace is never the answer -
        an unreachable server carries the command that starts one."""
        for line in str(exc).splitlines():
            self._say(f"[red]{_escape(line)}[/]")

    def _detail(self, markup: str) -> None:
        self.query_one("#detail", RichLog).write(markup)

    # -- board --------------------------------------------------------------- #

    def lane_of(self, task_id: str) -> str | None:
        for task in self.tasks:
            if str(task.get("id")) == task_id:
                return lane_for(task)
        return None

    async def _refresh(self) -> None:
        try:
            tasks = await self.client.board()
        except (NhServerUnreachable, NhApiError) as exc:
            self._say_error(exc)
            return
        self.tasks = list(tasks)
        self._reconcile_selection()
        self._paint()

    def _reconcile_selection(self) -> None:
        """Keep the selection if it still exists; otherwise land on the first
        task that needs a human, because that is what this surface is for."""
        ids = {str(t.get("id")) for t in self.tasks}
        if self.selected_id in ids:
            return
        gates = [str(t.get("id")) for t in self.tasks if needs_you(t)]
        order = flat_order(self.tasks)
        self._select(gates[0] if gates else (order[0] if order else None))

    def _select(self, task_id: str | None) -> None:
        if task_id == self.selected_id:
            return
        self.selected_id = task_id
        self.query_one("#detail", RichLog).clear()
        if task_id:
            self._detail(f"[b]{task_id[:8]}[/] [dim]- live events[/]")
            self.run_worker(self._follow(task_id), group="events", exclusive=True)

    def _paint(self) -> None:
        self.query_one("#header", Static).update(render_header(self.tasks))
        self.query_one("#lanes", Static).update(
            render_lanes(self.tasks, selected_id=self.selected_id))

    async def _follow(self, task_id: str) -> None:
        """GET /api/tasks/{id}/events/stream, rendered like `nh watch`.

        The server ENDS this stream whenever the task is not in flight for five
        ticks (app.py `task_events_stream`), which for anything parked, queued
        or awaiting approval is about six seconds after you select it. Running
        the stream once therefore left the detail pane permanently silent for
        the tasks this surface exists to show you, and the only way back was to
        select the task again.

        So it reconnects, carrying the `last-event-id` cursor the server reads
        so a reconnect resumes instead of replaying the deque. The worker is
        `exclusive=True` in its group: selecting another task cancels this
        loop, and app exit cancels it too.
        """
        cursor = ""
        while True:
            try:
                async for event in self.client.stream_events(
                        task_id, last_event_id=cursor):
                    ts = event.get("ts")
                    if ts is not None:
                        cursor = str(ts)
                    if event.get("kind") == "done":
                        continue
                    self._detail(self._format_event(event))
            except (NhServerUnreachable, NhApiError) as exc:
                self._say_error(exc)
                return
            if not self.follow_reconnect:
                return
            await asyncio.sleep(self.follow_reconnect)

    @staticmethod
    def _format_event(event: dict[str, Any]) -> str:
        kind = str(event.get("kind") or "")
        text = _escape(event.get("text") or "").strip()
        if kind == "tool_use":
            return f"  [cyan]-> {text}[/]"
        if kind == "thinking":
            return f"  [dim italic]. {text[:160]}[/]"
        if kind == "error":
            return f"  [red]{text}[/]"
        if kind == "result":
            return f"[bold]-- {text} --[/]"
        return f"[blue]* {_escape(kind)}[/] {text}"

    # -- actions -------------------------------------------------------------- #

    def action_refresh_board(self) -> None:
        self.run_worker(self._refresh(), group="board")

    def _step_selection(self, delta: int) -> None:
        order = flat_order(self.tasks)
        if not order:
            return
        try:
            index = order.index(self.selected_id or "")
        except ValueError:
            index = 0 if delta > 0 else len(order) - 1
        else:
            index = max(0, min(len(order) - 1, index + delta))
        self._select(order[index])
        self._paint()

    def action_next_task(self) -> None:
        self._step_selection(1)

    def action_prev_task(self) -> None:
        self._step_selection(-1)

    # -- input ---------------------------------------------------------------- #

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if is_slash(text):
            self._say(f"[dim]> {_escape(text)}[/]")
            await self._run_slash(parse_slash(text, self.selected_id))
        else:
            await self._handle_text(text)

    # -- slash ---------------------------------------------------------------- #

    async def _run_slash(self, parsed: Slash | SlashError) -> None:
        if isinstance(parsed, SlashError):
            self._say(f"[yellow]{_escape(parsed.message)}[/]")
            return
        if parsed.name == "help":
            for line in help_text().splitlines():
                self._say(_escape(line) or " ")
            return
        if parsed.name == "quit":
            self.exit()
            return
        self.run_worker(self._call_slash(parsed), group="slash")

    async def _call_slash(self, cmd: Slash) -> None:
        task_id = cmd.task_id or ""
        try:
            if cmd.name == "diff":
                diff = await self.client.diff(task_id)
                self.query_one("#detail", RichLog).clear()
                self._detail(f"[b]diff {task_id[:8]}[/]")
                for line in (diff or "(no diff)").splitlines():
                    self._detail(f"[dim]{_escape(line)}[/]")
            elif cmd.name == "logs":
                events = await self.client.events(task_id)
                self.query_one("#detail", RichLog).clear()
                self._detail(f"[b]log {task_id[:8]}[/] [dim]{len(events)} events[/]")
                for event in events[-200:]:
                    self._detail(self._format_event(event))
            elif cmd.name == "reply":
                await self.client.reply(task_id, cmd.arg)
                self._say(f"[green]answered {task_id[:8]}[/]")
            else:
                await self.client.act(task_id, cmd.name)
                self._say(f"[green]{cmd.name} {task_id[:8]}[/]")
        except (NhServerUnreachable, NhApiError) as exc:
            self._say_error(exc)
            return
        await self._refresh()

    # -- natural language ------------------------------------------------------ #

    async def _handle_text(self, text: str) -> None:
        self._say(f"[b]you:[/] {_escape(text)}")
        if self._grill_busy:
            self._say("[yellow]still working on the last one - one moment[/]")
            return

        session = self.intake
        if session is not None and session.result is not None:
            await self._decide_on_spec(session, text)
            return
        if session is not None and session.pending_question:
            session.take_answer(text)
        else:
            session = self.intake = IntakeSession.start(text, repo_path=self.repo_path)
        self.run_worker(self._grill(), group="grill")

    async def _decide_on_spec(self, session: IntakeSession, text: str) -> None:
        answer = text.strip().lower().rstrip(".!")
        if answer in ACCEPT_WORDS:
            await self._create_task(session)
            return
        if answer in DECLINE_WORDS:
            self.intake = None
            self._say("[yellow]discarded - nothing was created[/]")
            return
        self._say("[yellow]type [b]yes[/b] to create this task, or [b]no[/b] to "
                  "discard it[/]")

    async def _grill(self) -> None:
        session = self.intake
        if session is None:
            return
        self._grill_busy = True
        self._say("[dim]grilling the spec...[/]")
        try:
            async for raw in self.client.grill_stream(**session.payload()):
                self._render_grill_frame(session, raw)
        except (NhServerUnreachable, NhApiError) as exc:
            self._say_error(exc)
        finally:
            self._grill_busy = False

    def _render_grill_frame(self, session: IntakeSession, frame: dict[str, Any]) -> None:
        kind = classify_grill_frame(frame)
        text = _escape(frame.get("text") or "").strip()
        if kind == "event":
            if text:
                self._say(f"  [dim cyan]{text}[/]")
        elif kind == "eval":
            verdict = _escape(frame.get("verdict") or "")
            rationale = _escape(frame.get("rationale") or "")
            self._say(f"  [magenta]spec check: {verdict}[/] [dim]{rationale}[/]")
        elif kind == "question":
            session.take_question(frame)
            self._say(f"[b yellow]grill:[/] {_escape(session.pending_question)}")
            for suggestion in session.suggestions:
                self._say(f"    [dim]- {_escape(suggestion)}[/]")
            self._say("[dim]answer in plain English.[/]")
        elif kind == "result":
            session.take_result(frame)
            self._render_spec(session)
        elif kind == "error":
            self._say(f"[red]{text or 'the grill failed'}[/]")

    def _render_spec(self, session: IntakeSession) -> None:
        spec = session.result or {}
        self._say(f"[b green]refined spec:[/] {_escape(spec.get('title') or '')}")
        description = str(spec.get("description") or "").strip()
        if description:
            self._say(f"  [dim]{_escape(description)}[/]")
        criteria = spec.get("acceptance_criteria")
        if isinstance(criteria, list) and criteria:
            self._say("  [b]acceptance criteria[/]")
            for i, item in enumerate(criteria, 1):
                self._say(f"    {i}. {_escape(item)}")
        self._say("[b]create this task?[/] type [b]yes[/b] or [b]no[/b]")

    async def _create_task(self, session: IntakeSession) -> None:
        try:
            created = await self.client.create_task(**session.task_payload())
        except (NhServerUnreachable, NhApiError) as exc:
            self._say_error(exc)
            return
        self.intake = None
        task_id = str(created.get("id") or "")
        self._say(f"[bold green]created {task_id[:8]}[/] - "
                  "it lands in Working and the agent picks it up")
        await self._refresh()
        self._select(task_id or self.selected_id)
        self._paint()


# --------------------------------------------------------------------------- #
# Launcher                                                                     #
# --------------------------------------------------------------------------- #

def _repo_from_cwd() -> str | None:
    """The repo the operator is standing in, if any. POST /api/tasks rejects a
    path that is not a git checkout, so anything else is sent as no repo."""
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def stdio_is_interactive() -> bool:
    """Is there a human at a terminal on BOTH ends?

    Textual takes the screen and does not give it back until a key says so, so
    without a terminal there is no key and no exit: `nh </dev/null` ran
    forever, wrote nothing to stdout, and painted a full screen of escapes to
    stderr. That wedges a CI job, a wrapper script, and — since no_human's own
    coder agents run shell commands — an agent's run. Worse, the SIGINT that
    eventually kills it exits 0, so the wedge reads as success.

    A stream that has been closed or replaced raises instead of answering;
    guessing "interactive" there is how the hang comes back.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            if not stream.isatty():
                return False
        except (AttributeError, ValueError, OSError):
            return False
    return True


def non_interactive_message() -> str:
    """What a non-tty caller gets instead of a full-screen app: one line of
    reason, then the verb list bare `nh` used to print."""
    head = ("nh's conversational shell needs an interactive terminal, and "
            "stdin/stdout here are not one.\n"
            "Run `nh` from a terminal for the shell, or use a verb:\n")
    try:
        import click

        from .commands import cli

        return head + "\n" + cli.get_help(click.Context(cli, info_name="nh"))
    except Exception:  # noqa: BLE001 — the reason must survive a help failure
        return head


def base_url_from_config(config: Any = None) -> str:
    if config is None:
        return DEFAULT_BASE_URL
    try:
        server = config.get("server", {}) or {}
    except AttributeError:
        server = {}
    host = server.get("host") or "127.0.0.1"
    port = server.get("port") or 8420
    return f"http://{host}:{port}"


def run_shell(
    config: Any = None,
    *,
    base_url: str | None = None,
    repo_path: str | None = None,
    _echo: Any = None,
) -> int:
    """Launch the shell. Returns the process exit code.

    The reachability probe happens BEFORE Textual takes the terminal: a
    full-screen app that then paints "cannot connect" is a worse way to learn
    the server is down than one plain line on stdout.

    The tty guard happens before even that. A caller with no terminal must not
    pay a connect timeout — up to 15s against a blackholed host — to be told
    something that was knowable with no network at all.
    """
    echo = _echo or print
    url = base_url or base_url_from_config(config)

    if not stdio_is_interactive():
        echo(non_interactive_message())
        return 2

    async def _probe() -> bool:
        async with NhClient(url) as client:
            return await client.ping()

    if not asyncio.run(_probe()):
        echo(unreachable_message(url))
        return 1

    async def _run() -> None:
        async with NhClient(url) as client:
            app = ShellApp(client, repo_path=repo_path or _repo_from_cwd())
            await app.run_async()

    asyncio.run(_run())
    return 0


__all__ = [
    "ShellApp",
    "base_url_from_config",
    "non_interactive_message",
    "run_shell",
    "stdio_is_interactive",
]
