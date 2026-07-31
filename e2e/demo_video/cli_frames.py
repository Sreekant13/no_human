"""Record the `nh` conversational shell, one frame at a time.

Not a screen capture. The real :class:`ShellApp` runs headless under Textual's
own test pilot at a fixed 100x31 grid; a scripted clock releases each server
response on a named frame; the composited screen is exported as Rich HTML and
painted into a Chromium ``<pre>`` whose cell size we choose. Re-running it
produces byte-identical frames, which a hand-timed asciinema recording never
does — and which is the only reason the next person can regenerate this asset.

The client is injected, not stubbed at the socket: ``ShellApp(client, ...)``
exists precisely so "tests never need a server" (shell.py). Every payload the
fake returns comes from ``fixture.py`` and is the shape the real endpoints
return. What is rendered is 100% the shipped rendering code — shell.py,
shell_lanes.py, shell_input.py, Textual's compositor.

    python -m e2e.demo_video.cli_frames /tmp/frames-cli
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

from rich.console import Console

from . import fixture as fx
from .camera import FPS, N_FRAMES, t_of

# 100 columns is what the shell's three-pane layout actually wants; it was 84
# only because a 12 px floor at a 600 CSS px display forbade anything wider
# (camera.py). Full frame at ~1176 CSS px puts 100 columns at 19.6 effective px
# — nearly twice the old floor — so the terminal is finally the size the
# product is meant to be run at, and the conversation pane stops wrapping every
# other line. 31 rows makes the grid 16:9 at a 1.8 cell ratio (100/31 = 3.2).
COLS, ROWS = 100, 31
CELL_W, CELL_H = 25, 45          # supersampled; 100*25 x 31*45 = 2500 x 1395
VIEW_W, VIEW_H = 2560, 1440      # 16:9, with the difference used as padding
PAD_X = (VIEW_W - COLS * CELL_W) / 2
PAD_Y = (VIEW_H - ROWS * CELL_H) / 2

BG = "#1e1e1e"   # Textual's dark-theme surface; the padding must match it

PAGE = """
<style>
  html, body {{ margin:0; padding:0; background:{bg}; }}
  #s {{
    margin:0; padding:{pad_y}px {pad_x}px;
    font-family: Menlo, "DejaVu Sans Mono", monospace;
    font-size: {fs}px; line-height: {lh}px;
    white-space: pre; font-variant-ligatures: none;
    -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision;
  }}
</style>
<pre id="s"></pre>
<span id="probe" style="font-family:Menlo,monospace;font-size:100px;position:absolute;
      visibility:hidden;white-space:pre">MMMMMMMMMM</span>
