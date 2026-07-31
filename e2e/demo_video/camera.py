"""Timing, and the spotlight that replaced the crop.

Both clips are 26.000 s, 30 fps, 780 frames, frame-locked to each other: beat n
starts on the same frame index in both, so a viewer watching one after the
other sees one story told twice, not two clips drifting apart.

    0.00-0.60   fade up from black
    0.60-5.20   beat 1  you describe it, it writes the spec
    5.20-9.80   beat 2  the plan
    9.80-14.40  beat 3  the agents, running
    14.40-19.00 beat 4  the diff
    19.00-25.00 beat 5  cited evidence, then approve
    25.00-26.00 fade to black, hold

Frame 0 and frame 779 are both pure black, so the loop seam is invisible.

WHY FIVE BEATS, AND WHY THE LAST ONE IS LONGER
==============================================
The four-beat cut showed: file it / it works / it proves / approve. "It works"
was the board's three lanes with a card crossing them — true, but it is the
product's OUTPUT, and the operator asked to see its WORK: the plan and specs it
writes, the agents running, and the diff. Those are three surfaces, and the
lanes beat is what paid for them: with the whole window in frame the lanes are
visible behind every beat anyway (they are what beat 1 opens on, and the board
is the frame the drawer sits in), so "many tasks at once" survives without a
beat of its own. Four beats became five, not seven.

The four beats before the last are 4.6 s, unchanged, which is why beat 1's
script — the typing cadence, the grill's frame spacing, the two jump cuts —
did not have to be re-derived at all. The LAST beat carries two things that
used to have a beat each (the review evidence, and the approval) and it gets
6.0 s for them: 3.0 s to read the verdict and its four cited rows, a drift, and
2.4 s of the approved state before the fade. Splitting them back into two beats
would have made a 29.2 s loop; compressing them into 4.6 s would have left the
payoff 1.6 s on screen. Neither is a trade worth making, and the beat grid is
not required to be uniform — only followable (BEAT_LEN_MIN).

WHAT CHANGED, AND WHY THE OLD ARITHMETIC NO LONGER APPLIES
==========================================================
The first cut of these clips was a series of CROPS: the frame zoomed into one
region per beat and the rest of the app was off camera. That was forced by the
display size — the pair sat in a two-column grid inside a 1240 px `.wrap` and
each clip rendered at 607 CSS px, so a 10 px card id landed at 5 effective px
unless the camera magnified it.

The clips are now stacked one per row and render at 1240 CSS px (measured in a
browser at a 1440 px viewport), and the whole legibility budget scales with the
display width. Full frame is readable at that size, so full frame is what both
clips show — which is what they should have shown all along: "the board" and
"the shell" are products, and a product you only ever see one tenth of at a
time is not on camera.

    effective CSS px = source CSS px * 1240 / capture CSS width

Both clips capture 2560x1440 device px, and both fill the player, so the one
number that sets everything is 1240/1280 = 0.969 for the board (captured at
1280 CSS logical) and, for the terminal, the monospace closed form (a 0.6-em
advance, COLS advances spanning the 2500 of 2560 px the grid occupies):

    board, full frame     15 px card title    -> 14.5 effective px
                          13 px checklist     -> 12.6
                          12.5 px diff line   -> 12.1   (beat 4)
                          11 px lane model    -> 10.7   (beat 3)
                          10 px card id       ->  9.7
    shell, full frame    100 columns          -> 20.2 effective px

The board's smallest type (the 10 px `.card-id`) is the one number that is
smaller than it was under the old beat-2 crop (12.6). That is the deliberate
trade: the id is legible on a retina screen at 9.7 CSS px, and showing the
whole board is worth more than magnifying eight hex characters. Everything a
viewer must READ — titles, the review verdict, the checklist rows, every line
of the diff, every line of the terminal — clears 12 px.

The one number that does NOT clear 12 is the System view's per-lane model label
(`.fx-model`, 11 px CSS -> 10.7 effective). It is the smallest thing beat 3
asks anyone to read, and there is no framing lever left: the spotlight does not
magnify, and the only way up is a narrower capture, which would shrink the
window this cut exists to show whole. 10.7 CSS px is ~21 device px on a retina
display and `claude-sonnet-5` is legible at it; the ROLE names beside it
(`.fx-role-name`, 13.5 -> 13.1) and the stage labels (`.fx-label`, 14.5 -> 14.0)
carry the beat, and the model is the detail that rewards a second look.

What this costs on a phone: at a 390 px viewport the player is ~350 CSS px, so
the terminal reads at 5.7 effective px, the board's body type at 3.6 — and the
CAPTION BAR, which is part of the frame and not page chrome, at 25 * 350/1600 =
5.5. So the caption does not rescue mobile either; it is the same 0.219x as
everything else in the frame. There is no framing that is both "the whole
screen" and phone-legible, and no caption size that is right at both ends (the
25 px that reads as a caption on desktop would have to be ~55 px to clear 12 on
a phone, which on desktop is a headline). The page therefore stops pretending
below 900 px: it prints the five beats as text (`.demo-beats` in the site's
index.html) and the clips run as an impression above it.

DIRECTING THE EYE WITHOUT A CROP
================================
With no crop, nothing points at the part of the frame that just changed. So
each beat carries a SPOTLIGHT: the frame stays whole and fully visible, and
everything outside the beat's region is multiplied to DIM_LUMA. The mask is a
rectangle blurred by SPOT_BLUR, so the edge is a soft falloff rather than a
box, and the rectangle drifts between beats over SPOT_DRIFT seconds instead of
cutting — a cut in a full frame reads as a flash.

Under it sits a caption bar, BAR_H px of chrome below the app, naming the beat
("1 / 4 - You file the ticket, in plain English"). A silent autoplaying clip
has no narration; on-screen text is the only thing that can carry the story.
It is a bar rather than an overlay so it can never cover the app — the terminal
clip's fourth beat is the prompt line at the bottom of the screen, which is
exactly where an overlaid caption would have sat.

NOTES ON THE REAL SURFACES (all measured, all still true)
=========================================================
* `[black on bright_yellow]` paints nothing under Textual 8.2.7 — neither the
  foreground nor the background lands, so the "! NEEDS YOU" bar and the gate
  lane headers render as plain text. Nothing may lean on that bar.
* Neither surface has a sub-minute clock. Board.jsx `relativeTime` floors to
  whole minutes ("<1m" under 60 s) and the shell renders no elapsed time at
  all. The synchronising quantity between the two clips is the 8-character task
  id (identical in both) plus the pipeline STATUS, which does advance in
  lockstep — a progress bar on the board, a word in the shell.
* The shell's conversation pane WRAPS (shell.py `min_width=0`); it used to
  truncate. Nothing in this harness needs to keep lines short any more.
"""

