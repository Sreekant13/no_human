"""Guards for the narrated sprint demo (e2e/demo_video: narration, sprint,
voiceover, compose, sprint_gui, sprint_cli).

Nothing here needs a browser, `say` or ffmpeg — the renders take minutes and
run by hand. What IS checked is everything that can rot silently and only
show up months later as a narrated line talking over the wrong screen:

* the timing map is self-consistent, inside the 60-90 s brief, and every
  line has room to be SPOKEN in its window
* narration.md — the doc a VO artist reads — agrees with narration.py
* the sprint fixture is cut to the narration: five real-looking tickets,
  created in the handoff, running in the parallel beat, all five PRs before
  the payoff, exactly one review bounce
* every scripted event survives the shipped serialisers, every review
  citation is a line of the diff, every line fits the shell's panes — the
  same bar test_demo_video.py holds the 26 s cut to
* both recorders' scripts and the audio assembly are functions of the map,
  not copies of it
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from e2e.demo_video import compose, narration as nr  # noqa: E402
from e2e.demo_video import fixture as fx  # noqa: E402
from e2e.demo_video import sprint as sp  # noqa: E402
from e2e.demo_video import sprint_cli, sprint_gui, voiceover  # noqa: E402

NARRATION_MD = Path(__file__).resolve().parents[1] / "e2e" / "demo_video" / \
    "narration.md"

# --------------------------------------------------------------------------- #
# The timing map                                                               #
# --------------------------------------------------------------------------- #


def test_the_piece_fits_the_brief():
    """60-90 seconds, whole frames, black at both ends."""
    assert 60.0 <= nr.DURATION <= 90.0
    assert nr.N_FRAMES == int(nr.FPS * nr.DURATION)
    assert nr.fade_at(0.0) == 0.0
    assert nr.fade_at(nr.t_of(nr.N_FRAMES - 1)) == 0.0
    assert nr.FADE_OUT_END < nr.DURATION


def test_the_beats_are_the_briefed_arc_in_order():
    """hook -> handoff -> parallel -> gate -> payoff -> close, the operator's
    structure, in the operator's order."""
    names = [b.name for b in nr.BEATS]
    assert names == ["hook", "handoff", "parallel", "gate", "payoff", "close"]
    starts = [b.start for b in nr.BEATS]
    assert starts == sorted(starts)
    assert starts[0] == nr.FADE_IN_END


def test_every_line_lives_inside_its_beat():
    for i, beat in enumerate(nr.BEATS):
        end = nr.BEATS[i + 1].start if i + 1 < len(nr.BEATS) \
            else nr.FADE_OUT_START
        assert beat.lines, beat.name
        for line in beat.lines:
            assert beat.start <= line.start < end, (beat.name, line.start)


def test_lines_are_strictly_ordered_with_room_to_breathe():
    starts = [ln.start for ln in nr.LINES]
    assert starts == sorted(set(starts))
    for line, window in nr.line_windows():
        assert window >= 1.2, (line.start, window, line.text)


@pytest.mark.parametrize("line,window", nr.line_windows(),
                         ids=[f"{ln.start:05.2f}s" for ln, _w in
                              nr.line_windows()])
def test_every_line_can_be_spoken_in_its_window(line, window):
    """~2.75 words/s is voiceover.RATE's measured pace; a line denser than
    3.0 words per second of window cannot be read without racing the screen,
    no matter what the renderer measures later. This is the static version of
    voiceover.check_fit — it holds even on machines with no `say`."""
    words = len(line.tts_text.split())
    assert words / window <= 3.0, (line.text, words, window)


def test_the_gate_beat_outweighs_every_pitch_beat():
    """The independent reviewer is the differentiating claim and the timing
    has to show it: the gate runs longer than the hook and the handoff
    COMBINED, and only the parallel beat (which carries four lines) may
    exceed it. A recut that squeezes the gate to make room for more sizzle
    is the exact trade this test refuses."""
    spans = {}
    for i, beat in enumerate(nr.BEATS[:-1]):
        spans[beat.name] = nr.BEATS[i + 1].start - beat.start
    assert spans["gate"] > spans["hook"] + spans["handoff"], spans
    for name in ("hook", "handoff", "payoff"):
        assert spans["gate"] > spans[name], spans


