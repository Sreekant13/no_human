"""Guards for the demo-video harness (e2e/demo_video).

The videos themselves need a browser and ten minutes, so they are not built
here. What IS checked is everything that can silently rot and only show up as a
bad frame months later:

* the two clips are describing the same ticket and the same five moments
* every line scripted for a shell pane still FITS it — a line that grows past
  the pane wraps on camera, with no error anywhere
* the legibility arithmetic the framing was built on still holds
* the spotlight actually points at something, and the caption names every beat
* every event frame is one the SHIPPED serialisers would actually emit, and
  every line the review cites is a line of the diff the next beat shows
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from e2e.demo_video import camera, captions, fixture as fx  # noqa: E402
from e2e.demo_video import cli_frames, gui_frames, record  # noqa: E402

#: Usable columns in the shell's conversation and detail panes at a 100-column
#: terminal. Read off a live `ShellApp` at size=(100, 31): both RichLogs get
#: `region` 55 wide, and `content_size` 51 after the round border (2) and the
#: widget's `padding: 0 1` (2); one more column goes to the scrollbar whenever
#: the pane has scrolled, which it always has by the time anything long is in
#: it. 50 is that worst case.
#:
#: This was 40 at the old 84-column terminal, and the failure it guarded was
#: TRUNCATION — `RichLog.min_width` defaulted to 78 and the compositor cropped
#: the overhang. That is fixed (shell.py `min_width=0`) and the pane wraps, so
#: what this now guards is a line wrapping mid-phrase on camera. Every line
#: below was authored against the old 40 and clears the new bound easily; the
#: number is here so the next person who adds one has a bound to hit.
CONVERSATION_COLS = 50

#: The width the delivered clips are DISPLAYED at, in CSS px, on the marketing
#: site's desktop layout: one clip per row, stepped out of `.wrap`'s gutters, so
#: the full 1240 of `--wrap`. Measured in a browser at a 1440 px viewport. Every
#: legibility number below is a function of this and the capture width — nothing
#: is cropped any more, so the asset's own pixel count does not enter into it.
DISPLAY_CSS = 1240.0


def test_clip_is_twenty_six_seconds_at_thirty_fps():
    """20.000 s bought four beats. The fifth beat and the longer payoff cost
    six more seconds; `DURATION` is what the site's copy and every timing in
    record.py are derived from, so it is asserted rather than assumed."""
    assert camera.N_FRAMES == 780
    assert camera.FPS * camera.DURATION == camera.N_FRAMES


def test_frame_zero_and_frame_last_are_both_black():
    """The loop seam. `fade_at` is what the encoder's fade filter reproduces."""
    assert camera.fade_at(0.0) == 0.0
    assert camera.fade_at(camera.t_of(camera.N_FRAMES - 1)) == 0.0


def test_no_beat_is_too_short_to_follow():
    """The invariant is that a viewer can keep track, not that the grid is
    uniform.

    This asserted `all(span == BEAT_LEN)` while there were four equal beats.
    The grid is now 4.6, 4.6, 4.6, 4.6, 6.0 — the last beat carries the review
    evidence AND the approval, which used to be a beat each. Uniformity was
    never the property worth guarding; the floor is, and it is unchanged at
    4 s. The longer beat is asserted explicitly below so it cannot silently
    shrink back and re-compress the payoff.
    """
    beats = list(camera.BEATS)
    assert beats == sorted(beats)
    spans = [b - a for a, b in zip(beats, beats[1:] + [camera.FADE_OUT_START])]
    assert all(s >= camera.BEAT_LEN_MIN - 1e-9 for s in spans), spans
    assert camera.BEAT_LEN_MIN >= 4.0, "a beat under 4 s is not followable"
    assert spans[:-1] == [pytest.approx(camera.BEAT_LEN)] * 4
    assert spans[-1] == pytest.approx(camera.LAST_BEAT_LEN)
    assert camera.LAST_BEAT_LEN > camera.BEAT_LEN, (
        "the beat that carries both the evidence and the approval must be the "
        "longest one, or one of the two is on screen for under two seconds")


