"""The conversational shell driven for real: Textual's Pilot presses the keys
and types the text, and the assertions read the RENDERED frame.

`export_screenshot()` renders the app to SVG through the same compositor a
terminal would drive, so the `<text>` runs it contains are what the operator
would actually see - not the widget attributes we happened to set. A test that
asserted on `Static.renderable` would pass even if the widget were never
mounted, zero-height, or covered.

The HTTP layer is a fake in every test here. Nothing in this file needs a
running server.
"""
from __future__ import annotations

import asyncio
import html
import re

import pytest
from click.testing import CliRunner

from no_human.cli.api_client import NhApiError, NhServerUnreachable
from no_human.cli.commands import cli
from no_human.cli.shell import ShellApp, run_shell

SIZE = (150, 50)


def frame(app) -> str:
    """Everything the rendered screen says, as one whitespace-joined string."""
    svg = app.export_screenshot()
    runs = re.findall(r"<text[^>]*>(.*?)</text>", svg)
    text = " ".join(html.unescape(r) for r in runs)
    return text.replace("\xa0", " ")


def _t(**kw) -> dict:
    base = {"id": "0123456789ab", "title": "A task", "status": "pending"}
    base.update(kw)
    return base


class FakeClient:
    """Stands in for NhClient. Records every call; raises what it is told to."""

    def __init__(self, tasks=None, *, grill_frames=None, event_frames=None):
        self.tasks = list(tasks or [])
        self._grill_frames = list(grill_frames or [])
        self._event_frames = list(event_frames or [])
        self.calls: list[tuple] = []
        self.grill_payloads: list[dict] = []
        self.created: list[dict] = []
        self.raise_on: dict[str, Exception] = {}
        self.diff_text = "diff --git a/x b/x"

    def _maybe_raise(self, name):
        exc = self.raise_on.pop(name, None)
        if exc is not None:
            raise exc

    async def board(self):
        self.calls.append(("board",))
        self._maybe_raise("board")
        return [dict(t) for t in self.tasks]

    async def act(self, task_id, verb):
        self.calls.append(("act", task_id, verb))
        self._maybe_raise("act")
        return {"status": "ok"}

    async def reply(self, task_id, answer):
        self.calls.append(("reply", task_id, answer))
        self._maybe_raise("reply")
        return {"status": "ok"}

    async def diff(self, task_id):
        self.calls.append(("diff", task_id))
        self._maybe_raise("diff")
        return self.diff_text

    async def events(self, task_id):
        self.calls.append(("events", task_id))
        self._maybe_raise("events")
        return [{"kind": "tool_use", "text": "Read app.py"}]

    async def create_task(self, **kw):
        self.calls.append(("create_task", kw))
        self._maybe_raise("create_task")
        self.created.append(kw)
        created = _t(id="newtask0001", title=kw["title"], status="pending")
        self.tasks.append(created)
        return created

    async def grill_stream(self, **payload):
        self.grill_payloads.append(payload)
        self._maybe_raise("grill_stream")
        batch = self._grill_frames.pop(0) if self._grill_frames else [{"kind": "done"}]
        for f in batch:
            yield f

    async def stream_events(self, task_id):
        self.calls.append(("stream_events", task_id))
        for f in self._event_frames:
            yield f


async def type_line(pilot, text):
    pilot.app.query_one("#prompt").value = text
    await pilot.press("enter")
    await pilot.pause()
    # The submit handler kicks off a worker; let it run to completion.
    for _ in range(6):
        await pilot.pause()
        await asyncio.sleep(0)


def make_app(client, **kw):
    kw.setdefault("poll_interval", 0)  # 0 = no background polling in tests
    return ShellApp(client, **kw)


# --------------------------------------------------------------------------- #
# Layout                                                                       #
# --------------------------------------------------------------------------- #

async def test_the_shell_mounts_lanes_conversation_and_detail_panes():
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE):
        for pane in ("#header", "#lanes", "#conversation", "#detail", "#prompt"):
            assert app.query_one(pane) is not None


async def test_the_lanes_pane_draws_the_board_columns_in_board_order():
    tasks = [
        _t(id="aaaaaaaa1111", title="Ship it", status="done"),
        _t(id="bbbbbbbb2222", title="Answer me", status="escalated"),
        _t(id="cccccccc3333", title="In flight", status="implementing"),
    ]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    order = [text.index(label) for label in
             ("Needs Answer", "Working", "Failed", "Review PR", "Done")]
    assert order == sorted(order)
    assert "Answer me" in text
    assert "In flight" in text
    assert "Ship it" in text


