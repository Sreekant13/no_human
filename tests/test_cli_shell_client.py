"""The HTTP layer the conversational shell sits on (`nh` with no arguments).

The shell is an API CLIENT, never a second reader of the SQLite file the
server already holds open — `nh watch`'s TUI opens `Store(config.db_path)`
directly and races the server for it. Everything here is therefore exercised
against `httpx.MockTransport`: no test may need a live server.
"""
from __future__ import annotations

import json

import httpx
import pytest

from no_human.cli.api_client import (
    DEFAULT_BASE_URL,
    MAX_SSE_LINE,
    NhApiError,
    NhClient,
    NhServerUnreachable,
    SSEDecoder,
    classify_grill_frame,
    unreachable_message,
)


def _client(handler, **kw) -> NhClient:
    return NhClient(transport=httpx.MockTransport(handler), **kw)


# --------------------------------------------------------------------------- #
# SSE frame handling — pure, and the piece most likely to lose data silently   #
# --------------------------------------------------------------------------- #

def test_decoder_yields_one_frame_per_data_line():
    dec = SSEDecoder()
    frames = dec.feed('data: {"kind": "text", "text": "a"}\n\n'
                      'data: {"kind": "text", "text": "b"}\n\n')
    assert [f["text"] for f in frames] == ["a", "b"]


def test_decoder_buffers_a_frame_split_across_chunks():
    """A JSON object cut in half by a TCP boundary must not be dropped.
    web/src/api.js keeps the trailing partial line in `buffer`; so must this."""
    dec = SSEDecoder()
    assert dec.feed('data: {"kind": "text", "te') == []
    assert dec.feed('xt": "split"}\n\n') == [{"kind": "text", "text": "split"}]


def test_decoder_never_replays_a_frame_it_already_returned():
    """The consumed text must LEAVE the buffer, not merely be skipped for this
    call. Keeping it means every later chunk re-emits the whole stream: the
    conversation pane would print each grill question again, and each answer
    would be recorded against a question that was already answered."""
    dec = SSEDecoder()
    assert [f["text"] for f in dec.feed('data: {"kind": "text", "text": "a"}\n')] == ["a"]
    assert [f["text"] for f in dec.feed('data: {"kind": "text", "text": "b"}\n')] == ["b"]
    assert dec.feed("") == []


def test_decoder_holds_a_complete_line_until_its_newline_arrives():
    dec = SSEDecoder()
    assert dec.feed('data: {"kind": "text", "text": "x"}') == []
    assert dec.feed("\n")[0]["text"] == "x"


def test_decoder_skips_malformed_json_without_losing_the_next_frame():
    dec = SSEDecoder()
    frames = dec.feed('data: {oops\n'
                      'data: {"kind": "done"}\n')
    assert frames == [{"kind": "done"}]


def test_decoder_ignores_id_and_comment_lines():
    """`/api/tasks/{id}/events/stream` interleaves `id: <ts>` lines for
    EventSource resume. They are not payload and must not become frames."""
    dec = SSEDecoder()
    frames = dec.feed("id: 1712.5\n"
                      ": keep-alive\n"
                      'data: {"kind": "tool_use"}\n')
    assert frames == [{"kind": "tool_use"}]


def test_decoder_tolerates_crlf():
    dec = SSEDecoder()
    assert dec.feed('data: {"kind": "done"}\r\n\r\n') == [{"kind": "done"}]


def test_decoder_ignores_a_data_line_that_is_not_an_object():
    dec = SSEDecoder()
    assert dec.feed("data: 7\ndata: null\n") == []