def test_the_delivered_frame_is_the_app_plus_the_caption_bar():
    """libx264 in yuv420p refuses an odd side, and the site's `aspect-ratio`
    is hard-coded to this shape — a change here without a change there
    letterboxes the player."""
    assert camera.OUT_W == camera.APP_W
    assert camera.OUT_H == camera.APP_H + camera.BAR_H
    assert camera.OUT_W % 2 == 0 and camera.OUT_H % 2 == 0
    assert camera.APP_W / camera.APP_H == 16 / 9, "the app area must be 16:9"


def test_both_captures_are_the_app_area_scaled_by_one_factor():
    """Both clips capture 16:9 at the same resolution, so both downscale into
    the app area by the same factor and their type comes out the same weight.
    A capture that is not 16:9 would letterbox or stretch in `compose`."""
    for w, h in ((gui_frames.DEV_W, gui_frames.DEV_H),
                 (cli_frames.VIEW_W, cli_frames.VIEW_H)):
        assert w / h == 16 / 9, (w, h)
        assert w >= camera.APP_W, "capturing below the delivered size upscales"


def test_both_clips_spotlight_every_beat():
    for focus in (record.GUI_FOCUS, record.CLI_FOCUS):
        starts = [f[0] for f in focus]
        assert starts == sorted(starts)
        for beat in (camera.BEAT_2, camera.BEAT_3, camera.BEAT_4, camera.BEAT_5):
            assert beat in starts, (beat, starts)


#: Keyframes that are ALLOWED to cover the whole frame, and why. Everything
#: else has to point somewhere or it is directing nothing.
#:   - record.PULL_BACK: the ending deliberately opens all the way out.
#:   - the board's beat 1: the composer is a modal and the product draws its own
#:     scrim; a second dim on top of it crushes the board to black.
FULL_FRAME_IS_DELIBERATE = {("cli", record.PULL_BACK),
                            ("gui", record.PULL_BACK),
                            ("gui", 0.00)}


def test_every_beat_spotlight_is_a_real_part_of_the_frame():
    """A spotlight that covers the whole frame directs nothing, and one that
    falls outside it points off camera. Both are silent failures."""
    for name, focus, dims in (("gui", record.GUI_FOCUS, record.GUI_CAPTURE),
                              ("cli", record.CLI_FOCUS, record.CLI_CAPTURE)):
        frame_w, frame_h = dims
        for start, x, y, w, h in focus:
            assert x >= 0 and y >= 0, (name, start, x, y)
            assert x + w <= frame_w and y + h <= frame_h, (name, start, x, y, w, h)
            area = (w * h) / (frame_w * frame_h)
            if (name, start) in FULL_FRAME_IS_DELIBERATE:
                continue
            assert 0.05 < area < 0.92, (name, start, area)
    # ...and the exception list must not quietly become the rule.
    assert len(FULL_FRAME_IS_DELIBERATE) == 3


def test_both_clips_end_on_the_whole_screen():
    """The last framing is the same as the first frame of the next loop, so
    the fade seam is not also a jump."""
    for focus, dims in ((record.GUI_FOCUS, record.GUI_CAPTURE),
                        (record.CLI_FOCUS, record.CLI_CAPTURE)):
        assert focus[-1][0] == record.PULL_BACK
        assert record.PULL_BACK < camera.FADE_OUT_START
        rect = camera.focus_at(focus, camera.FADE_OUT_START)
        area = (rect[2] * rect[3]) / (dims[0] * dims[1])
        assert area > 0.9, area