"""


# --------------------------------------------------------------------------- #
# A clock the fake client blocks on                                            #
# --------------------------------------------------------------------------- #

class FrameClock:
    """Wall time is not the clock here; the frame index is.

    A worker that wants to deliver a frame at t=5.95 s awaits ``until(5.95)``
    and is released when the recorder steps past frame 178. Nothing races.
    """

    def __init__(self) -> None:
        self.frame = -1
        self._cond = asyncio.Condition()

    async def advance(self, frame: int) -> None:
        async with self._cond:
            self.frame = frame
            self._cond.notify_all()

    async def until(self, t: float) -> None:
        target = int(round(t * FPS))
        async with self._cond:
            await self._cond.wait_for(lambda: self.frame >= target)


class ScriptedClient:
    """Every call `ShellApp` makes on `NhClient`, answered from the fixture."""

    def __init__(self, clock: FrameClock) -> None:
        self.clock = clock

    async def board(self):
        return fx.board_at(t_of(max(self.clock.frame, 0)))

    async def stream_events(self, task_id, last_event_id=""):
        for due, event in fx.EVENT_TRAIL:
            await self.clock.until(due)
            yield event
        await self.clock.until(1e6)   # hold the stream open for the whole clip

    async def grill_stream(self, *, title, description=None, repo_path=None,
                           project_id=None, qa_history=None):
        rounds = list(qa_history or [])
        frames = fx.GRILL_ROUND_2 if rounds else fx.GRILL_ROUND_1
        # 0.32 s between frames: fast enough to fit a 4.6 s beat, slow enough
        # that a viewer reads them arriving one at a time rather than as a
        # block. (0.18 on the 12-second cut, scaled by the same 4.6/2.6.)
        base = t_of(self.clock.frame) + 0.35
        for i, frame in enumerate(frames):
            await self.clock.until(base + i * 0.32)
            yield dict(frame)

    async def create_task(self, **kw):
        return {"id": fx.HERO_ID}

    async def act(self, task_id, verb):
        return {"ok": True}

    async def events(self, task_id):
        return [e for _, e in fx.EVENT_TRAIL]

    async def diff(self, task_id):
        return fx.DIFF

    async def reply(self, task_id, answer):
        return {"ok": True}


# --------------------------------------------------------------------------- #
# The script                                                                   #
# --------------------------------------------------------------------------- #

def _typing(text: str, start: float, cps: float) -> list[tuple[float, str, str]]:
    """(time, "key", key-name) triples at an EVEN cadence.

    The brief forbids speed-ramped typing, so a line that will not fit its beat
    is cut or submitted whole (see the "submit" step) — never accelerated at
    the end. Only space needs a key NAME; Textual's pilot takes every other
    printable character literally.
    """
    return [(start + i / cps, "key", "space" if ch == " " else ch)
            for i, ch in enumerate(text)]


#: Where the detail pane is scrolled to while beat 4 holds on the diff.
#:
#: `/diff` writes 30 lines into an 8-row pane, so what a viewer would otherwise
#: see is whatever the diff ENDS on — the test file's hunk. The change the
#: ticket is about is `mul()`, so the pane is scrolled to it. This is a real
#: affordance, not a staged one: `RichLog` is a Textual `ScrollView` and a
#: person running `nh` scrolls it with the wheel; the recorder does it with the
#: widget's own `scroll_to` because a pilot has no wheel. Measured against the
#: rendered frame, not guessed — see :func:`_diff_scroll_probe`.
DIFF_SCROLL_Y = 8


def build_script() -> list[tuple[float, str, str]]:
    """(time, kind, payload); kind is key / enter / submit / refresh / scroll.

    Beat 1 has 4.6 s to carry a two-round grill, which is still more than 4.6 s
    of typing. The answer to that is a cut, not a speed ramp: the request is
    typed on camera because that is the moment being sold, and the two short
    replies ("raise TypeError", "yes") are SUBMITTED whole — a jump cut. The
    conversation pane is a transcript, so nothing is lost: the answer is still
    on screen, in the log, right where a viewer looks next.

    Beats 2 and 3 are scripted only as board refreshes: what they show arrives
    on its own, from the fixture's grill result (which is still in the
    transcript) and its event stream (which the shell follows into the detail
    pane). Beats 4 and 5 are the two slash commands the shell has for exactly
    these two questions — `/diff` and `/logs`.
    """
    script: list[tuple[float, str, str]] = []
    # --- beat 1: you describe it, it writes the spec ----------------------- #
    script += _typing(fx.HERO_PROMPT, 0.78, 24.0)     # 33 chars, ~1.38 s
    script.append((2.51, "enter", ""))                # -> grill round 1
    script.append((4.17, "submit", fx.GRILL_ANSWER))  # cut: the answer
    script.append((4.88, "submit", "yes"))            # cut: create it
    # --- beats 2-3: the plan, then the agents ------------------------------ #
    # One extra refresh on the beat boundary so the card is in Working the
    # instant beat 2 begins — `_create_task` refreshes at 4.88, before the
    # first stage lands.
    script.append((5.16, "refresh", ""))
    script += [(start + 0.05, "refresh", "") for start, *_ in fx.STAGES[1:]]
    # --- beat 4: the diff -------------------------------------------------- #
    # Typed on camera, at the same cadence as `/approve`, because the command
    # IS the answer to "can I see the diff in the terminal?" — the shell has
    # one, and this is it.
    script += _typing("/diff", 14.52, 11.3)           # 5 chars, ~0.44 s
    script.append((15.06, "enter", ""))
    script.append((15.30, "scroll", str(DIFF_SCROLL_Y)))
    # --- beat 5: the evidence, then the one step that stays yours ---------- #
    # `/logs` replays the whole trail into the same pane, ending on the four
    # lines that ARE the evidence: tests, tamper guard, lint, PR.
    script.append((19.10, "submit", "/logs"))
    script += _typing("/approve", 21.66, 11.3)        # 8 chars, ~0.71 s
    script.append((fx.APPROVE_AT + 0.05, "enter", ""))
    script.append((24.40, "refresh", ""))
    return sorted(script, key=lambda s: s[0])


# --------------------------------------------------------------------------- #
# Capture                                                                      #
# --------------------------------------------------------------------------- #

def screen_html(app) -> str:
    """The composited screen as Rich HTML — the same path `save_screenshot`
    takes to SVG, diverted to HTML so we own the cell size."""
    width, height = app.size
    console = Console(width=width, height=height, file=io.StringIO(),
                      force_terminal=True, color_system="truecolor",
                      record=True, legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(
        full=True, screen_stack=app.app._background_screens, simplify=False))
    return console.export_html(inline_styles=True, code_format="{code}")


async def capture_html() -> list[str]:
    from no_human.cli.shell import ShellApp

    clock = FrameClock()
    app = ShellApp(ScriptedClient(clock), repo_path=fx.REPO_PATH,
                   poll_interval=0,        # refreshes are scripted, not timed
                   follow_reconnect=0)     # one pass; the fake never closes
    script = build_script()
    out: list[str] = []

    async with app.run_test(size=(COLS, ROWS)) as pilot:
        for _ in range(3):
            await pilot.pause()
        # The prompt's cursor blinks on a WALL-CLOCK timer, and this recorder
        # steps in frames, not seconds — so the blink phase at frame n depended
        # on how long the machine took to get there. Measured: 119 of 360
        # frames differed between two runs of the identical script, all of them
        # the cursor. Off, the two runs are byte-identical.
        from textual.widgets import Input
        app.query_one("#prompt", Input).cursor_blink = False
        await pilot.pause()
        cursor = 0
        for frame in range(N_FRAMES):
            t = t_of(frame)
            await clock.advance(frame)
            while cursor < len(script) and script[cursor][0] <= t:
                _, kind, payload = script[cursor]
                cursor += 1
                if kind == "key":
                    await pilot.press(payload)
                elif kind == "enter":
                    await pilot.press("enter")
                elif kind == "submit":
                    from textual.widgets import Input
                    app.query_one("#prompt", Input).value = payload
                    await pilot.pause()
                    await pilot.press("enter")
                elif kind == "refresh":
                    app.action_refresh_board()
                elif kind == "scroll":
                    from textual.widgets import RichLog
                    app.query_one("#detail", RichLog).scroll_to(
                        y=int(payload), animate=False)
            # Three pumps: worker wake -> await chain -> repaint. Two was
            # enough for the input but lost the first line of a grill burst.
            for _ in range(3):
                await pilot.pause()
            out.append(screen_html(app))
    return out


async def render(html_frames: list[str], out_dir: Path) -> None:
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=1)
        await page.set_content(PAGE.format(bg=BG, fs=50, lh=CELL_H,
                                           pad_x=PAD_X, pad_y=PAD_Y))
        ratio = await page.evaluate(
            "document.getElementById('probe').getBoundingClientRect().width/1000")
        font_size = round(CELL_W / ratio, 4)
        print(f"[cli] Menlo advance {ratio:.5f} em -> font-size {font_size} px "
              f"for a {CELL_W}px cell")
        await page.set_content(PAGE.format(bg=BG, fs=font_size, lh=CELL_H,
                                           pad_x=PAD_X, pad_y=PAD_Y))
        for i, html in enumerate(html_frames):
            await page.evaluate("h => document.getElementById('s').innerHTML = h", html)
            await page.screenshot(path=str(out_dir / f"f{i:04d}.png"))
        await browser.close()


async def main(out_dir: Path) -> None:
    frames = await capture_html()
    print(f"[cli] {len(frames)} screens captured")
    await render(frames, out_dir)
    print(f"[cli] frames -> {out_dir}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/frames-cli")))