def test_the_copy_carries_no_hype_vocabulary():
    """The brief bans the words a senior dev would smell. This is a floor,
    not a style checker — but the floor is load-bearing: one 'revolutionary'
    in a narrated demo undoes the whole evidence-first register."""
    banned = ("revolutionary", "supercharge", "game-chang", "10x", "magic",
              "blazing", "unleash", "effortless", "seamless", "incredible")
    text = " ".join(ln.text for ln in nr.LINES).lower()
    for word in banned:
        assert word not in text, word


def test_the_underscore_is_never_read_aloud():
    """Every line whose written text says "no_human" must carry a spoken
    variant saying "no human" — a TTS reading "no underscore human" in the
    close is the single worst frame the piece could ship."""
    for line in nr.LINES:
        if "no_human" in line.text:
            assert line.spoken, line.text
        assert "no_human" not in line.tts_text, line.text
        assert "_" not in line.tts_text, line.text


def test_the_close_names_the_product_and_makes_one_promise():
    last = nr.LINES[-1]
    assert last.text.startswith("no_human.")
    assert "merge" in last.text


def test_every_beat_describes_both_screens():
    for beat in nr.BEATS:
        assert beat.gui and beat.cli and beat.caption, beat.name
        # The caption bar clips past ~70 chars (captions.py's measured bar).
        assert len(beat.caption) <= 70, beat.caption


# --------------------------------------------------------------------------- #
# narration.md — the doc must agree with the data                              #
# --------------------------------------------------------------------------- #


def test_the_doc_carries_every_line_at_its_time():
    doc = NARRATION_MD.read_text()
    # The doc wraps prose at ~76 cols inside `>` blockquotes; strip the
    # markers and collapse whitespace before comparing.
    flat = " ".join(doc.replace(">", " ").split())
    for line in nr.LINES:
        stamp = f"{line.start:.2f}"
        assert stamp in doc, f"missing time {stamp}"
        assert line.text in flat, line.text[:50]


def test_the_doc_carries_every_beat_and_the_duration():
    doc = " ".join(NARRATION_MD.read_text().split())
    assert f"{nr.DURATION:.3f}" in doc
    assert str(nr.N_FRAMES) in doc
    for beat in nr.BEATS:
        assert f"{beat.start:.2f}" in doc, beat.name
        assert beat.gui in doc, beat.name
        assert beat.cli in doc, beat.name


# --------------------------------------------------------------------------- #
# The sprint fixture                                                           #
# --------------------------------------------------------------------------- #


def test_the_backlog_is_ten_real_looking_tickets_five_chosen():
    assert len(sp.BACKLOG) == 10
    assert len(sp.CHOSEN) == 5
    keys = [k for k, *_ in sp.BACKLOG]
    assert len(set(keys)) == 10
    for key in keys:
        assert re.fullmatch(r"[A-Z]{2,6}-\d{2,5}", key), key
    for key, title, kind, _chosen in sp.BACKLOG:
        assert kind in ("feature", "bugfix", "ci_fix", "traceability",
                        "test_gap"), (key, kind)
        assert 10 <= len(title) <= 50, (key, title)
    # The chosen five cover the brief's spread, not five of a kind.
    assert len({kind for k, _t, kind, chosen in sp.BACKLOG if chosen}) >= 3


def test_nothing_synthetic_references_anything_real():
    """The fixture must stay synthetic: no real person, company, product or
    the operator's employer, anywhere a viewer could read."""
    banned = ("example-inc", "atlassian", "stripe", "paypal", "shopify",
              "eyal", "golan", "acme")
    corpus = " ".join([
        " ".join(f"{k} {t}" for k, t, *_ in sp.BACKLOG),
        " ".join(sp.DESCRIPTIONS.values()),
        sp.DIFF, str(sp.SPEC), str(sp.REVIEW_CHECKLIST), sp.REPO_PATH,
        " ".join(ln.text for ln in nr.LINES),
    ]).lower()
    for word in banned:
        assert word not in corpus, word