def test_there_is_a_caption_for_every_beat():
    assert len(camera.CAPTIONS) == len(camera.BEATS)
    for step, text in camera.CAPTIONS:
        assert step and text
        # The bar is 1600 px wide with 36 px gutters and ~120 px of step/rule/
        # progress furniture; DM Sans 500 at 25 px averages ~12.5 px a
        # character, so ~110 characters is where it would start to clip.
        assert len(text) <= 70, (len(text), text)
    n = len(camera.BEATS)
    assert [s for s, _ in camera.CAPTIONS] == [f"{i} / {n}" for i in range(1, n + 1)]


def test_the_progress_indicator_has_one_segment_per_beat():
    """The strip's dots were four hard-coded `<i>`s in the markup while the
    step read "1 / 4". Going to five beats without touching that would have
    burned "5 / 5" into a bar with four segments and the fourth lit — a
    progress indicator that is wrong about the progress, in every frame of the
    last beat, on both clips."""
    page = captions.PAGE % {"css": "x.css", "inline": "", "dots": "<i></i>" * 5}
    assert page.count("<i></i>") == 5
    assert "%(dots)s" in captions.PAGE, "the dots must not go back to markup"


def test_the_spotlight_drifts_rather_than_cuts():
    """Every move is eased across SPOT_DRIFT. Halfway through a move the rect
    must be strictly between the two framings — a cut in a full frame reads as
    a flash rather than as a camera move."""
    before = camera.focus_at(record.CLI_FOCUS, camera.BEAT_2 - 0.01)
    mid = camera.focus_at(record.CLI_FOCUS, camera.BEAT_2 + camera.SPOT_DRIFT / 2)
    after = camera.focus_at(record.CLI_FOCUS, camera.BEAT_2 + camera.SPOT_DRIFT)
    assert before != mid != after
    assert min(before[0], after[0]) <= mid[0] <= max(before[0], after[0])


# --------------------------------------------------------------------------- #
# The shared ticket                                                            #
# --------------------------------------------------------------------------- #

def test_the_hero_id_is_eight_readable_characters_in_both_surfaces():
    """The board prints `id.slice(0, 8)`, the shell prints `id[:8]`. If the two
    ever disagree the pair loses the only thing tying it together."""
    assert len(fx.HERO_ID) == 32
    assert fx.HERO_SHORT == fx.HERO_ID[:8]
    assert all(c in "0123456789abcdef" for c in fx.HERO_ID)


def test_the_ticket_does_not_exist_before_it_is_filed():
    assert fx.stage_at(0.0) is None
    assert fx.stage_at(camera.BEAT_1) is None
    assert fx.stage_at(camera.BEAT_2) is not None


def test_the_ticket_is_in_review_before_the_evidence_beat_opens():
    """Beat 5 spotlights the review checklist, which only exists once the task
    reaches awaiting_approval."""
    status, _live, _burn, _turns = fx.stage_at(camera.BEAT_5)
    assert status == "awaiting_approval"
    assert fx.hero_detail(camera.BEAT_5)["attempts"][0]["review_checklist"]


def test_the_plan_exists_when_the_plan_beat_lands_on_it():
    """Beat 2 is `SlideOver.jsx:SpecTab` rendering `task.context.spec`, and
    that section prints "Spec not generated yet" for pending/context/planning.
    A stage grid that leaves the task in `planning` for the whole beat gives a
    caption that promises a plan over a box that says there isn't one."""
    spotlight_lands = camera.BEAT_2 + camera.SPOT_DRIFT
    status, _live, _burn, _turns = fx.stage_at(spotlight_lands)
    assert status not in ("pending", "context", "planning"), status
    assert fx.hero_detail(spotlight_lands)["context"]["spec"] == fx.SPEC
    # ...and not before: the orchestrator writes it after the planner returns.
    assert "spec" not in fx.hero_detail(camera.BEAT_2)["context"]