@pytest.mark.parametrize("frame,expected", [
    ({"kind": "done"}, "done"),
    ({"kind": "eval_verdict", "verdict": "good"}, "eval"),
    ({"kind": "grill_result", "title": "t"}, "result"),
    ({"kind": "grill_question", "question": "q"}, "question"),
    ({"kind": "error", "text": "boom"}, "error"),
    ({"kind": "tool_use", "text": "Read foo"}, "event"),
    ({"kind": "text", "text": "thinking"}, "event"),
    ({}, "event"),
])
def test_classify_grill_frame_mirrors_the_web_composer(frame, expected):
    """grillStepSSE (web/src/api.js:144-153) routes exactly these kinds. The
    CLI shares the endpoint, so it must share the routing or the two surfaces
    drift on the same stream."""
    assert classify_grill_frame(frame) == expected


# --------------------------------------------------------------------------- #
# Reachability and error surfaces — never a traceback                          #
# --------------------------------------------------------------------------- #

def test_unreachable_message_names_the_command_that_starts_the_server():
    msg = unreachable_message("http://127.0.0.1:8420")
    assert "nh start" in msg
    assert "127.0.0.1:8420" in msg
    assert "Traceback" not in msg


def test_default_base_url_is_the_local_server():
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8420"


async def test_connection_refused_raises_unreachable_not_httpx():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as c:
        with pytest.raises(NhServerUnreachable):
            await c.board()


async def test_api_error_carries_the_servers_detail_string():
    def handler(request):
        return httpx.Response(422, json={"detail": "repo_path 'x' is not a git repository"})

    async with _client(handler) as c:
        with pytest.raises(NhApiError) as ei:
            await c.create_task(title="t", description="d", repo_path="x")
    assert "not a git repository" in str(ei.value)


async def test_api_error_without_a_detail_body_still_reports_the_status():
    def handler(request):
        return httpx.Response(500, text="<html>nope</html>")

    async with _client(handler) as c:
        with pytest.raises(NhApiError) as ei:
            await c.board()
    assert "500" in str(ei.value)


async def test_ping_is_false_when_the_server_is_down_and_true_when_up():
    async with _client(lambda r: httpx.Response(200, json=[])) as c:
        assert await c.ping() is True

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(refuse) as c:
        assert await c.ping() is False


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #

async def test_board_returns_the_task_summaries():
    def handler(request):
        assert request.url.path == "/api/tasks"
        return httpx.Response(200, json=[{"id": "a1", "title": "x", "status": "pending"}])

    async with _client(handler) as c:
        assert (await c.board())[0]["id"] == "a1"


async def test_act_posts_to_the_verbs_endpoint():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["method"] = request.method
        return httpx.Response(200, json={"status": "done"})

    async with _client(handler) as c:
        await c.act("abc123", "approve")
    assert seen == {"path": "/api/tasks/abc123/approve", "method": "POST"}


async def test_reply_sends_the_answer_in_the_body():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "pending"})

    async with _client(handler) as c:
        await c.reply("abc123", "use the second option")
    assert seen["path"] == "/api/tasks/abc123/reply"
    assert seen["body"] == {"answer": "use the second option"}


async def test_diff_returns_plain_text():
    async with _client(lambda r: httpx.Response(200, text="diff --git a b")) as c:
        assert await c.diff("abc") == "diff --git a b"


async def test_create_task_sends_the_grilled_spec():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "new1", "title": "Refined"})

    async with _client(handler) as c:
        out = await c.create_task(
            title="Refined", description="desc", repo_path="/repo",
            acceptance_criteria=["ac1", "ac2"],
        )
    assert out["id"] == "new1"
    assert seen["path"] == "/api/tasks"
    assert seen["body"]["title"] == "Refined"
    assert seen["body"]["acceptance_criteria"] == ["ac1", "ac2"]
    assert seen["body"]["source"] == "cli"


async def test_grill_stream_yields_every_frame_in_order():
    body = (
        'data: {"kind": "tool_use", "text": "Grep lanes"}\n\n'
        'data: {"kind": "grill_question", "question": "which repo?", '
        '"suggestions": ["a", "b"], "round": 1}\n\n'
        'data: {"kind": "done"}\n\n'
    )
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=body)

    frames = []
    async with _client(handler) as c:
        async for f in c.grill_stream(title="t", description="d", repo_path="/repo",
                                      qa_history=[{"question": "q", "answer": "a"}]):
            frames.append(f)

    assert seen["path"] == "/api/grill/stream"
    assert seen["body"]["qa_history"] == [{"question": "q", "answer": "a"}]
    assert [classify_grill_frame(f) for f in frames] == ["event", "question", "done"]