def test_every_chosen_ticket_is_created_inside_the_handoff():
    for key in sp.CHOSEN:
        created = sp.STAGES[key][0][0]
        assert sp.HANDOFF < created < sp.PARALLEL, (key, created)


def test_all_five_run_in_parallel_during_the_parallel_beat():
    """The narration says "five agents start" at 17.60 and the worker widget
    prints the in-flight count: it must reach 5 while those words hang."""
    assert sp.inflight_at(sp.PARALLEL + 3.0) == 5
    for key in sp.CHOSEN:
        status = sp.stage_at(key, sp.PARALLEL + 3.0)[0]
        assert status not in ("pending", None), (key, status)


def test_the_header_drain_chip_tells_the_fixtures_truth():
    """`drainChip.js` formats {workers_busy}/{max_workers} · {queue_depth}
    queued, and it is on camera in every wide shot. During the handoff the
    five are queued; during the parallel beat all five slots are busy. The
    first capture served a wrong-shaped stub here and the chip read
    "0/0 workers busy" under a narration claiming five agents were running."""
    handoff = sp.queue_health_at(sp.PARALLEL - 1.0)
    assert handoff == {"workers_busy": 0, "max_workers": 5,
                       "queue_depth": 5, "est_drain_seconds": 120}
    running = sp.queue_health_at(sp.PARALLEL + 3.0)
    assert running["workers_busy"] == 5 and running["queue_depth"] == 0
    done = sp.queue_health_at(sp.PAYOFF + 1.0)
    assert done["workers_busy"] == 0 and done["queue_depth"] == 0


def test_all_five_prs_exist_before_the_payoff_opens():
    """"The result: five pull requests" is spoken at 56.40; a card still in
    Working at 56.0 makes the narration a liar."""
    for key in sp.CHOSEN:
        status = sp.stage_at(key, sp.PAYOFF)[0]
        assert status == "awaiting_approval", (key, status)
        assert sp.row_at(key, sp.PAYOFF)["pr_url"] == sp.PR_URLS[key]
    assert len(set(sp.PR_URLS.values())) == 5


def test_exactly_one_task_bounces_off_review_and_still_lands():
    """The gate that never fails anything is decoration; a gate that fails
    two of five makes the payoff a lie. Exactly one, and it recovers."""
    bounced = [key for key in sp.CHOSEN
               if any(a == 2 for *_x, a in sp.STAGES[key])]
    assert bounced == ["ORD-2187"]
    stages = [s for _t, s, *_ in sp.STAGES["ORD-2187"]]
    # reviewing -> implementing is the bounce; it must happen once and end
    # awaiting_approval.
    assert "implementing" in stages[stages.index("reviewing"):]
    assert stages[-1] == "awaiting_approval"


def test_the_bounce_is_on_camera_when_it_is_narrated():
    """"The refactor came back once. It went again." starts at 50.20 — by
    then ORD-2187 must be visibly on its second round, and not yet done."""
    line = next(ln for ln in nr.LINES if "came back once" in ln.text)
    status, _live, _burn, _turns, attempt = sp.stage_at("ORD-2187", line.start)
    assert attempt == 2
    assert status in ("implementing", "testing", "reviewing"), status


def test_stage_times_are_ordered_and_burn_only_goes_up():
    for key, stages in sp.STAGES.items():
        times = [t for t, *_ in stages]
        assert times == sorted(times), key
        burns = [b for _t, _s, _l, b, *_x in stages]
        assert burns == sorted(burns), key
        turns = [n for *_x, n, _a in stages]
        assert turns == sorted(turns), key