def test_the_task_is_still_running_when_the_agents_beat_lands_on_it():
    """Beat 3 is the System view's live board. `SlideOver.jsx` computes
    `isActive` from the STATUS and `clampAgentState` turns every "active" node
    into "done" when it is false — so on an awaiting_approval task the beat
    about the agents running shows five finished lanes and no live dot."""
    active = ("pending", "context", "planning", "implementing", "testing",
              "reviewing")
    for t in (camera.BEAT_3 + camera.SPOT_DRIFT, camera.BEAT_3 + 2.0):
        status, _live, _burn, _turns = fx.stage_at(t)
        assert status in active, (t, status)
        assert fx.hero_row(t)["claimed"] is True, t


def test_every_stage_lands_inside_the_beat_it_belongs_to():
    """Four of the five beats watch a different surface of the same task, and
    three of them are only honest at a particular point in the pipeline. A
    retiming is exactly the sort of edit that leaves one stage stranded in the
    wrong beat with nothing to show for it."""
    starts = [s[0] for s in fx.STAGES]
    assert starts == sorted(starts)
    assert camera.BEAT_1 < starts[0] < camera.BEAT_2, starts[0]
    # The run finishes inside beat 3, and NOT later: beats 4 and 5 are the
    # shell reading its own detail pane, and a live event arriving into it
    # scrolls whichever of `/diff` or `/logs` is on camera off the top.
    assert camera.BEAT_3 < starts[-1] < camera.BEAT_4, starts[-1]
    assert all(due < starts[-1] for due, _e in fx.EVENT_TRAIL), \
        "an event after awaiting_approval would scroll beat 4's diff away"


def test_approval_lands_inside_the_last_beat():
    assert camera.BEAT_5 < fx.APPROVE_AT < camera.FADE_OUT_START
    assert fx.approved_at(camera.BEAT_4) is None
    assert fx.approved_at(fx.APPROVE_AT) == fx.NOW


def test_the_approved_state_is_on_screen_long_enough_to_read():
    """The payoff arriving 0.4 s before the fade would be a payoff nobody
    sees. Two seconds is the floor."""
    assert camera.FADE_OUT_START - fx.APPROVE_AT >= 2.0


def test_burn_only_ever_goes_up():
    burns = [burn for _t, _s, _l, burn, _turns in fx.STAGES]
    assert burns == sorted(burns)


def _diff_files() -> dict[str, list[str]]:
    """The diff applied: {path: [line 1, line 2, ...]} of the NEW file, with a
    placeholder at index 0 so `lines[n]` is line n. Lines the diff does not
    carry (before the first hunk) read `<unchanged>` — a citation landing on
    one of those is citing a line nobody watching the clip can see."""
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in fx.DIFF.splitlines():
        if raw.startswith("+++ b/"):
            current = files.setdefault(raw[len("+++ b/"):], [""])
            continue
        if raw.startswith("@@"):
            # `@@ -a,b +c,d @@` — the new file starts at c, so pad to it.
            new = raw.split("+", 1)[1].split(" ", 1)[0]
            start = int(new.split(",")[0])
            assert current is not None
            while len(current) < start:
                current.append("<unchanged>")
            continue
        if current is None or raw.startswith(("diff --git", "index ", "--- ")):
            continue
        if raw.startswith("+"):
            current.append(raw[1:])
        elif raw.startswith(" "):
            current.append(raw[1:])
        # a "-" line is in the OLD file only; it does not advance the new one.
    return files


def test_the_diff_is_a_diff_of_the_files_the_plan_named():
    """Beat 2 promises two files and beat 4 shows the change. A plan that names
    a file the diff never touches is the exact kind of thing a viewer notices
    and nothing else checks."""
    assert sorted(_diff_files()) == sorted(fx.SPEC["files_to_change"])
    assert "test_calc.py" in _diff_files(), \
        "the trail commits '2 files' and the checklist cites a test"


@pytest.mark.parametrize("item", fx.REVIEW_CHECKLIST["items"],
                         ids=[i["file"] + ":" + str(i["line"])
                              for i in fx.REVIEW_CHECKLIST["items"]])