async def test_grill_stream_reports_a_rejected_request_as_an_api_error():
    def handler(request):
        return httpx.Response(422, json={"detail": "repo_path 'nope' is not a git repository"})

    with pytest.raises(NhApiError) as ei:
        async with _client(handler) as c:
            async for _ in c.grill_stream(title="t", description="d", repo_path="nope"):
                pass
    assert "not a git repository" in str(ei.value)


async def test_stream_events_yields_task_events():
    body = ('data: {"kind": "tool_use", "text": "Edit app.py"}\n\n'
            'data: {"kind": "done", "text": "task not found"}\n\n')

    def handler(request):
        assert request.url.path == "/api/tasks/abc/events/stream"
        return httpx.Response(200, text=body)

    got = []
    async with _client(handler) as c:
        async for f in c.stream_events("abc"):
            got.append(f)
    assert [f["kind"] for f in got] == ["tool_use", "done"]


async def test_stream_events_resumes_from_the_cursor_it_is_given():
    """The server reads `last-event-id` and replays only what came after it
    (app.py `task_events_stream`). Reconnecting without it re-sends the whole
    deque, and the detail pane would print every event twice."""
    seen = {}

    def handler(request):
        seen["last_event_id"] = request.headers.get("last-event-id")
        return httpx.Response(200, text='data: {"kind": "done"}\n\n')

    async with _client(handler) as c:
        async for _ in c.stream_events("abc", last_event_id="1712.5"):
            pass
    assert seen["last_event_id"] == "1712.5"


# --------------------------------------------------------------------------- #
# The bodies the client sends, checked against the SERVER'S OWN models         #
#                                                                              #
# Every test above answers through a MockTransport that returns 2xx for any    #
# input, so none of them can catch a body the real server refuses — and one    #
# shipped exactly that way: `create_task` put `kind: None, priority: None` in  #
# the body unconditionally, the server declares them `str` with defaults, and  #
# Pydantic rejects an explicit null for a non-optional str. Every create was a #
# 422 and the shell's headline feature could not file a single task.           #
#                                                                              #
# So these validate the bytes the client actually puts on the wire against the #
# request model `no_human.api.app` binds for that route. A permissive mock     #
# proves nothing; the server's own model is the contract.                      #
# --------------------------------------------------------------------------- #

async def _body_of(call) -> dict:
    """Run one client call against a mock that accepts anything, and hand back
    the JSON body it sent."""
    seen: dict = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x"}, headers={"content-type": "application/json"})

    async with _client(handler) as c:
        out = call(c)
        if hasattr(out, "__aiter__"):
            async for _ in out:
                pass
        else:
            await out
    return seen["body"]


#: (name, the call, the model the route declares). Every POST the shell makes
#: that builds a body is here — the two verb POSTs (`act`) send no body at all.
BODY_CONTRACTS = [
    ("create_task as the shell calls it",
     lambda c: c.create_task(title="Refined", description="desc", repo_path="/repo",
                             acceptance_criteria=["ac1", "ac2"]),
     "CreateTaskRequest"),
    ("create_task with nothing but a title",
     lambda c: c.create_task(title="Just a title"),
     "CreateTaskRequest"),
    ("create_task with every optional field set",
     lambda c: c.create_task(title="T", description="d", repo_path="/repo",
                             project_id="p1", kind="bugfix", priority="high",
                             acceptance_criteria=["ac"]),
     "CreateTaskRequest"),
    ("reply",
     lambda c: c.reply("abc123", "use the second option"),
     "ReplyRequest"),
    ("grill_stream as the shell calls it",
     lambda c: c.grill_stream(title="t", description="d", repo_path="/repo",
                              qa_history=[{"question": "q", "answer": "a"}]),
     "GrillStepRequest"),
    ("grill_stream with nothing but a title",
     lambda c: c.grill_stream(title="t"),
     "GrillStepRequest"),
]