def test_updated_at_stays_inside_the_minute():
    """The revision stamp is seconds appended to NOW's minute; a task with
    more revisions than seconds would wrap into invalid timestamps."""
    for key in sp.CHOSEN:
        final = sp._revision(key, nr.DURATION)
        assert final <= 59, (key, final)
        stamp = sp.updated_at(key, nr.DURATION)
        assert re.fullmatch(r"2026-08-03T09:14:\d{2}\+00:00", stamp), stamp


def test_the_board_is_never_empty_and_the_hero_leads_the_handoff():
    assert len(sp.board_at(0.0)) >= 1        # the leftover PR
    assert len(sp.board_at(sp.PARALLEL)) == 6
    created = [sp.STAGES[key][0][0] for key in sp.CHOSEN]
    assert created[0] == min(created), "the hero lands first"


def test_the_hero_is_approved_on_camera_and_only_the_hero():
    assert sp.PAYOFF < sp.APPROVE_AT < nr.FADE_OUT_START
    assert sp.approved_at(sp.HERO_KEY, sp.APPROVE_AT) == sp.NOW
    assert sp.approved_at(sp.HERO_KEY, sp.APPROVE_AT - 1) is None
    for key in sp.CHOSEN:
        if key != sp.HERO_KEY:
            assert sp.approved_at(key, nr.DURATION) is None, key


def test_the_approved_state_is_on_screen_long_enough_to_read():
    assert nr.FADE_OUT_START - sp.APPROVE_AT >= 2.0


# --------------------------------------------------------------------------- #
# The hero's evidence — same bar as the 26 s cut                               #
# --------------------------------------------------------------------------- #


def _diff_files() -> dict[str, list[str]]:
    """sp.DIFF applied — {path: 1-indexed new-file lines} — the same parser
    test_demo_video.py uses on fixture.DIFF, re-derived here so a citation
    can be checked against what a viewer of the gate beat can actually see."""
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in sp.DIFF.splitlines():
        if raw.startswith("+++ b/"):
            current = files.setdefault(raw[len("+++ b/"):], [""])
            continue
        if raw.startswith("@@"):
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
    return files


def test_the_diff_is_a_diff_of_the_files_the_plan_named():
    assert sorted(_diff_files()) == sorted(sp.SPEC["files_to_change"])
    assert "test_webhooks.py" in _diff_files(), \
        "the checklist cites a regression test"


@pytest.mark.parametrize("item", sp.REVIEW_CHECKLIST["items"],
                         ids=[i["file"] + ":" + str(i["line"])
                              for i in sp.REVIEW_CHECKLIST["items"]])
def test_every_line_the_review_cites_is_a_line_of_the_diff(item):
    files = _diff_files()
    assert item["file"] in files, item
    lines = files[item["file"]]
    assert item["line"] < len(lines), (item, len(lines) - 1)
    assert lines[item["line"]] != "<unchanged>", item
    assert f"{item['file']}:{item['line']}" in item["evidence"], item
    quoted = {("test_webhooks.py", 36): "assert Charge.count() == 1",
              ("webhooks.py", 15): "return Response(status=200)",
              ("test_webhooks.py", 33): "test_replay_502_charges_once"}
    want = quoted.get((item["file"], item["line"]))
    if want:
        assert want in item["evidence"], item
        assert want in lines[item["line"]], (want, lines[item["line"]])


def test_the_review_passed_with_something_left_to_read():
    assert sp.REVIEW_CHECKLIST["passed"] is True
    graded = [i for i in sp.REVIEW_CHECKLIST["items"] if not i["passed"]]
    assert graded, "an all-green checklist renders no evidence text"
    assert all(i.get("severity") in ("low", "nit") for i in graded)
    assert all(i.get("evidence") for i in sp.REVIEW_CHECKLIST["items"])


def test_every_line_of_the_diff_fits_the_shell_pane():
    for line in sp.DIFF.splitlines():
        assert len(line) <= fx.DIFF_PANE_COLS, (len(line), line)