def test_every_line_the_review_cites_is_a_line_of_the_diff(item):
    """Beats 4 and 5 are four seconds apart and show the same change twice —
    once as `git diff`, once as the reviewer's citations. Before this cut
    nothing compared them, and they did not agree: the checklist cited
    `calc.py:6 — return a * b` while the diff put that statement on line 9,
    cited `test_calc.py:11` and `:16` against a diff that contained no
    test_calc.py at all, and pinned the docstring nit to `calc.py:2-5`, which
    was `def add`. All four were wrong, in a beat nobody had ever framed.
    """
    files = _diff_files()
    assert item["file"] in files, item
    lines = files[item["file"]]
    assert item["line"] < len(lines), (item, len(lines) - 1)
    cited = lines[item["line"]]
    assert cited != "<unchanged>", (item, "cites a line outside every hunk")
    # ...and the prose the operator reads names the same location.
    assert f"{item['file']}:{item['line']}" in item["evidence"], item
    # Two of the four go further and QUOTE the line. Those are checked against
    # the diff verbatim; the other two describe it (`calc.py:9 raises;
    # test_calc.py:18 asserts it`, `calc.py:6 — the docstring omits the raise`)
    # and there is nothing to compare a sentence to.
    quoted = {("calc.py", 10): "return a * b",
              ("test_calc.py", 14): "assert mul(3, 4) == 12"}
    want = quoted.get((item["file"], item["line"]))
    if want:
        assert want in item["evidence"], item
        assert want in cited, (want, cited)


def test_the_review_passed_with_something_left_to_read():
    """SlideOver.jsx renders `.ci-evidence` only for an item with
    passed=false, so an all-green checklist is a wall of labels with no cited
    evidence in it — which is the one thing beat 3 exists to show."""
    checklist = fx.REVIEW_CHECKLIST
    assert checklist["passed"] is True
    graded = [i for i in checklist["items"] if not i["passed"]]
    assert graded, "a fully-passing checklist renders no evidence text"
    assert all(i.get("severity") in ("low", "nit") for i in graded), \
        "a blocking finding would contradict the PASSED verdict"
    assert all(i.get("evidence") for i in checklist["items"])


# --------------------------------------------------------------------------- #
# The event trail — what the two serialisers would actually let through        #
# --------------------------------------------------------------------------- #

def _is_agent_session(source: str) -> bool:
    """`core/orchestrator.py:is_agent_session`, reproduced from its constants
    rather than imported, so this test still holds if the demo harness is ever
    read without the package importable. CODER_ROLE / AGGREGATOR_ROLE /
    PLANNER_ROLE are "agent" / "aggregator" / "planner"."""
    return (source == "agent" or source == "aggregator"
            or source.startswith("planner"))


def _survives_the_serialisers(event: dict) -> bool:
    """The allow-list `api/app.py:_format_events` and `task_events_stream`
    BOTH carry, verbatim. A frame that fails it is dropped before it reaches
    either surface."""
    source, kind = event["source"], event["kind"]
    if source in ("orchestrator", "watcher", "human") or kind in ("result", "error"):
        return True
    return _is_agent_session(source) and (
        kind in ("tool_use", "tool_result", "text", "agent_text", "thinking")
        or kind.startswith("subagent_"))


@pytest.mark.parametrize("event", [e for _due, e in fx.EVENT_TRAIL],
                         ids=[f"{i}-{e['kind']}"
                              for i, (_d, e) in enumerate(fx.EVENT_TRAIL)])