@pytest.mark.parametrize("name,call,model_name", BODY_CONTRACTS,
                         ids=[c[0] for c in BODY_CONTRACTS])
async def test_the_body_the_client_sends_validates_against_the_server_model(
        name, call, model_name):
    from no_human.api import models as api_models

    model = getattr(api_models, model_name)
    body = await _body_of(call)
    model(**body)  # raises ValidationError if the server would have said 422


async def test_create_task_omits_the_fields_it_was_not_given_rather_than_nulling_them():
    """`kind` and `priority` are `str` with server-side defaults. An absent key
    takes the default; an explicit null is a type error. This is the exact bug,
    named: the body must not carry a key it has no value for."""
    body = await _body_of(lambda c: c.create_task(title="T", description="d"))
    assert [k for k, v in body.items() if v is None] == []
    assert "kind" not in body
    assert "priority" not in body
    assert "project_id" not in body


async def test_create_task_still_sends_a_kind_and_priority_when_it_has_them():
    body = await _body_of(
        lambda c: c.create_task(title="T", kind="bugfix", priority="high"))
    assert body["kind"] == "bugfix"
    assert body["priority"] == "high"


async def test_the_server_defaults_survive_the_omission():
    """Omitting is only correct if the server fills the same values in. Read
    them off the model rather than restating them here."""
    from no_human.api.models import CreateTaskRequest

    body = await _body_of(lambda c: c.create_task(title="T"))
    parsed = CreateTaskRequest(**body)
    assert parsed.kind == "feature"
    assert parsed.priority == "medium"
    assert parsed.title == "T"


async def test_a_422_names_the_fields_the_server_rejected():
    """FastAPI's 422 `detail` is a LIST of {loc, msg}, not a str, so the old
    fallback printed `POST /api/tasks -> 422` and nothing else — which is
    precisely how a broken client body reached an operator undiagnosed."""
    def handler(request):
        return httpx.Response(422, json={"detail": [
            {"type": "string_type", "loc": ["body", "kind"],
             "msg": "Input should be a valid string"},
            {"type": "string_type", "loc": ["body", "priority"],
             "msg": "Input should be a valid string"},
        ]})

    async with _client(handler) as c:
        with pytest.raises(NhApiError) as ei:
            await c.create_task(title="t")
    msg = str(ei.value)
    assert "kind" in msg
    assert "priority" in msg
    assert "Input should be a valid string" in msg
    assert "422" in msg


async def test_a_422_whose_detail_is_neither_string_nor_list_still_reports_the_call():
    def handler(request):
        return httpx.Response(422, json={"detail": {"unexpected": "shape"}})

    async with _client(handler) as c:
        with pytest.raises(NhApiError) as ei:
            await c.create_task(title="t")
    assert "/api/tasks" in str(ei.value)
    assert "422" in str(ei.value)


# --------------------------------------------------------------------------- #
# Hostile streams                                                              #
# --------------------------------------------------------------------------- #

def test_the_decoder_does_not_retain_an_unbounded_line():
    """A stream that never sends a newline must not turn into a memory leak in
    a long-lived TUI: the buffer is capped, not grown forever."""
    dec = SSEDecoder()
    for _ in range(200):
        dec.feed("x" * 100_000)
    assert len(dec._buffer) <= MAX_SSE_LINE + 100_000


def test_the_decoder_survives_json_nested_past_the_recursion_limit():
    """`json.loads` raises RecursionError — NOT a ValueError — on deep nesting,
    so catching only ValueError lets a hostile frame kill the stream."""
    dec = SSEDecoder()
    frames = dec.feed("data: " + "[" * 200_000 + "\n"
                      'data: {"kind": "done"}\n')
    assert frames == [{"kind": "done"}]
