"""Record the `nh` shell for the narrated sprint demo, one frame at a time.

The harness is ``cli_frames.py``'s, reused verbatim where it can be: the real
:class:`ShellApp` under Textual's pilot at 100x31, a :class:`FrameClock` the
scripted client blocks on, Rich-HTML export painted into Chromium at a chosen
cell size. What changes is the world (``sprint.py``) and the script — and, as
in ``sprint_gui.py``, every scripted time is a narration.py anchor, so the
shell and the voice stay in step by construction.

    PYTHONPATH=src python -m e2e.demo_video.sprint_cli /tmp/nh-sprint-demo/frames-cli

What the clip shows, beat by beat:

    hook      the quiet shell — one leftover task in its Review lane
    handoff   five tasks fill the lanes as the tracker sync lands them
    parallel  the webhook task is selected; its live event stream fills the
              detail pane — models, plan, edits, tests
    gate      `/diff` prints the same bytes the board's Diff section shows;
              the lanes behind it show the refactor bouncing back
    payoff    `/logs` replays the evidence trail; `/approve` is typed and
              answered
    close     the whole shell, quiet prompt, holds to the fade
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from . import narration as nr
from . import sprint as sp
from .cli_frames import COLS, ROWS, FrameClock, render, screen_html

#: Where the detail pane is scrolled while the gate beat holds on `/diff`:
#: to the top of the webhooks.py hunk, so the change the narration is talking
#: about ("it reads the diff line by line") is the change on camera.
DIFF_SCROLL_Y = 4

#: Typing cadence for slash commands — cli_frames' own, unchanged.
CMD_CPS = 11.3


class SprintClient:
    """Every call `ShellApp` makes on `NhClient`, answered from sprint.py."""

    def __init__(self, clock: FrameClock) -> None:
        self.clock = clock

    async def board(self):
        return sp.board_at(nr.t_of(max(self.clock.frame, 0)))

    async def stream_events(self, task_id, last_event_id=""):
        if task_id == sp.HERO_ID:
            for due, event in sp.EVENT_TRAIL:
                await self.clock.until(due)
                yield event
        await self.clock.until(1e6)   # hold the stream open; the fake never closes

    async def events(self, task_id):
        return [e for _, e in sp.EVENT_TRAIL]

    async def diff(self, task_id):
        return sp.DIFF

    async def act(self, task_id, verb):
        return {"ok": True}

    async def reply(self, task_id, answer):
        return {"ok": True}

    async def create_task(self, **kw):   # never called in this cut
        return {"id": sp.HERO_ID}

    async def grill_stream(self, **kw):  # never called in this cut
        return
        yield  # pragma: no cover


def build_script() -> list[tuple[float, str, str]]:
    """(time, kind, payload); kind is key / enter / submit / refresh /
    select / scroll. Refreshes repaint the lanes on every fixture change —
    the shell polls in production; here the poll is the script, indexed to
    the same clock the fixture advances on."""
    script: list[tuple[float, str, str]] = []
    # --- handoff + the whole run: repaint on every status change ----------- #
    stamps = sorted({start for stages in sp.STAGES.values()
                     for start, *_ in stages})
    script += [(t + 0.05, "refresh", "") for t in stamps]
    # --- parallel: follow the hero -----------------------------------------#
    # Selection is the shell's own affordance (j/k step it); the recorder
    # selects the hero directly so the detail pane follows its event stream.
    script.append((sp.PARALLEL + 1.0, "select", sp.HERO_ID))
    # --- gate: the diff, in the terminal ------------------------------------#
    script += [(44.50 + i / CMD_CPS, "key", ch)
               for i, ch in enumerate("/diff")]
    script.append((45.10, "enter", ""))
    script.append((45.60, "scroll", str(DIFF_SCROLL_Y)))
    # --- payoff: the evidence trail, then the one human act ------------------#
    script.append((56.20, "submit", "/logs"))
    script += [(65.30 + i / CMD_CPS, "key", ch)
               for i, ch in enumerate("/approve")]
    script.append((sp.APPROVE_AT + 0.05, "enter", ""))
    script.append((68.00, "refresh", ""))
    script.append((sp.CLOSE + 0.40, "refresh", ""))
    return sorted(script, key=lambda s: s[0])


async def capture_html() -> list[str]:
    from no_human.cli.shell import ShellApp

    clock = FrameClock()
    app = ShellApp(SprintClient(clock), repo_path=sp.REPO_PATH,
                   poll_interval=0, follow_reconnect=0)
    script = build_script()
    out: list[str] = []

    async with app.run_test(size=(COLS, ROWS)) as pilot:
        for _ in range(3):
            await pilot.pause()
        from textual.widgets import Input
        app.query_one("#prompt", Input).cursor_blink = False
        await pilot.pause()
        cursor = 0
        for frame in range(nr.N_FRAMES):
            t = nr.t_of(frame)
            await clock.advance(frame)
            while cursor < len(script) and script[cursor][0] <= t:
                _, kind, payload = script[cursor]
                cursor += 1
                if kind == "key":
                    await pilot.press("space" if payload == " " else payload)
                elif kind == "enter":
                    await pilot.press("enter")
                elif kind == "submit":
                    app.query_one("#prompt", Input).value = payload
                    await pilot.pause()
                    await pilot.press("enter")
                elif kind == "refresh":
                    app.action_refresh_board()
                elif kind == "select":
                    app._select(payload)
                elif kind == "scroll":
                    from textual.widgets import RichLog
                    app.query_one("#detail", RichLog).scroll_to(
                        y=int(payload), animate=False)
            for _ in range(3):
                await pilot.pause()
            out.append(screen_html(app))
    return out


async def main(out_dir: Path) -> None:
    frames = await capture_html()
    print(f"[sprint-cli] {len(frames)} screens captured")
    await render(frames, out_dir)
    print(f"[sprint-cli] frames -> {out_dir}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1
                          else "/tmp/nh-sprint-demo/frames-cli")))