def test_every_scripted_event_is_one_the_product_would_actually_serialise(event):
    """This used to allow `source: "coder"` — a value the product never emits.
    `CODER_ROLE` is "agent", "coder" fails `is_agent_session`, and every one of
    the old trail's tool_use frames would therefore have been DROPPED by the
    real `_format_events` before reaching the board or the shell. The clip
    showed them anyway, because the fixture answered the endpoint instead of
    being answered by it.

    The reviewer's verdict is still absent for the reason it always was:
    `_emit_review` stamps `source: "reviewer"`, which is in neither allow-list
    (the KNOWN BUG comment at `api/app.py:_format_events` has the measured
    4-in/1-out). Scripting one would put a line on camera the shipped product
    cannot print.
    """
    assert event["source"] != "reviewer", \
        "the reviewer's own frames are dropped by both serialisers"
    assert _survives_the_serialisers(event), event


def test_the_agent_board_has_a_frame_for_every_role_it_draws():
    """`SlideOver.jsx:SystemTab` derives the whole beat-3 board from this trail
    and nothing else, and a role with no frame renders as a lane that says "not
    started yet". Four of the five stages must therefore be represented, and
    each by a source/kind pair `eventRoles.js` maps to that node.

    Shepherding (the post-PR watcher) is deliberately NOT here: nothing has
    merged yet at the moment beat 3 looks, so its lane is honestly empty.
    """
    # eventRoles.js: a `source` wins; else SOURCE_BY_KIND; else "worker".
    # An "orchestrator" source is re-attributed by kind via ORCHESTRATOR_EMITS_FOR.
    emits_for = {"supervisor": "supervisor", "supervisor_decision": "supervisor",
                 "review_start": "reviewer", "review": "reviewer",
                 "review_error": "reviewer", "tamper": "reviewer"}

    def node(e):
        src = e["source"]
        if src == "orchestrator":
            return emits_for.get(e["kind"], "worker")
        if src == "aggregator" or src.startswith("planner"):
            return "planner"
        return src

    seen = {node(e) for _due, e in fx.EVENT_TRAIL}
    assert {"worker", "planner", "agent", "supervisor", "reviewer"} <= seen, seen
    assert "watcher" not in seen, "nothing has merged yet; Shepherding is empty"


def test_the_models_frame_names_every_role_the_board_labels():
    """`eventRoles.js:modelsByNode` labels a lane from a `models` frame's dict,
    keyed coder/planner/reviewer/supervisor (MODEL_ROLE_TO_NODE). A missing key
    is a lane with no model on it, which is the one thing beat 3 claims that
    the shell cannot."""
    frames = [e for _d, e in fx.EVENT_TRAIL if e["kind"] == "models"]
    assert frames, "no models frame — beat 3's lanes would carry no model"
    for frame in frames:
        assert set(frame["models"]) == {"coder", "planner", "reviewer",
                                        "supervisor"}
        assert frame["text"].startswith("coder=")
        for role, model in frame["models"].items():
            assert f"{role}={model}" in frame["text"]
    # The project's four fixed model tiers, unabbreviated and undated.
    assert fx.MODELS["coder"] == "claude-sonnet-5"
    assert fx.MODELS["planner"] == fx.MODELS["reviewer"] == "claude-opus-5"
    assert fx.MODELS["supervisor"] == "claude-sonnet-5"


def test_a_subagent_runs_under_a_role_the_board_can_attribute_it_to():
    """`discoverSubagents` pairs a `subagent_start` with its `subagent_done` by
    `task_id` and hangs the node off whichever role emitted it — a lone start
    renders as a child stuck on "active" forever, and `task_type: local_bash`
    is filtered out entirely (NOT_A_SUBAGENT)."""
    starts = {e["task_id"]: e for _d, e in fx.EVENT_TRAIL
              if e["kind"] == "subagent_start"}
    dones = {e["task_id"]: e for _d, e in fx.EVENT_TRAIL
             if e["kind"] == "subagent_done"}
    assert starts and set(starts) == set(dones), (starts.keys(), dones.keys())
    for tid, start in starts.items():
        assert start["task_type"] != "local_bash", tid
        assert dones[tid]["status"] == "completed", tid
        # `label: (e.text || "Subagent").slice(0, 40)` — anything longer is
        # rendered truncated, and the lane column ellipsises well before that.
        assert len(start["text"]) <= 34, (len(start["text"]), start["text"])
    # One under the planner and one under the coder: the fan-out is the claim.
    assert {e["source"].split(":")[0] for e in starts.values()} == {"planner",
                                                                   "agent"}