async def test_a_server_provided_lane_places_the_task_even_against_its_status():
    """The parallel branch's `lane` field is the truth when it is there."""
    app = make_app(FakeClient([_t(id="ffff0000aaaa", title="Server says review",
                                 status="pending", lane="review")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.lane_of("ffff0000aaaa") == "review"


# --------------------------------------------------------------------------- #
# "Needs you" is the point                                                     #
# --------------------------------------------------------------------------- #

async def test_the_header_shows_the_needs_you_count():
    tasks = [_t(id="a" * 12, status="escalated"),
             _t(id="b" * 12, status="awaiting_approval"),
             _t(id="c" * 12, status="implementing")]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    assert "NEEDS YOU: 2" in text


async def test_the_header_says_all_clear_when_nothing_is_waiting_on_you():
    app = make_app(FakeClient([_t(status="implementing")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    assert "NEEDS YOU" not in text
    assert "all clear" in text


async def test_the_first_task_that_needs_you_is_selected_on_startup():
    """Not merely the topmost row. The first row here sits in Needs Answer but
    has already had its answer (the human stopped it), so it is NOT a gate -
    landing on it would open the shell on the one task that wants nothing."""
    tasks = [
        _t(id="stopped00001", status="escalated", blocker_human_stopped=True),
        _t(id="work00000001", status="implementing"),
        _t(id="gate00000001", status="awaiting_approval"),
    ]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.selected_id == "gate00000001"


async def test_the_burn_on_screen_includes_cache_reads():
    app = make_app(FakeClient([
        _t(id="burner000001", title="Expensive", status="implementing",
           total_tokens=731, total_cache_read=4_000_000)]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    assert "4,000,731" in text


# --------------------------------------------------------------------------- #
# Selection and the event tail                                                 #
# --------------------------------------------------------------------------- #

async def test_ctrl_n_and_ctrl_p_walk_the_selection_in_lane_order():
    tasks = [_t(id="cccccccc3333", status="done"),
             _t(id="aaaaaaaa1111", status="escalated"),
             _t(id="bbbbbbbb2222", status="implementing")]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.selected_id == "aaaaaaaa1111"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app.selected_id == "bbbbbbbb2222"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app.selected_id == "cccccccc3333"
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.selected_id == "bbbbbbbb2222"


async def test_the_detail_pane_tails_the_selected_tasks_event_stream():
    client = FakeClient(
        [_t(id="tailme000001", title="Tail me", status="implementing")],
        event_frames=[{"kind": "tool_use", "text": "Edit shell.py"}],
    )
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0)
        text = frame(app)
    assert ("stream_events", "tailme000001") in client.calls
    assert "Edit shell.py" in text


async def test_moving_the_selection_switches_which_stream_is_followed():
    client = FakeClient([_t(id="aaaaaaaa1111", status="escalated"),
                         _t(id="bbbbbbbb2222", status="implementing")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+n")
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0)
    followed = [c[1] for c in client.calls if c[0] == "stream_events"]
    assert followed[-1] == "bbbbbbbb2222"


# --------------------------------------------------------------------------- #
# Natural language is the primary affordance                                   #
# --------------------------------------------------------------------------- #

QUESTION = [
    {"kind": "tool_use", "text": "Grep boardLanes.js"},
    {"kind": "grill_question", "type": "question",
     "question": "Which lane should the button live in?",
     "suggestions": ["Failed", "Review PR"], "round": 1},
    {"kind": "done"},
]
RESULT = [
    {"kind": "eval_verdict", "verdict": "good", "rationale": "clear enough"},
    {"kind": "grill_result", "type": "done", "title": "Add a retry action",
     "description": "Refined description", "acceptance_criteria": ["ac one"]},
    {"kind": "done"},
]


async def test_typing_plain_text_runs_the_grill_and_renders_its_question():
    client = FakeClient([_t()], grill_frames=[QUESTION])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        text = frame(app)
    assert client.grill_payloads[0]["title"] == "add a retry button"
    assert client.grill_payloads[0]["qa_history"] == []
    assert "Which lane should the button live in?" in text
    assert "Grep boardLanes.js" in text
    assert "Failed" in text


async def test_the_answer_goes_back_as_qa_history_on_the_same_endpoint():
    client = FakeClient([_t()], grill_frames=[QUESTION, RESULT])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "the Failed lane")
        text = frame(app)
    assert client.grill_payloads[1]["qa_history"] == [
        {"question": "Which lane should the button live in?", "answer": "the Failed lane"}]
    assert "Add a retry action" in text
    assert "ac one" in text


async def test_accepting_the_refined_spec_creates_the_task():
    client = FakeClient([_t()], grill_frames=[RESULT])
    app = make_app(client, repo_path="/repo")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "yes")
    assert client.created == [{
        "title": "Add a retry action",
        "description": "Refined description",
        "repo_path": "/repo",
        "acceptance_criteria": ["ac one"],
    }]


async def test_declining_the_refined_spec_creates_nothing():
    client = FakeClient([_t()], grill_frames=[RESULT])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "no")
        text = frame(app)
    assert client.created == []
    assert "discarded" in text.lower()


async def test_a_grill_error_reads_as_a_sentence_and_the_shell_survives():
    client = FakeClient([_t()])
    client.raise_on["grill_stream"] = NhApiError("repo_path 'x' is not a git repository")
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        text = frame(app)
        still_up = app.is_running
    assert "not a git repository" in text
    assert "Traceback" not in text
    assert still_up


# --------------------------------------------------------------------------- #
# Slash commands                                                               #
# --------------------------------------------------------------------------- #

async def test_slash_approve_acts_on_the_selected_task():
    client = FakeClient([_t(id="gate00000001", status="awaiting_approval")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/approve")
    assert ("act", "gate00000001", "approve") in client.calls


@pytest.mark.parametrize("verb", ["pause", "resume", "cancel", "retry"])
async def test_the_lifecycle_slashes_hit_their_endpoints(verb):
    client = FakeClient([_t(id="gate00000001", status="escalated")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, f"/{verb}")
    assert ("act", "gate00000001", verb) in client.calls


async def test_slash_reply_sends_the_message_to_the_selected_task():
    client = FakeClient([_t(id="gate00000001", status="awaiting_input")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/reply go with the second option")
    assert ("reply", "gate00000001", "go with the second option") in client.calls


async def test_slash_diff_renders_into_the_detail_pane():
    client = FakeClient([_t(id="gate00000001", status="awaiting_approval")])
    client.diff_text = "--- a/lanes.py\n+++ b/lanes.py\n+one added line"
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/diff")
        text = frame(app)
    assert "one added line" in text


async def test_slash_logs_replays_the_event_log():
    client = FakeClient([_t(id="gate00000001", status="escalated")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/logs")
        text = frame(app)
    assert ("events", "gate00000001") in client.calls
    assert "Read app.py" in text


async def test_slash_help_lists_the_commands():
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/help")
        text = frame(app)
    assert "/approve" in text
    assert "/quit" in text


async def test_an_unknown_slash_names_the_real_commands_and_does_not_crash():
    client = FakeClient([_t(id="gate00000001", status="escalated")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/merge")
        text = frame(app)
        still_up = app.is_running
    assert "/merge is not a command" in text
    assert "/approve" in text
    assert still_up
    assert not any(c[0] == "act" for c in client.calls)


async def test_a_failed_slash_call_is_reported_plainly():
    client = FakeClient([_t(id="gate00000001", status="pending")])
    client.raise_on["act"] = NhApiError("cannot approve a task in status pending")
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/approve")
        text = frame(app)
        still_up = app.is_running
    assert "cannot approve a task in status pending" in text
    assert "Traceback" not in text
    assert still_up


async def test_slash_quit_exits_the_shell():
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/quit")
        await pilot.pause()
    assert not app.is_running


# --------------------------------------------------------------------------- #
# The server going away                                                        #
# --------------------------------------------------------------------------- #

async def test_the_server_dropping_mid_session_says_how_to_start_it():
    client = FakeClient([_t()])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        client.raise_on["board"] = NhServerUnreachable(
            "no_human is not running at http://127.0.0.1:8420\nStart it first:\n  nh start")
        await type_line(pilot, "/help")  # any interaction; refresh runs on ctrl+r
        await pilot.press("ctrl+r")
        for _ in range(6):
            await pilot.pause()
            await asyncio.sleep(0)
        text = frame(app)
        still_up = app.is_running
    assert "nh start" in text
    assert still_up


def test_run_shell_refuses_plainly_when_nothing_is_listening(capsys):
    """A real connect to a port nothing is bound to - no live server, no mock,
    and no traceback."""
    code = run_shell(base_url="http://127.0.0.1:1")
    out = capsys.readouterr().out
    assert code == 1
    assert "nh start" in out
    assert "Traceback" not in out


# --------------------------------------------------------------------------- #
# The `nh` entry point — additive, every existing verb untouched               #
# --------------------------------------------------------------------------- #

# Every verb registered on `nh` before the shell landed. Scripts and the
# installer call these; the shell is only allowed to ADD.
EXISTING_VERBS = {
    "init", "task", "repo", "config", "auth", "rules", "playbook",
    "merge-stack", "skills", "watch", "mcp-serve", "onboard", "docs",
    "ci-gate", "blocked", "reply", "wake", "serve", "status", "autonomy",
    "recall", "agents", "unblock", "approve", "review-comments", "reject",
    "diff", "review", "investigate", "logs", "learnings-curate", "learnings",
    "history", "doctor", "start", "dashboard", "stop", "eval", "bench",
    "shadow", "test",
}


def test_every_pre_existing_verb_is_still_registered():
    assert EXISTING_VERBS <= set(cli.commands)


def test_nh_help_still_lists_the_verbs():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for verb in ("start", "approve", "watch", "status"):
        assert verb in result.output


def test_nh_with_no_arguments_enters_the_shell(monkeypatch):
    import no_human.cli.shell as shell_mod
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(shell_mod, "run_shell", fake)
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert seen != {}


def test_nh_shell_is_also_an_explicit_verb(monkeypatch):
    import no_human.cli.shell as shell_mod
    monkeypatch.setattr(shell_mod, "run_shell", lambda **kw: 0)
    result = CliRunner().invoke(cli, ["shell"])
    assert result.exit_code == 0


def test_a_subcommand_still_runs_instead_of_the_shell(monkeypatch):
    import no_human.cli.shell as shell_mod

    def boom(**kw):
        raise AssertionError("the shell must not launch for a subcommand")

    monkeypatch.setattr(shell_mod, "run_shell", boom)
    result = CliRunner().invoke(cli, ["approve", "--help"])
    assert result.exit_code == 0
    assert "approve" in result.output