from __future__ import annotations

FPS = 30
DURATION = 26.0
N_FRAMES = int(round(FPS * DURATION))  # 780

#: The app area, and the caption bar under it. 1600x900 is 16:9 and matches
#: both captures exactly (2560x1440 -> 0.625), so nothing is stretched; the bar
#: makes the delivered frame 1600x960. At the ~1176 CSS px the site renders
#: these at, 1600 wide is 1.36x — still oversampled on a retina display.
APP_W, APP_H = 1600, 900
BAR_H = 60
OUT_W, OUT_H = APP_W, APP_H + BAR_H   # 1600 x 960, both even for yuv420p

#: Everything outside a beat's spotlight is multiplied by this. 0.58 is dark
#: enough to rank the frame and light enough that the dimmed text is still
#: read-able rather than blanked — a viewer who looks at the "wrong" pane must
#: not find a black hole there.
DIM_LUMA = 0.58
#: Gaussian sigma on the spotlight mask, in OUTPUT px. A hard-edged rectangle
#: over a screenshot looks like a redaction; 30 px of falloff reads as light.
SPOT_BLUR = 30
#: Seconds the spotlight takes to travel to the next beat's region.
SPOT_DRIFT = 0.55

BEAT_LEN = 4.6
#: The shortest a beat may be and still be followable. The four-beat cut before
#: this one made every beat exactly BEAT_LEN; the grid is now 4.6, 4.6, 4.6,
#: 4.6, 6.0 and this is the invariant that survived — "a viewer can keep track",
#: not "every beat is the same length".
BEAT_LEN_MIN = 4.0
#: Beat 5 carries the review evidence AND the approval, which were a beat each
#: before. See the module docstring for why they merged and why this is longer.
LAST_BEAT_LEN = 6.0