def test_the_board_is_never_empty():
    """Opening on an empty board shows nothing about what the product does."""
    for t in (0.0, camera.BEAT_2, camera.BEAT_3, camera.FADE_OUT_START):
        assert len(fx.board_at(t)) >= 3


def test_the_hero_reaches_the_review_lane_and_leaves_the_working_lane():
    """It now crosses during beat 3, not beat 2 — the pipeline runs across the
    beat about the agents running rather than finishing before it."""
    lanes = {row["id"]: row["lane"] for row in fx.board_at(camera.BEAT_3)}
    assert lanes[fx.HERO_ID] == "working"
    lanes = {row["id"]: row["lane"] for row in fx.board_at(camera.BEAT_4)}
    assert lanes[fx.HERO_ID] == "review"


# --------------------------------------------------------------------------- #
# Legibility                                                                   #
# --------------------------------------------------------------------------- #

def _shell_lines() -> list[str]:
    """Every string this harness puts into the shell's conversation pane, with
    the prefix the shell itself adds."""
    lines = [f"you: {fx.HERO_PROMPT}", f"you: {fx.GRILL_ANSWER}"]
    for frame in fx.GRILL_ROUND_1 + fx.GRILL_ROUND_2:
        kind = frame.get("kind")
        if kind == "tool_use":
            lines.append(f"  {frame['text']}")
        elif kind == "eval_verdict":
            lines.append(f"  spec check: {frame['verdict']} {frame['rationale']}")
        elif kind == "grill_question":
            lines.append(f"grill: {frame['question']}")
            lines += [f"    - {s}" for s in frame["suggestions"]]
        elif kind == "grill_result":
            lines.append(f"refined spec: {frame['title']}")
            lines.append(f"  {frame['description']}")
            lines += [f"    {i}. {c}"
                      for i, c in enumerate(frame["acceptance_criteria"], 1)]
    return lines


@pytest.mark.parametrize("line", _shell_lines())
def test_every_scripted_conversation_line_fits_the_pane(line):
    assert len(line) <= CONVERSATION_COLS, (
        f"{len(line)} chars; the pane is {CONVERSATION_COLS} wide and the "
        f"overflow wraps mid-phrase on camera: {line!r}")


#: The one scripted line that is ALLOWED to wrap in the shell's detail pane,
#: and how many pane rows it may take.
#:
#: `_emit_models` builds its text as `role=model · … · auth=<profile>`, which is
#: 115 characters — it cannot be shortened without misreporting what ran. It is
#: also the SHELL'S ONLY ANSWER to beat 3: `shell.py:_format_event` prints an
#: event's kind and text and drops its `source`, so a planner's tool call and
#: the coder's both render as `-> Edit calc.py` and this is the one frame that
#: names the four roles and their models. The pane wraps (shell.py
#: `min_width=0`), three rows of an eight-row pane is affordable, and dropping
#: the line to satisfy a one-line rule would cost the beat.
WRAPPING_IS_DELIBERATE = {"models"}
MAX_WRAPPED_ROWS = 3


