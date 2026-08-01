"""The narration: one script, one timing map, two videos.

This module is the SINGLE SOURCE OF TRUTH for the narrated sprint demo. The
sprint fixture (sprint.py) times every status change off these beats, both
recorders (sprint_gui.py, sprint_cli.py) derive every scripted step from them,
the voiceover (voiceover.py) places every rendered line at its start second,
and compose.py cuts both clips to `N_FRAMES` and muxes the same audio over
each. Nothing else holds a copy of a time: a beat that moves here moves
everywhere, which is the only way "the same audio works over both recordings"
stays true after an edit.

The human-readable version of this file is `narration.md` — the script as a
VO artist would read it, plus the timing map as a table. A test asserts the
two agree line for line, so the doc cannot drift from the data.

THE COPY, AND WHY IT SOUNDS LIKE THIS
=====================================
Written to be read aloud. Short declarative sentences, one claim per line,
no filler adjectives — a senior engineer hears "revolutionary" and closes the
tab, so every line names a thing that is literally on screen while it is
spoken: the tickets, the plan, the live edits, the reviewer's citations, the
five PRs, the Approve click. The one repeated promise ("the agent never
merges — that click stays yours") is the product's actual hard rule, and it is
the close because it is the differentiating claim, not a safety disclaimer.

Structure (the operator's brief, beat for beat):

    hook      the sprint pain — ten tickets, one engineer
    handoff   five of them, handed over; the whole handoff is a tag
    parallel  five agents running at once; plans first; live edits
    gate      the independent reviewer that refuses to rubber-stamp
    payoff    five PRs with evidence; the human approves
    close     the name, and the one-line promise

TIMING
======
78.000 s at 30 fps — inside the 60-90 s brief with air around every line.
Every `Line.start` is when the voice starts speaking; the line's WINDOW runs
to the next line's start minus `LINE_GAP` (the last line's, to the fade). The
voiceover build measures each rendered line and refuses to assemble a track
where any line overruns its window — audio that outruns its beat is the
narrated-demo version of typing that outruns its beat, and it is caught at
build time, not on camera.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FPS = 30
DURATION = 78.0
N_FRAMES = int(round(FPS * DURATION))   # 2340

FADE_IN_END = 0.60
FADE_OUT_START = 76.60
FADE_OUT_END = 77.40

#: Breathing room between the end of one line's window and the next line's
#: start. Also the minimum silence after a line if it fills its window.
LINE_GAP = 0.35


@dataclass(frozen=True)
class Line:
    """One narrated sentence (or two short ones) with its place in time.

    ``text`` is what the doc and the report print; ``spoken`` is what the TTS
    is handed when the written form would be misread aloud ("no_human" must be
    spoken "no human", not "no underscore human").
    """
    start: float
    text: str
    spoken: str = ""

    @property
    def tts_text(self) -> str:
        return self.spoken or self.text


@dataclass(frozen=True)
class Beat:
    """One movement of the piece: a name, a start, what each surface shows.

    ``gui`` and ``cli`` are the timing map's "what's on screen" columns —
    they are documentation with teeth: the recorders' scripted steps are
    asserted (in tests) to land inside the beat whose description they serve.
    """
    name: str
    start: float
    caption: str
    gui: str
    cli: str
    lines: tuple[Line, ...] = field(default_factory=tuple)


BEATS: tuple[Beat, ...] = (
    Beat(
        "hook", 0.60,
        caption="Sprint planning left you ten tickets",
        gui="The board, quiet: one leftover PR in Review, empty Working lane",
        cli="The shell, quiet: same one leftover task in its Review lane",
        lines=(
            Line(0.80, "Monday. Sprint planning just handed you ten tickets."),
            Line(4.40, "A webhook bug. A flaky test. "
                       "A refactor nobody wants to touch."),
        ),
    ),
    Beat(
        "handoff", 9.00,
        caption="Five of them go to no_human",
        gui="Five cards land in Queued/Working, Jira keys in the titles",
        cli="The same five tasks fill the shell's lanes, same keys",
        lines=(
            Line(9.40, "Pick five. Hand them to no_human.",
                 spoken="Pick five. Hand them to no human."),
            Line(12.40, "Tag them in your tracker, or paste them in. "
                        "That's the whole handoff."),
        ),
    ),
    Beat(
        "parallel", 17.00,
        caption="Five agents run your tickets in parallel",
        gui="Statuses tick on every card; the drawer opens on the webhook "
            "fix: its written plan, then the live agent lanes",
        cli="The webhook task is selected; its event stream fills the detail "
            "pane - models, plan, edits, tests",
        lines=(
            Line(17.60, "Five agents start. In parallel, on real branches."),
            Line(21.80, "Each one writes a plan before it writes code. "
                        "Files, approach, tests. It gets held to that plan."),
            Line(29.20, "The webhook fix is already editing code. "
                        "The flaky test is being reproduced, not guessed at."),
            Line(35.40, "Nothing here waits for you."),
        ),
    ),
    Beat(
        "gate", 38.00,
        caption="An independent reviewer refuses to rubber-stamp",
        gui="The drawer shows the diff, then the review: PASSED, with "
            "evidence cited file:line; the drawer closes on the refactor, "
            "bounced back to Working for a second round",
        cli="/diff prints the same diff in the terminal; the refactor's "
            "card drops back to Working, then returns",
        lines=(
            Line(38.60, "Then the gate. An independent reviewer, in a fresh "
                        "context, with one brief: prove this task is not "
                        "done."),
            Line(46.20, "It reads the diff line by line, and cites its "
                        "evidence."),
            Line(50.60, "The refactor came back once. It went again. "
                        "No rubber stamps."),
        ),
    ),
    Beat(
        "payoff", 56.00,
        caption="Five pull requests, waiting on you",
        gui="All five cards sit in Review with PR links; the drawer opens "
            "on the webhook fix and Approve is clicked",
        cli="/logs replays the evidence trail - tests, tamper guard, lint, "
            "PR; /approve is typed and answered",
        lines=(
            Line(56.40, "The result: five pull requests, each with tests "
                        "and review evidence attached."),
            Line(62.20, "The agent never merges. That click stays yours."),
            Line(66.20, "You read. You approve."),
        ),
    ),
    Beat(
        "close", 70.00,
        caption="no_human - your sprint, in parallel",
        gui="The drawer closes; the whole board, five approved-or-waiting "
            "PRs, holds to the fade",
        cli="The whole shell - full lanes, quiet prompt - holds to the fade",
        lines=(
            Line(70.60, "no_human. Your sprint, run in parallel. "
                        "You keep the merge button.",
                 spoken="no human. Your sprint, run in parallel. "
                        "You keep the merge button."),
        ),
    ),
)

LINES: tuple[Line, ...] = tuple(ln for beat in BEATS for ln in beat.lines)


def beat_at(t: float) -> Beat:
    """The beat covering ``t`` — the first one before it starts, the last
    one after it ends."""
    current = BEATS[0]
    for beat in BEATS:
        if t >= beat.start:
            current = beat
    return current


def line_windows() -> tuple[tuple[Line, float], ...]:
    """Each line with the seconds it may occupy: to the next line's start
    minus :data:`LINE_GAP`; the last line runs to the fade-out."""
    out = []
    for i, line in enumerate(LINES):
        end = LINES[i + 1].start if i + 1 < len(LINES) else FADE_OUT_START
        out.append((line, end - LINE_GAP - line.start))
    return tuple(out)


def t_of(frame: int) -> float:
    return frame / FPS


def fade_at(t: float) -> float:
    """Same contract as camera.fade_at, on this cut's own clock."""
    if t <= 0:
        return 0.0
    if t < FADE_IN_END:
        return t / FADE_IN_END
    if t < FADE_OUT_START:
        return 1.0
    if t < FADE_OUT_END:
        return 1.0 - (t - FADE_OUT_START) / (FADE_OUT_END - FADE_OUT_START)
    return 0.0
