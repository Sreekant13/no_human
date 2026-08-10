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


def _event_burn(event: dict) -> int:
    """Every token a result event reports — non-cache + cache-read +
    cache-creation. The status line showed `tokens_used` alone (non-cache),
    which under-reports real cost the same way `nh logs` and `nh agents` did
    before #210; cache reads are the bulk of the burn. Pure so it is testable
    without mounting the Textual app."""
    return (int(event.get("tokens_used") or 0)
            + int(event.get("cache_read_tokens") or 0)
            + int(event.get("cache_creation_tokens") or 0))


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
            # min_width=0 for the same reason as shell.py's panes: RichLog's
            # min_width floor (78) is applied after `shrink` and undoes it, so
            # on a terminal under ~80 columns every event line is rendered at
            # 78 and then cropped mid-word to what fits. See the long comment
            # above ShellApp.compose.
            yield RichLog(id="log", highlight=True, markup=True, wrap=True,
                          min_width=0)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._run(), exclusive=True)
        self.set_interval(0.5, self._refresh_status)

    def _refresh_status(self) -> None:
        elapsed = int(time.monotonic() - self._start)
        self.query_one("#status", Static).update(
            f"[b]{self.task_id[:8]}[/]  step=[blue]{self._step}[/]  "
            f"turns={self._turns}  burn={self._tokens:,}  elapsed={elapsed}s"
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
                self._tokens = _event_burn(event) or self._tokens
            elif text.strip():
                # Everything else an agent session emits. This used to be the
                # end of the chain, so any kind not named above was silently
                # DROPPED — and `transport_retry` / `transport_failed` (a dead
                # nested SDK session being retried, at full cost) landed in
                # exactly that hole: the backend went to the trouble of saying
                # "I am spending a second session because the first one died"
                # and `nh watch` showed a gap. A catch-all rather than two more
                # names, so the next kind added to `AgentEvent` is visible on
                # the day it is added instead of the day someone notices.
                log.write(f"  [yellow]! {kind}[/] {text.strip()[:400]}")
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
            from .commands import _build_orchestrator, _persisting
            from ..core.events import EventPersister

            # Persist events like every other in-process runner (`nh task add
            # --run`, `nh reply --run`): the board reads task_events, and the
            # scheduler's stranded sweep reads them as LIVENESS — a TUI run
            # that persisted nothing was invisible to both, so an `nh serve`
            # scheduler sharing the DB could requeue it mid-review (R15/B2).
            async with EventPersister(store, t.id) as persister:
                orch = _build_orchestrator(
                    self.config, store,
                    event_sink=_persisting(persister, t.id, self._sink),
                    task=t,
                )
                outcome = await orch.run_task(t)
            log.write(f"[bold]── {outcome.status.value} ──[/]")
            if outcome.pr_url:
                log.write(f"[bold green]PR: {outcome.pr_url}[/]")
            log.write(outcome.detail)
            self._step = outcome.status.value


def run_watch(config, task_id: str) -> None:
    WatchApp(config, task_id).run()
