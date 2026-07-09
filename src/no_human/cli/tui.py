"""`nh watch` — a Claude-Code-like live view of an agent run (Textual + Rich).

Renders the stream of tool calls, a live status line (step, turns, tokens,
elapsed), and a scrolling activity log. Phase 0 runs a *staged* (pending) task
inside the TUI; later phases attach to the daemon's live stream.
"""

from __future__ import annotations

import time

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, RichLog, Static

from ..core.db import Store
from ..core.orchestrator import is_agent_session


class WatchApp(App):
    CSS = """
    Screen { background: $surface; }
    #status { height: 3; padding: 0 1; color: $text; background: $panel; }
    #log { border: round $primary; height: 1fr; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, config, task_id: str):
        super().__init__()
        self.config = config
        self.task_id = task_id
        self._start = time.monotonic()
        self._turns = 0
        self._tokens = 0
        self._step = "starting"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="status")
            yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._run(), exclusive=True)
        self.set_interval(0.5, self._refresh_status)

    def _refresh_status(self) -> None:
        elapsed = int(time.monotonic() - self._start)
        self.query_one("#status", Static).update(
            f"[b]{self.task_id[:8]}[/]  step=[blue]{self._step}[/]  "
            f"turns={self._turns}  tokens={self._tokens}  elapsed={elapsed}s"
        )

    def _sink(self, event: dict) -> None:
        log = self.query_one("#log", RichLog)
        src, kind, text = event.get("source"), event.get("kind"), event.get("text", "")
        if is_agent_session(src):
            if kind == "tool_use":
                args = event.get("tool_input") or {}
                summary = ", ".join(f"{k}={str(v)[:50]}" for k, v in list(args.items())[:3])
                log.write(f"  [cyan]→ {event.get('tool_name')}[/]([dim]{summary}[/])")
            elif kind == "text" and text.strip():
                log.write(f"  {text.strip()[:400]}")
            elif kind == "thinking" and text.strip():
                log.write(f"  [dim italic]· {text.strip()[:160]}[/]")
            elif kind == "tool_result" and text.strip():
                log.write(f"    [dim]{text.strip()[:160]}[/]")
            elif kind == "result":
                self._turns = event.get("num_turns", self._turns)
                self._tokens = event.get("tokens_used", self._tokens)
        else:
            if "status" in event:
                self._step = event["status"]
            log.write(f"[blue]● {kind}[/] {text}")

    async def _run(self) -> None:
        log = self.query_one("#log", RichLog)
        async with Store(self.config.db_path) as store:
            t = await store.find_task(self.task_id)
            if not t:
                log.write(f"[red]no task matching {self.task_id}[/]")
                return
            log.write(f"[b]{t.title}[/]")
            # One production construction site. This used to build its own
            # Orchestrator and forgot the reviewer, so `nh watch` drove tasks
            # all the way to a PR with the review gate silently pass-through.
            from .commands import _build_orchestrator

            orch = _build_orchestrator(
                self.config, store, event_sink=self._sink, task=t
            )
            outcome = await orch.run_task(t)
            log.write(f"[bold]── {outcome.status.value} ──[/]")
            if outcome.pr_url:
                log.write(f"[bold green]PR: {outcome.pr_url}[/]")
            log.write(outcome.detail)
            self._step = outcome.status.value


def run_watch(config, task_id: str) -> None:
    WatchApp(config, task_id).run()
