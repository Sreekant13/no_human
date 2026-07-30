"""The pure pieces behind the conversational shell: lane grouping, the burn
figure, slash-command parsing, and the intake state machine.

None of this touches Textual or the network, so it is where the semantics get
pinned. The rendering tests live in tests/test_cli_shell_app.py and drive the
real app with Pilot.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from no_human.cli.shell_input import (
    SLASH_COMMANDS,
    IntakeSession,
    SlashError,
    help_text,
    is_slash,
    parse_slash,
)
from no_human.cli.shell_lanes import (
    LANE_KEYS,
    LANES,
    flat_order,
    group_by_lane,
    is_waiting,
    lane_for,
    needs_you,
    needs_you_count,
    render_header,
    render_lanes,
    task_burn,
    total_burn,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _t(**kw) -> dict:
    base = {"id": "0123456789ab", "title": "A task", "status": "pending"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Lane routing — the server's field wins, local routing is the fallback        #
# --------------------------------------------------------------------------- #

def test_lane_keys_and_order_match_the_board():
    """web/src/boardLanes.js:19-25, left to right."""
    assert [lane.key for lane in LANES] == [
        "answer", "working", "failed", "review", "done"]
    assert [lane.label for lane in LANES] == [
        "Needs Answer", "Working", "Failed", "Review PR", "Done"]
    assert LANE_KEYS == {"answer", "working", "failed", "review", "done"}


def test_exactly_the_needs_you_lanes_are_loud():
    assert {lane.key for lane in LANES if lane.needs_you} == {"answer", "review"}


def test_lane_prefers_the_server_provided_field():
    """A parallel branch adds `lane` to the board payload. When it is there it
    is the truth — the CLI must not re-derive a second opinion."""
    assert lane_for(_t(status="pending", lane="review")) == "review"


@pytest.mark.parametrize("bogus", ["waiting", "", 7, True, {}, None])
def test_a_bogus_server_lane_falls_back_to_local_routing(bogus):
    """Rendering a phantom column because the payload said "waiting" is worse
    than routing it ourselves."""
    assert lane_for(_t(status="awaiting_approval", lane=bogus)) == "review"


def test_lane_field_absent_degrades_to_local_routing():
    """The branch that adds `lane` may not have merged yet."""
    assert lane_for(_t(status="escalated")) == "answer"
    assert lane_for(_t(status="implementing")) == "working"
    assert lane_for(_t(status="failed")) == "failed"
    assert lane_for(_t(status="done")) == "done"


def test_blocked_routes_on_its_wake_condition():
    """boardLanes.js:41-43 — with a wake condition it self-resolves (Working);
    without one a human owes it an answer."""
    assert lane_for(_t(status="blocked", blocker_wake_condition="CI goes green")) == "working"
    assert lane_for(_t(status="blocked")) == "answer"
    assert lane_for(_t(status="blocked", blocker_wake_condition=None)) == "answer"
    assert lane_for(_t(status="blocked", blocker_wake_condition="")) == "answer"


def test_unknown_status_falls_back_to_working_not_to_a_new_lane():
    """boardLanes.js:47. A status the CLI has never heard of must not invent a
    lane — no lane may claim a state the API does not report."""
    assert lane_for(_t(status="quantum_superposition")) == "working"
    assert lane_for(_t(status=None)) == "working"
    assert lane_for({}) == "working"


def test_is_waiting_covers_quota_pause_and_self_resolving_blocks():
    assert is_waiting(_t(status="paused_quota")) is True
    assert is_waiting(_t(status="blocked", blocker_wake_condition="pr merges")) is True
    assert is_waiting(_t(status="blocked")) is False
    assert is_waiting(_t(status="implementing")) is False


# --------------------------------------------------------------------------- #
# "Needs you" — the count in the header                                        #
# --------------------------------------------------------------------------- #

def test_needs_you_is_true_for_the_gate_lanes():
    assert needs_you(_t(status="awaiting_input")) is True
    assert needs_you(_t(status="escalated")) is True
    assert needs_you(_t(status="awaiting_approval")) is True
    assert needs_you(_t(status="blocked")) is True
    assert needs_you(_t(status="implementing")) is False
    assert needs_you(_t(status="done")) is False


def test_an_approved_pr_stops_shouting_but_keeps_its_lane():
    """boardLanes.js:71-73 — approved, waiting on the merge, not on you."""
    task = _t(status="awaiting_approval", approved_at="2026-07-30T00:00:00Z")
    assert needs_you(task) is False
    assert lane_for(task) == "review"


def test_a_human_stopped_blocker_stops_shouting_but_keeps_its_lane():
    """boardLanes.js:76 — the human already gave their answer."""
    task = _t(status="escalated", blocker_human_stopped=True)
    assert needs_you(task) is False
    assert lane_for(task) == "answer"


def test_needs_you_count_matches_the_loud_lane_membership():
    tasks = [
        _t(id="a", status="awaiting_input"),
        _t(id="b", status="awaiting_approval"),
        _t(id="c", status="awaiting_approval", approved_at="2026-07-30T00:00:00Z"),
        _t(id="d", status="implementing"),
        _t(id="e", status="blocked"),
        _t(id="f", status="blocked", blocker_wake_condition="ci"),
    ]
    assert needs_you_count(tasks) == 3


# --------------------------------------------------------------------------- #
# Grouping                                                                     #
# --------------------------------------------------------------------------- #

def test_group_by_lane_returns_every_lane_even_when_empty():
    groups = group_by_lane([_t(id="a", status="done")])
    assert list(groups) == ["answer", "working", "failed", "review", "done"]
    assert groups["done"][0]["id"] == "a"
    assert groups["answer"] == []


def test_group_by_lane_uses_the_server_field_per_task():
    groups = group_by_lane([
        _t(id="a", status="pending", lane="review"),
        _t(id="b", status="pending"),
    ])
    assert [t["id"] for t in groups["review"]] == ["a"]
    assert [t["id"] for t in groups["working"]] == ["b"]


def test_flat_order_walks_lanes_in_board_order():
    """Selection with ctrl+n/ctrl+p follows what the eye sees."""
    tasks = [
        _t(id="done1", status="done"),
        _t(id="work1", status="implementing"),
        _t(id="ans1", status="escalated"),
    ]
    assert flat_order(tasks) == ["ans1", "work1", "done1"]


# --------------------------------------------------------------------------- #
# Burn — never tokens_used alone                                               #
# --------------------------------------------------------------------------- #

def test_task_burn_sums_all_nine_buckets():
    """web/src/cost.js `totalBurn`: coder + reviewer + aux, fresh + creation +
    read. `nh logs` reported "tokens=731" for an attempt that spent 4M because
    it showed the first bucket alone."""
    task = _t(
        total_tokens=1, total_cache_read=10, total_cache_creation=100,
        total_review_tokens=1000, total_review_cache_read=10_000,
        total_review_cache_creation=100_000,
        total_aux_tokens=1_000_000, total_aux_cache_read=10_000_000,
        total_aux_cache_creation=100_000_000,
    )
    assert task_burn(task) == 111_111_111


def test_task_burn_is_never_the_fresh_bucket_alone():
    task = _t(total_tokens=731, total_cache_read=4_000_000)
    assert task_burn(task) == 4_000_731


def test_task_burn_treats_missing_and_null_buckets_as_zero():
    assert task_burn(_t()) == 0
    assert task_burn(_t(total_tokens=None, total_cache_read=5)) == 5


def test_total_burn_sums_the_board():
    assert total_burn([_t(total_tokens=2), _t(total_cache_read=3)]) == 5


# --------------------------------------------------------------------------- #
# Rendering — pure markup, asserted for truthfulness                           #
# --------------------------------------------------------------------------- #

def test_header_shows_the_needs_you_count():
    out = render_header([_t(status="escalated"), _t(status="awaiting_approval")])
    assert "NEEDS YOU" in out
    assert "2" in out


def test_header_says_all_clear_when_nothing_needs_you():
    out = render_header([_t(status="implementing")])
    assert "NEEDS YOU" not in out
    assert "all clear" in out.lower()


def test_header_burn_includes_cache_read():
    out = render_header([_t(total_tokens=731, total_cache_read=4_000_000)])
    assert "4,000,731" in out
    assert "731 " not in out.replace("4,000,731", "")


def test_render_lanes_labels_every_lane_and_its_count():
    out = render_lanes([_t(id="a1b2c3d4", title="Fix the gate", status="escalated")])
    for lane in LANES:
        assert lane.label in out
    assert "Fix the gate" in out
    assert "a1b2c3d4"[:8] in out


def test_render_lanes_shows_the_status_the_api_reported():
    out = render_lanes([_t(status="implementing", title="T")])
    assert "implementing" in out


def test_render_lanes_marks_the_selected_task():
    tasks = [_t(id="aaaa1111", status="escalated"), _t(id="bbbb2222", status="escalated")]
    out = render_lanes(tasks, selected_id="bbbb2222")
    selected_line = [ln for ln in out.splitlines() if "bbbb2222" in ln][0]
    other_line = [ln for ln in out.splitlines() if "aaaa1111" in ln][0]
    assert "reverse" in selected_line
    assert "reverse" not in other_line


def test_empty_loud_lane_says_so_instead_of_going_blank():
    out = render_lanes([])
    assert "All caught up" in out


# --------------------------------------------------------------------------- #
# Slash commands — every one maps to an endpoint that already exists           #
# --------------------------------------------------------------------------- #

def test_the_documented_slash_set_is_exactly_what_is_registered():
    assert set(SLASH_COMMANDS) == {
        "approve", "diff", "logs", "pause", "resume", "cancel", "reply",
        "retry", "help", "quit",
    }


def test_every_slash_route_is_a_route_the_server_already_serves():
    """No invented server behaviour: each command's path template must appear
    verbatim in a decorator in api/app.py."""
    source = (REPO_ROOT / "src" / "no_human" / "api" / "app.py").read_text()
    for name, spec in SLASH_COMMANDS.items():
        if spec.path is None:
            continue
        decorator = f'@app.{spec.method.lower()}("{spec.path}"'
        assert decorator in source, f"/{name} -> {decorator} not found in api/app.py"


def test_is_slash_only_for_a_leading_slash():
    assert is_slash("/approve") is True
    assert is_slash("  /approve") is True
    assert is_slash("approve the pr") is False
    assert is_slash("fix /api/tasks routing") is False
    assert is_slash("") is False


def test_parse_slash_defaults_to_the_focused_task():
    cmd = parse_slash("/approve", selected_id="deadbeef")
    assert (cmd.name, cmd.task_id, cmd.arg) == ("approve", "deadbeef", "")


def test_parse_slash_takes_an_explicit_task_id():
    cmd = parse_slash("/diff 1234abcd", selected_id="deadbeef")
    assert (cmd.name, cmd.task_id) == ("diff", "1234abcd")


def test_parse_slash_without_a_task_says_which_task():
    err = parse_slash("/approve", selected_id=None)
    assert isinstance(err, SlashError)
    assert "task" in err.message.lower()


def test_reply_keeps_the_whole_message_as_its_argument():
    cmd = parse_slash("/reply use the second option, not the first",
                      selected_id="deadbeef")
    assert cmd.task_id == "deadbeef"
    assert cmd.arg == "use the second option, not the first"


def test_reply_with_an_explicit_id_splits_id_from_message():
    cmd = parse_slash("/reply 1234abcd go with option 2", selected_id="deadbeef")
    assert cmd.task_id == "1234abcd"
    assert cmd.arg == "go with option 2"


def test_reply_without_a_message_is_an_error():
    err = parse_slash("/reply", selected_id="deadbeef")
    assert isinstance(err, SlashError)
    assert "message" in err.message.lower()


def test_unknown_slash_names_the_commands_that_do_exist():
    err = parse_slash("/merge", selected_id="deadbeef")
    assert isinstance(err, SlashError)
    assert "/merge" in err.message
    assert "/approve" in err.message


def test_help_and_quit_need_no_task():
    for name in ("help", "quit"):
        cmd = parse_slash(f"/{name}", selected_id=None)
        assert cmd.name == name


def test_help_text_lists_every_command():
    text = help_text()
    for name in SLASH_COMMANDS:
        assert f"/{name}" in text


# --------------------------------------------------------------------------- #
# Intake — the same grill the composer runs                                    #
# --------------------------------------------------------------------------- #

def test_a_first_message_becomes_the_title_and_the_description():
    s = IntakeSession.start("Add a retry button to the failed lane", repo_path="/repo")
    assert s.payload() == {
        "title": "Add a retry button to the failed lane",
        "description": "Add a retry button to the failed lane",
        "repo_path": "/repo",
        "qa_history": [],
    }


def test_a_long_first_message_titles_from_the_first_line_and_keeps_it_all():
    text = "Fix the review gate\n\nIt rejects everything because the parser and\nthe prompt disagree."
    s = IntakeSession.start(text, repo_path=None)
    assert s.payload()["title"] == "Fix the review gate"
    assert s.payload()["description"] == text


def test_a_very_long_single_line_title_is_truncated_but_the_body_is_not():
    text = "x" * 300
    s = IntakeSession.start(text, repo_path=None)
    assert len(s.payload()["title"]) <= 120
    assert s.payload()["description"] == text


def test_a_question_then_an_answer_builds_qa_history_the_server_expects():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    s.take_question({"question": "Which lane?", "suggestions": ["failed"], "round": 1})
    assert s.pending_question == "Which lane?"
    s.take_answer("the failed lane")
    assert s.payload()["qa_history"] == [
        {"question": "Which lane?", "answer": "the failed lane"}]
    assert s.pending_question is None


def test_an_answer_with_no_question_pending_is_not_recorded_as_qa():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    with pytest.raises(ValueError):
        s.take_answer("hello?")


def test_the_result_frame_becomes_the_create_task_payload():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    s.take_result({
        "kind": "grill_result", "type": "done",
        "title": "Add a retry action to the Failed lane",
        "description": "Refined description",
        "acceptance_criteria": ["ac one", "ac two"],
    })
    assert s.result is not None
    assert s.task_payload() == {
        "title": "Add a retry action to the Failed lane",
        "description": "Refined description",
        "repo_path": "/repo",
        "acceptance_criteria": ["ac one", "ac two"],
    }


def test_task_payload_before_a_result_is_refused():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    with pytest.raises(ValueError):
        s.task_payload()


def test_a_result_with_no_acceptance_criteria_still_creates_a_task():
    s = IntakeSession.start("Add retry", repo_path=None)
    s.take_result({"title": "T", "description": "D"})
    assert s.task_payload()["acceptance_criteria"] == []
