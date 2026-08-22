"""The transport cap is pinned by BEHAVIOUR, not by value.

INCIDENT 2026-08-22 (tasks c8d1a30d 11:13, 024d2cd5 11:45): the Agent SDK
transport killed two sessions on one stdout JSON line over its 1 MB default
("JSON message exceeded maximum buffer size"), zero tokens, turn 1. The value
test in tests/test_infra_park_is_not_a_wall.py pins that `_options` raises the
cap; this test manufactures the trigger — a real subprocess CLI emitting one
2 MiB line through the REAL SDK transport — and asserts the session survives
and yields its message. RED on the unfixed backend with the SDK's own error
text; a mutant that drops `max_buffer_size` from `_options` fails it again.

This is ALSO a regression guard on the SDK itself, not only on our fix: if
a future claude_agent_sdk changes its default buffer, renames
`max_buffer_size`, or moves where the limit is enforced
(`_internal/transport/subprocess_cli.py`, the stdout framer's guard), this
test goes red on the upgrade instead of in production. If it fails after an
SDK bump, look there first, not in claude_backend.py.

The fake CLI speaks just enough of the stream-json protocol: it answers every
`control_request` (the SDK's `initialize` handshake) with a success
`control_response`, and on the first user message emits one assistant message
of 2 MiB of text and a `result`.
"""

from __future__ import annotations

import os
import stat
import sys
import textwrap

import pytest

from no_human.agent.claude_backend import ClaudeBackend

_TWO_MIB = 2 * 1024 * 1024

_FAKE_CLI = textwrap.dedent(
    '''\
    #!{python}
    import json, sys
    BIG = "x" * {size}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "control_request":
            sys.stdout.write(json.dumps({{
                "type": "control_response",
                "response": {{"subtype": "success",
                              "request_id": msg.get("request_id"),
                              "response": {{"commands": [], "output_style": "default"}}}},
            }}) + "\\n")
            sys.stdout.flush()
            continue
        if msg.get("type") == "user":
            sys.stdout.write(json.dumps({{
                "type": "assistant",
                "session_id": "fake-session",
                "message": {{"role": "assistant", "model": "fake",
                             "id": "msg_1",
                             "content": [{{"type": "text", "text": BIG}}],
                             "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
            }}) + "\\n")
            sys.stdout.write(json.dumps({{
                "type": "result", "subtype": "success", "duration_ms": 1,
                "duration_api_ms": 1, "is_error": False, "num_turns": 1,
                "session_id": "fake-session", "total_cost_usd": 0.0,
                "result": "done",
                "usage": {{"input_tokens": 1, "output_tokens": 1}},
            }}) + "\\n")
            sys.stdout.flush()
            break
    '''
)


@pytest.fixture
def fake_cli(tmp_path):
    path = tmp_path / "fake-claude"
    path.write_text(_FAKE_CLI.format(python=sys.executable, size=_TWO_MIB))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


async def test_a_two_megabyte_stdout_line_does_not_kill_the_session(
        fake_cli, tmp_path, monkeypatch):
    """RED before the fix: the SDK raises 'JSON message exceeded maximum
    buffer size of 1048576 bytes' and the backend reports an is_error result
    at zero tokens — the exact incident shape."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backend = ClaudeBackend(model="claude-sonnet-5", cli_path=fake_cli)

    result = await backend.run("say something long", cwd=tmp_path, max_turns=2)

    assert "maximum buffer size" not in (result.final_text or ""), (
        "the transport cap killed the session: " + result.final_text[:200])
    assert result.is_error is False, result.final_text[:300]
    assert len(result.final_text or "") >= _TWO_MIB or "done" in (result.final_text or ""), (
        "the 2 MiB message never reached the caller")