FADE_IN_END = 0.60
BEAT_1 = 0.60
BEAT_2 = BEAT_1 + BEAT_LEN      # 5.20
BEAT_3 = BEAT_2 + BEAT_LEN      # 9.80
BEAT_4 = BEAT_3 + BEAT_LEN      # 14.40
BEAT_5 = BEAT_4 + BEAT_LEN      # 19.00
FADE_OUT_START = BEAT_5 + LAST_BEAT_LEN   # 25.00
FADE_OUT_END = 25.40

BEATS = (BEAT_1, BEAT_2, BEAT_3, BEAT_4, BEAT_5)

#: The burned-in caption per beat: (step number, text). Same in both clips —
#: they are the same five moments, and a viewer who watches both should be told
#: so. Rendered by captions.py with the product's own two families.
#:
#: Every line has to be TRUE OF BOTH SURFACES, which is the whole constraint on
#: the wording: the board and the shell are not the same app and do not have the
#: same views. Beat 2 says "the plan it will be held to" and not "the files, the
#: approach, the test plan", because those three headings are the board's Spec
#: section and the shell has no equivalent — its plan is the refined spec and
#: its acceptance criteria, still sitting in the transcript. Beat 3 names the
#: roles rather than the models, because the board prints a model per lane and
#: the shell prints one `models` line that scrolls. Beat 4 is the one the two
#: surfaces show the same way: `/diff` and the Diff section are the same bytes.
CAPTIONS: tuple[tuple[str, str], ...] = (
    ("1 / 5", "You describe it. It asks back, then writes the spec"),
    ("2 / 5", "Before any code: the plan it will be held to"),
    ("3 / 5", "Five agents, one job each - planner, coder, supervisor, reviewer"),
    ("4 / 5", "The diff it actually wrote, line by line"),
    ("5 / 5", "Cited evidence - then the one step that stays yours"),
)


def t_of(frame: int) -> float:
    return frame / FPS


def beat_index(t: float) -> int:
    """0-based index of the beat covering ``t``; the first beat before it
    starts, the last one after the fade begins."""
    idx = 0
    for i, start in enumerate(BEATS):
        if t >= start:
            idx = i
    return idx


def fade_at(t: float) -> float:
    """Multiplier on the frame's luma. 0 at both ends so the loop seam is a
    black-to-black cut."""
    if t <= 0:
        return 0.0
    if t < FADE_IN_END:
        return t / FADE_IN_END
    if t < FADE_OUT_START:
        return 1.0
    if t < FADE_OUT_END:
        return 1.0 - (t - FADE_OUT_START) / (FADE_OUT_END - FADE_OUT_START)
    return 0.0


def ease(x: float) -> float:
    """smoothstep — the spotlight's travel between beats."""
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def lerp(a: tuple[float, ...], b: tuple[float, ...], k: float) -> tuple[float, ...]:
    return tuple(p + (q - p) * k for p, q in zip(a, b))


def focus_at(keyframes: tuple, t: float) -> tuple[float, float, float, float]:
    """The spotlight rect (x, y, w, h) at time ``t``, in CAPTURE device px.

    A keyframe is (start, x, y, w, h). The rect eases from the previous
    keyframe's rect to this one over :data:`SPOT_DRIFT` seconds starting at
    ``start`` — every move drifts, because in a full frame a cut reads as a
    flash rather than as a camera move.
    """
    prev = keyframes[0]
    for kf in keyframes:
        if t >= kf[0]:
            prev = kf
        else:
            break
    index = keyframes.index(prev)
    rect = tuple(prev[1:])
    if index == 0:
        return rect  # type: ignore[return-value]
    k = ease((t - prev[0]) / SPOT_DRIFT)
    return lerp(tuple(keyframes[index - 1][1:]), rect, k)  # type: ignore[return-value]


def to_output(rect: tuple[float, float, float, float],
              frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """A capture-space rect mapped into the 1600x900 app area, clamped to it
    and rounded to even sides (every ffmpeg filter here prefers them)."""
    sx, sy = APP_W / frame_w, APP_H / frame_h
    x, y, w, h = rect[0] * sx, rect[1] * sy, rect[2] * sx, rect[3] * sy
    x = max(0.0, min(x, APP_W))
    y = max(0.0, min(y, APP_H))
    w = max(2.0, min(w, APP_W - x))
    h = max(2.0, min(h, APP_H - y))
    return (int(x) & ~1, int(y) & ~1, int(w) & ~1, int(h) & ~1)