def _survives_the_serialisers(event: dict) -> bool:
    """The allow-list both shipped serialisers carry — the same reproduction
    test_demo_video.py holds fixture.EVENT_TRAIL to."""
    source, kind = event["source"], event["kind"]
    if source in ("orchestrator", "watcher", "human") or \
            kind in ("result", "error"):
        return True
    is_agent = (source == "agent" or source == "aggregator"
                or source.startswith("planner"))
    return is_agent and (
        kind in ("tool_use", "tool_result", "text", "agent_text", "thinking")
        or kind.startswith("subagent_"))


@pytest.mark.parametrize("event", [e for _due, e in sp.EVENT_TRAIL],
                         ids=[f"{i}-{e['kind']}"
                              for i, (_d, e) in enumerate(sp.EVENT_TRAIL)])
def test_every_scripted_event_is_one_the_product_would_serialise(event):
    assert event["source"] != "reviewer"
    assert _survives_the_serialisers(event), event


def test_every_detail_pane_line_fits_or_wraps_deliberately():
    """`* kind text`, 50 usable columns — the same measurement as
    test_demo_video.py, models exempted at <= 3 wrapped rows."""
    for _due, event in sp.EVENT_TRAIL:
        rendered = f"* {event['kind']} {event['text']}"
        if event["kind"] == "models":
            assert -(-len(rendered) // 50) <= 3, rendered
            continue
        assert len(rendered) <= 50, (len(rendered), rendered)


def test_the_trail_is_quiet_before_the_shell_reads_its_own_pane():
    """`/diff` is typed at 44.50; a trail frame after that scrolls the diff
    (then the logs) off camera — the exact hazard fixture.py documents."""
    last = max(due for due, _e in sp.EVENT_TRAIL)
    assert last < 44.50, last
    trail_times = [due for due, _e in sp.EVENT_TRAIL]
    assert trail_times == sorted(trail_times)


def test_the_agent_board_has_a_frame_for_every_role_it_draws():
    emits_for = {"supervisor_decision": "supervisor", "tamper": "reviewer",
                 "review_start": "reviewer", "review": "reviewer",
                 "review_error": "reviewer", "supervisor": "supervisor"}

    def node(e):
        src = e["source"]
        if src == "orchestrator":
            return emits_for.get(e["kind"], "worker")
        if src == "aggregator" or src.startswith("planner"):
            return "planner"
        return src

    seen = {node(e) for _due, e in sp.EVENT_TRAIL}
    assert {"worker", "planner", "agent", "supervisor", "reviewer"} <= seen


def test_hero_detail_serves_evidence_only_when_it_exists():
    before_plan = sp.hero_detail(20.0)      # planning until 21.00
    assert "spec" not in before_plan["context"]
    assert sp.hero_detail(22.0)["context"]["spec"] == sp.SPEC
    running = sp.hero_detail(sp.GATE)       # reviewing at 38.40
    assert running["attempts"][0]["review_checklist"] is None
    done = sp.hero_detail(sp.PAYOFF)
    assert done["attempts"][0]["review_checklist"] == sp.REVIEW_CHECKLIST
    assert done["attempts"][0]["test_results"]["tamper_flag"] is False


# --------------------------------------------------------------------------- #
# The recorders — functions of the map                                         #
# --------------------------------------------------------------------------- #


def test_the_cli_script_is_ordered_and_inside_the_clip():
    script = sprint_cli.build_script()
    times = [t for t, _k, _p in script]
    assert times == sorted(times)
    assert 0 < times[0] and times[-1] < nr.DURATION


def test_the_cli_script_repaints_on_every_fixture_change():
    script = sprint_cli.build_script()
    refreshes = {t for t, kind, _p in script if kind == "refresh"}
    for key, stages in sp.STAGES.items():
        for start, *_ in stages:
            assert start + 0.05 in refreshes, (key, start)


def test_the_cli_selects_the_hero_before_the_gate_needs_its_pane():
    script = sprint_cli.build_script()
    selects = [(t, p) for t, k, p in script if k == "select"]
    assert selects == [(sp.PARALLEL + 1.0, sp.HERO_ID)]
    # /diff is typed after the trail has gone quiet, /approve lands on the
    # fixture's approval second.
    diff_keys = [t for t, k, p in script if k == "key" and p == "/"[0]]
    assert diff_keys[0] > max(due for due, _e in sp.EVENT_TRAIL)
    enters = [t for t, k, _p in script if k == "enter"]
    assert any(abs(t - (sp.APPROVE_AT + 0.05)) < 1e-9 for t in enters)


def test_the_cli_types_the_two_commands_the_beats_are_about():
    script = sprint_cli.build_script()
    typed = "".join(p for _t, k, p in script if k == "key")
    assert "/diff" in typed and "/approve" in typed
    submits = [p for _t, k, p in script if k == "submit"]
    assert "/logs" in submits


def test_the_gui_schedule_is_ordered_and_anchored():
    order = [sprint_gui.EXPAND_LANE_AT, sprint_gui.OPEN_DRAWER_AT,
             sprint_gui.SPEC_AT, sprint_gui.SYSTEM_AT, sprint_gui.DIFF_AT,
             sprint_gui.REVIEW_AT, sprint_gui.CLOSE_DRAWER_AT,
             sprint_gui.REOPEN_DRAWER_AT, sp.APPROVE_AT,
             sprint_gui.CLOSE2_AT]
    assert order == sorted(order)
    # The lane expands only after the last ticket exists, before "five
    # agents start" is spoken.
    last_created = max(stages[0][0] for stages in sp.STAGES.values())
    assert last_created < sprint_gui.EXPAND_LANE_AT < sp.PARALLEL
    # The Spec section opens only once the hero's plan exists.
    assert sp.stage_at(sp.HERO_KEY, sprint_gui.SPEC_AT)[0] not in (
        "pending", "context", "planning")
    # The Diff section opens only after the trail's commit frame.
    commit_at = next(due for due, e in sp.EVENT_TRAIL
                     if e["kind"] == "commit")
    assert sprint_gui.DIFF_AT > commit_at
    # The Review section opens only once the checklist exists.
    assert sp.stage_at(sp.HERO_KEY, sprint_gui.REVIEW_AT)[0] == \
        "awaiting_approval"
    # The drawer is closed while the bounce and the five PRs are narrated.
    bounce_line = next(ln for ln in nr.LINES if "came back once" in ln.text)
    five_prs_line = next(ln for ln in nr.LINES if "five pull requests"
                         in ln.text)
    for t in (bounce_line.start, five_prs_line.start):
        assert sprint_gui.CLOSE_DRAWER_AT <= t < sprint_gui.REOPEN_DRAWER_AT


def test_the_gui_pin_follows_the_open_section_and_only_then():
    assert sprint_gui.pin_at(sprint_gui.SPEC_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["spec"]
    assert sprint_gui.pin_at(sprint_gui.SYSTEM_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["system"]
    assert sprint_gui.pin_at(sprint_gui.DIFF_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["diff"]
    assert sprint_gui.pin_at(sprint_gui.REVIEW_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["review"]
    assert sprint_gui.pin_at(sprint_gui.REOPEN_DRAWER_AT + 1.0) == \
        sprint_gui.PIN_BY_BEAT["review"]
    for t in (5.0, sprint_gui.CLOSE_DRAWER_AT + 0.5,
              sprint_gui.CLOSE2_AT + 1.0):
        assert sprint_gui.pin_at(t) is None, t


# --------------------------------------------------------------------------- #
# Voiceover — the audio is a function of the map                               #
# --------------------------------------------------------------------------- #

SAY_LISTING_COMPACT = """\
Albert              en_US    # Hello! My name is Albert.
Anna                de_DE    # Hallo! Ich heisse Anna.
Daniel              en_GB    # Hello! My name is Daniel.
Karen               en_AU    # Hello! My name is Karen.
Samantha            en_US    # Hello! My name is Samantha.
"""

SAY_LISTING_PREMIUM = SAY_LISTING_COMPACT + """\
Ava (Premium)       en_US    # Hello! My name is Ava.
Zoe (Enhanced)      en_US    # Hello! My name is Zoe.
Serena (Premium)    en_GB    # Hello! My name is Serena.
"""


def test_voice_parsing_reads_names_and_locales():
    voices = voiceover.parse_voices(SAY_LISTING_PREMIUM)
    assert ("Samantha", "en_US") in voices
    assert ("Ava (Premium)", "en_US") in voices
    assert ("Anna", "de_DE") in voices


def test_voice_pick_prefers_premium_then_samantha():
    assert voiceover.pick_voice(
        voiceover.parse_voices(SAY_LISTING_PREMIUM)) == "Ava (Premium)"
    assert voiceover.pick_voice(
        voiceover.parse_voices(SAY_LISTING_COMPACT)) == "Samantha"
    with pytest.raises(SystemExit):
        voiceover.pick_voice([("Anna", "de_DE")])


def test_check_fit_passes_lines_that_fit_and_names_the_one_that_does_not():
    windows = [w for _ln, w in nr.line_windows()]
    voiceover.check_fit([w - 0.5 for w in windows])   # all fit: no raise
    bad = [w - 0.5 for w in windows]
    bad[3] = windows[3] + 1.0
    with pytest.raises(SystemExit) as err:
        voiceover.check_fit(bad)
    assert f"at {nr.LINES[3].start:.2f}s" in str(err.value)


def test_the_mix_places_every_line_at_its_map_offset():
    files = [Path(f"/x/line{i:02d}.aiff") for i in range(len(nr.LINES))]
    args = voiceover.mix_args(files, Path("/x/narration.m4a"))
    joined = " ".join(args)
    assert joined.count("-i ") == len(nr.LINES)
    for line in nr.LINES:
        assert f"adelay={int(round(line.start * 1000))}:all=1" in joined, \
            line.start
    assert f"-t {nr.DURATION:.3f}" in joined
    assert "amix=inputs=%d:normalize=0" % len(nr.LINES) in joined


# --------------------------------------------------------------------------- #
# Compose — both clips cut to one map, one audio over both                     #
# --------------------------------------------------------------------------- #


def test_beat_index_walks_the_narration():
    assert compose.beat_index(0.0) == 0
    assert compose.beat_index(nr.BEATS[2].start + 0.01) == 2
    assert compose.beat_index(nr.DURATION) == len(nr.BEATS) - 1


def test_the_composite_scales_into_the_same_frame_as_the_26s_cut():
    args = compose.composite_args(Path("/a/f0000.png"), Path("/b/f0000.png"),
                                  Path("/c/bar0.png"))
    graph = args[args.index("-filter_complex") + 1]
    assert f"scale={compose.APP_W}:{compose.APP_H}" in graph
    assert f"pad={compose.OUT_W}:{compose.OUT_H}" in graph
    assert "overlay=0:%d" % compose.APP_H in graph


def test_the_encode_muxes_the_narration_and_fades_on_the_maps_times():
    args = compose.encode_args(Path("/stage"), Path("/a/narration.m4a"),
                               Path("/out/demo-sprint-gui.mp4"), 24)
    joined = " ".join(args)
    assert "/a/narration.m4a" in joined
    assert "-c:a aac" in joined and "-shortest" in joined
    assert f"st={nr.FADE_OUT_START}" in joined
    assert f"afade=t=out:st={nr.FADE_OUT_START}" in joined


def test_the_side_by_side_stacks_both_clips_under_one_track():
    args = compose.sxs_args(Path("/g"), Path("/c"), Path("/a/narration.m4a"),
                            Path("/out/demo-sprint-sxs.mp4"))
    joined = " ".join(args)
    assert "hstack=inputs=2" in joined
    assert joined.count("f%04d.png") == 2
    assert "-map 2:a" in joined and "/a/narration.m4a" in joined
