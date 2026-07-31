"""Record both demo videos end to end.

    source .venv/bin/activate
    cd web && npm run build && cd ..          # once, if web/dist is stale
    PYTHONPATH=src python -m e2e.demo_video.record --out ~/git/nemlot-site/assets

Produces demo-gui.{webm,mp4}, demo-cli.{webm,mp4} and a .jpg poster for each,
all 1600x960, 26.000 s, 30 fps, 780 frames, silent, frame-locked to each other.
Add --skip-capture to re-cut from frames already on disk (the spotlight and the
caption are applied at cut time, so retuning either costs seconds, not a
re-record).

Nothing here talks to a real server, a database or an LLM: see fixture.py.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from . import captions, cli_frames, encode, gui_frames
from .camera import BEAT_2, BEAT_3, BEAT_4, BEAT_5, N_FRAMES

WORK = Path("/tmp/nh-demo-video")

#: When both clips stop pointing anywhere and open out to the whole screen at
#: full brightness. It is fx.APPROVE_AT plus most of a second: the frame
#: lighting up IS the payoff, and the last 1.6 s then show the finished state
#: everywhere at once — the lane, the transcript, the PR — before the fade.
PULL_BACK = 23.40

#: When the last beat stops looking at the evidence and travels to the button.
#: Inside beat 5, 0.7 s before the click, so the light is settled on the bar
#: when it happens rather than chasing it.
APPROACH_APPROVE = 21.90

# --------------------------------------------------------------------------- #
# The spotlight, per clip: (start_t, x, y, w, h)                                #
# Rects are in each clip's own CAPTURE resolution — both 2560x1440 — and are    #
# what stays at full brightness while the rest of the frame is multiplied to    #
# camera.DIM_LUMA. The whole app is in frame at all times; the rect only says   #
# where to look. Every move eases over camera.SPOT_DRIFT.                       #
# --------------------------------------------------------------------------- #

# Board capture is 2560x1440 (1280x720 CSS at dsf 2), so a CSS px is 2 device
# px and every rect below is a MEASURED CSS rect doubled — read off a live
# board by `_probe_rects.py`, not eyeballed off a frame. Re-run that after any
# change to the drawer's layout or to the fixture, and paste the numbers back.
GUI_FOCUS = (
    # beat 1 — NO spotlight. The composer is a modal and the product draws its
    # own scrim over the board behind it (see any frame in 0.6-4.9 s): a second
    # dim multiplied on top of that crushes the board to flat black, which is
    # the one thing this re-cut exists to stop. The app is already pointing at
    # the dialog; the honest move is to let it.
    (0.00, 0, 0, 2560, 1440),
    # beat 2 — the plan. The drawer's Spec section: files to change, approach,
    # test plan, out of scope, verification. Measured: the section body is
    # CSS x 71.4 w 1137.2, and the drawer's scroller shows CSS y 302.8-625.2.
    (BEAT_2, 132, 596, 2296, 664),
    # beat 3 — the agents. `.fx-board`, the five-stage lane grid, measured at
    # CSS x 100 y 359.5 w 1080 h 262.8 once pinned. The tightest rect in either
    # clip, and the one real camera move in the middle of this one: the
    # "Running — Coding" banner sits just above it, outside the light and still
    # perfectly readable at DIM_LUMA, which is what a spotlight is for.
    (BEAT_3, 184, 704, 2192, 556),
    # beat 4 — the diff. Same scroller, different content: 14 lines of
    # `git diff`, the whole `mul()` hunk.
    (BEAT_4, 132, 596, 2296, 652),
    # beat 5 — the review block: the PASSED verdict line and the four checklist
    # rows with their cited `calc.py:10` chips. A shade wider than beat 4 (the
    # scroller, not the section) so the move is small but not nothing; the
    # content underneath it has changed completely, which is what a viewer is
    # actually tracking.
    (BEAT_5, 80, 600, 2400, 660),
    # ...then to the drawer's action bar, where Approve lives, plus the flash
    # that answers it ("Approval recorded. You merge the PR in your git host -
    # the agent never merges.").
    (APPROACH_APPROVE, 68, 1140, 2424, 248),
    # ...then open out, as the shell clip does, on the same frame.
    (PULL_BACK, 0, 0, 2560, 1440),
)

# Shell capture is 2560x1440: a 100x31 grid of 25x45 cells, padded 30 x 22.5.
# col c -> x = 30 + 25c ; row r -> y = 22.5 + 45r
GUI_CAPTURE = (gui_frames.DEV_W, gui_frames.DEV_H)


def _cells(c0: int, r0: int, c1: int, r1: int) -> tuple[int, int, int, int]:
    """A rect covering terminal columns [c0, c1) and rows [r0, r1), in the
    shell capture's device px. Expressing the shell's spotlight in CELLS rather
    than pixels is the only way it survives a change of cell size."""
    x0 = cli_frames.PAD_X + c0 * cli_frames.CELL_W
    y0 = cli_frames.PAD_Y + r0 * cli_frames.CELL_H
    return (int(x0), int(y0),
            int((c1 - c0) * cli_frames.CELL_W), int((r1 - r0) * cli_frames.CELL_H))


# Cell rects below are the widgets' OWN regions, read off a live ShellApp at
# 100x31 rather than eyeballed:
#   header 0,0 100x1 | lanes-scroll 0,1 45x27 | conversation 45,1 55x17
#   detail 45,18 55x10 | prompt 0,28 100x3 | footer 0,30 100x1
#: WHAT THE SHELL CAN AND CANNOT SHOW, since this is where it is decided.
#:
#: The two clips tell the same story through their own surfaces, and for three
#: of the five beats the shell has a real one:
#:
#:   beat 1  the grill exchange and the refined spec, in the conversation pane.
#:           Identical in substance to the board's composer.
#:   beat 4  `/diff` (shell_input.py SLASH_COMMANDS) fetches the same
#:           `GET /api/tasks/{id}/diff` the board's Diff section does, and
#:           prints the same bytes. Not an equivalent — the same thing.
#:   beat 5  `/logs` replays the orchestrator's trail (tests, tamper guard,
#:           lint, commit, PR) and `/approve` is the approval.
#:
#: Two beats it does NOT have an equal of, and this cut does not pretend:
#:
#:   beat 2  there is no `/spec` and no plan view. The shell's plan is the
#:           refined spec the grill produced — title, description, acceptance
#:           criteria — which is still in the transcript, and that is what this
#:           beat points at. What the board shows and the shell cannot is the
#:           PLANNER's spec: files to change, approach, test plan, out of
#:           scope, verification. That is a product gap, not a framing one.
#:   beat 3  there is no agent graph, and `shell.py:_format_event` prints an
#:           event's kind and text and DROPS its `source` — so a planner's tool
#:           call and the coder's render identically, as `-> Edit calc.py`. The
#:           roles that do reach the pane are the ones whose names are in the
#:           kind or the text: `models` (one line naming all four models),
#:           `supervisor_decision`, and the subagent lifecycle. That is what
#:           this beat points at, and it is thinner than the board's.
CLI_FOCUS = (
    # beat 1 — the conversation pane on the right: what you typed, what the
    # grill asked back, and the spec it produced.
    (0.00, *_cells(45, 1, 100, 18)),
    # beat 2 — the bottom of that same pane, which by now holds the spec check,
    # the refined spec, its three acceptance criteria and the created task id.
    # A tighter rect inside the previous one: the camera moves in on the part of
    # the transcript the caption is about. Row 6 and not row 8, counted off a
    # rendered frame: the pane's content starts on terminal row 2, so `refined
    # spec:` is on row 7 and a rect starting at 8 would light everything the
    # spec produced while leaving the words "refined spec" outside it.
    (BEAT_2, *_cells(45, 6, 100, 18)),
    # beat 3 — the detail pane, where the followed task's event stream lands:
    # the `models` line, the supervisor's decisions, a subagent starting and
    # finishing under the coder.
    (BEAT_3, *_cells(45, 18, 100, 28)),
    # beat 4a — the prompt line, full width, while `/diff` is typed into it.
    # The command is half the answer to "can I see the diff from a terminal?".
    (BEAT_4, *_cells(0, 27, 100, 31)),
    # beat 4b — ...and back up to the detail pane, which now holds it.
    (15.30, *_cells(45, 18, 100, 28)),
    # beat 5a — the same pane; `/logs` replaces the diff with the evidence
    # trail. No move, because the subject has not moved — only what it says.
    (BEAT_5, *_cells(45, 18, 100, 28)),
    # beat 5b — the prompt line again, while /approve is typed. This is the one
    # moment in the clip where the human acts, and the prompt is where they act.
    (APPROACH_APPROVE, *_cells(0, 27, 100, 31)),
    # ...then open all the way out as the approval registers. The clip ends on
    # the whole screen, which is also where the next loop begins, so the seam
    # is a fade between two identical framings.
    (PULL_BACK, *_cells(0, 0, 100, 31)),
)

CLI_CAPTURE = (cli_frames.VIEW_W, cli_frames.VIEW_H)

#: Poster frames — the one still that has to sell the clip before it plays.
#: Both land inside beat 5, on the evidence, which is the frame that argues the
#: product rather than merely showing it. (The board's is also the page's
#: `og:image`, so it is the frame a shared link renders.)
GUI_POSTER = int(round((BEAT_5 + 1.6) * 30))   # t=20.60, the verdict + checklist
CLI_POSTER = int(round((BEAT_5 + 2.4) * 30))   # t=21.40, the full evidence trail


async def capture(work: Path, which: str) -> None:
    if which in ("gui", "both"):
        await gui_frames.main(work / "frames-gui")
    if which in ("cli", "both"):
        await cli_frames.main(work / "frames-cli")


def cut(work: Path, out: Path, which: str = "both") -> None:
    out.mkdir(parents=True, exist_ok=True)
    bars = asyncio.run(captions.main(work / "bars"))
    # The terminal clip is dearer to encode than the board at the same quality:
    # it is wall-to-wall high-contrast glyph edges, and VP9 spends bits on every
    # one. It therefore gets its own crf pair. Both were tuned against the
    # ceilings in encode.py at 1600x960, not inherited from the 12 s cut, and
    # deliberately NOT raised for the 26 s recut — see encode.py's budget note:
    # the extra duration was paid for out of the encoder's presets, not out of
    # the quality of the type a viewer is being asked to read.
    plan = [
        ("gui", GUI_FOCUS, GUI_CAPTURE, GUI_POSTER, 36, 25),
        ("cli", CLI_FOCUS, CLI_CAPTURE, CLI_POSTER, 40, 26),
    ]
    # `--only cli` has to reach the CUT as well as the capture: re-cutting one
    # clip must not require the other clip's frames to still be on disk, or a
    # one-clip fix silently forces a re-record of the accepted one.
    if which != "both":
        plan = [p for p in plan if p[0] == which]
    problems = []
    for name, focus, dims, poster_frame, webm_crf, mp4_crf in plan:
        frames = work / f"frames-{name}"
        n = len(list(frames.glob("f*.png")))
        if n != N_FRAMES:
            raise SystemExit(f"{frames} holds {n} frames, expected {N_FRAMES}")
        stage = work / f"cut-{name}"
        if stage.exists():
            shutil.rmtree(stage)
        encode.compose(frames, stage, focus, dims, bars)
        sizes = encode.encode(stage, out / f"demo-{name}",
                              webm_crf=webm_crf, mp4_crf=mp4_crf)
        pjpg = out / f"demo-{name}-poster.jpg"
        psize = encode.poster(stage, pjpg, poster_frame)
        print(f"[{name}] webm {sizes['webm']:,} B   mp4 {sizes['mp4']:,} B   "
              f"poster {psize:,} B")
        if sizes["webm"] > encode.MAX_WEBM:
            problems.append(f"{name}.webm {sizes['webm']:,} > {encode.MAX_WEBM:,}")
        if sizes["mp4"] > encode.MAX_MP4:
            problems.append(f"{name}.mp4 {sizes['mp4']:,} > {encode.MAX_MP4:,}")
    total = sum(f.stat().st_size for f in out.glob("demo-*"))
    print(f"[total] {total:,} B across {len(list(out.glob('demo-*')))} files")
    if total > encode.MAX_TOTAL:
        problems.append(f"total {total:,} > {encode.MAX_TOTAL:,}")
    if problems:
        raise SystemExit("CEILING EXCEEDED:\n  " + "\n  ".join(problems))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--work", default=WORK, type=Path)
    ap.add_argument("--only", choices=["gui", "cli", "both"], default="both")
    ap.add_argument("--skip-capture", action="store_true")
    args = ap.parse_args(argv)

    if not args.skip_capture:
        asyncio.run(capture(args.work, args.only))
    cut(args.work, args.out, args.only)


if __name__ == "__main__":
    main(sys.argv[1:])