def test_every_detail_pane_line_fits_the_pane():
    """Including the PR URL. At 84 columns it was 9 characters too long and had
    to be exempted; at 100 the whole `https://github.com/you/calc/pull/128`
    lands on one line, which is the point of the wider terminal."""
    for _due, event in fx.EVENT_TRAIL:
        rendered = f"* {event['kind']} {event['text']}"
        if event["kind"] in WRAPPING_IS_DELIBERATE:
            rows = -(-len(rendered) // CONVERSATION_COLS)   # ceil
            assert rows <= MAX_WRAPPED_ROWS, (rows, rendered)
            continue
        assert len(rendered) <= CONVERSATION_COLS, (len(rendered), rendered)
    # ...and the exception must not quietly become the rule.
    assert WRAPPING_IS_DELIBERATE == {"models"}


def test_every_line_of_the_diff_fits_the_shell_pane():
    """Beat 4 is `/diff` in a 55-column pane. A line of code that wraps there
    is a line of code a viewer reads as two, in the beat whose whole claim is
    that you can read the diff."""
    for line in fx.DIFF.splitlines():
        assert len(line) <= fx.DIFF_PANE_COLS, (len(line), line)


def _board_effective_px(css_px: float) -> float:
    """What a `css_px` element of the board measures on a viewer's screen.

    Nothing is cropped: the whole 1280 CSS px window is scaled to the player's
    width, so every source px is scaled by exactly DISPLAY_CSS / VIEW_W. The
    asset's own resolution only decides how sharp it is, not how big.
    """
    return css_px * DISPLAY_CSS / gui_frames.VIEW_W


def _terminal_effective_px() -> float:
    """Effective font-size of the shell's monospace, from the grid alone.

    A monospace advance is 0.6 * font-size; COLS advances span the grid, and
    the grid is `COLS * CELL_W` of the `VIEW_W` frame that fills the player.
    """
    grid_share = cli_frames.COLS * cli_frames.CELL_W / cli_frames.VIEW_W
    advance = DISPLAY_CSS * grid_share / cli_frames.COLS
    return advance / 0.6


def test_the_terminal_reads_at_full_frame():
    """The whole shell is in shot in every frame now, so this one number is
    the entire legibility question for that clip. It was 11.9 px at 84 columns
    on a 600 px player — the floor. It is ~20 px at 100 columns full width."""
    px = _terminal_effective_px()
    assert px >= 18.0, px


def test_the_board_review_evidence_reads_at_full_frame():
    """`.ci-title` and the checklist rows are 13 px CSS and are what beat 5
    exists to be read. 12 px effective was the floor the crops were tuned to;
    the full frame clears it without a crop."""
    assert _board_effective_px(13) >= 12.0, _board_effective_px(13)
    assert _board_effective_px(15) >= 14.0, _board_effective_px(15)


def test_the_diff_reads_at_full_frame():
    """`.diff-pre` is 12.5 px CSS (styles.css) — beat 4's entire content, and
    the one beat where a viewer is expected to read code rather than prose."""
    assert _board_effective_px(12.5) >= 12.0, _board_effective_px(12.5)


def test_the_agent_board_type_is_recorded_with_its_one_shortfall():
    """Beat 3's three type sizes, measured (`_probe_rects.py`):

        .fx-label      14.5 px CSS -> 14.0 effective   stage name
        .fx-role-row   13.5        -> 13.1             role name
        .fx-model      11.0        -> 10.7             the model that ran it

    The model label is the only thing in either clip that a viewer is asked to
    read below the 12 px floor, and there is no framing lever left: the
    spotlight does not magnify and a narrower capture would shrink the window
    the whole cut exists to show whole. It is recorded here rather than
    quietly tolerated, with a floor under it — if a layout change pushes it
    under 10, the trade stops being worth making and this test is where that
    conversation starts.
    """
    assert _board_effective_px(14.5) >= 14.0
    assert _board_effective_px(13.5) >= 13.0
    model_px = _board_effective_px(11)
    assert 10.0 <= model_px < 12.0, model_px


def test_the_ten_pixel_card_id_is_the_price_of_the_full_frame():
    """`.card-id` is 10 px CSS and is the id the terminal clip also shows. A
    beat-2 crop used to magnify it to 12.6 effective px; showing the whole
    board instead costs that, and this records the trade with a floor under
    it. If a future layout change pushes it below 9, the crop argument comes
    back and this test is where that conversation starts."""
    px = _board_effective_px(10)
    assert 9.0 <= px < 12.0, px
