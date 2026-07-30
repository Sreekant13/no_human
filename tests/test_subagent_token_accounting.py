"""Subagent spend must reach the attempt ledger — exactly once.

Measured against the SDK (claude-agent-sdk, CLI 2.1.220) on 2026-07-30 with a
real one-subagent run; the recorded stream is checked in at
``testdata/subagent_usage_stream.json``. Four properties were established
byte-exactly and this module pins all four:

1. ``ResultMessage.usage`` covers ONLY the parent's own API requests. In the
   recording the parent's cache reads total 74,002 and the subagent's 8,197 —
   the ResultMessages report 74,002, so subagent spend is genuinely absent.
2. Subagent assistant messages ARE streamed, in the parent's session, tagged by
   ``parent_tool_use_id`` == the Task tool's ``tool_use_id``. ``session_id`` is
   NOT a usable discriminator: it is identical for parent and subagent.
3. The stream repeats each API response several times under one ``message_id``
   with a byte-identical usage block (7 assistant messages / 4 ids here; 2.3x
   inflation across 1,867 historical sessions). Summing without dedup roughly
   doubles the bill.
4. Streamed ``output_tokens`` is an EARLY snapshot and never reaches its final
   value (parent streamed out=9 vs ResultMessage out=1,281). Cache reads,
   cache creation and input_tokens ARE final. So the streamed breakdown alone
   cannot be trusted for output, and ``TaskNotificationMessage.usage
   .total_tokens`` — the CLI's own scalar rollup for the subagent — is what
   closes the gap.
"""

import json
from pathlib import Path

import pytest

# Exercises the REAL ClaudeBackend over the monkeypatched claude_backend.query
# seam, like tests/test_backend.py.
pytestmark = pytest.mark.real_backend

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import (
    AssistantMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
)

from no_human.agent import claude_backend
from no_human.agent.claude_backend import ClaudeBackend

FIXTURE = Path(__file__).resolve().parent.parent / "testdata" / "subagent_usage_stream.json"


def _load_recorded_stream() -> list:
    """Rebuild SDK message objects from the recorded live run."""
    msgs = []
    for rec in json.loads(FIXTURE.read_text()):
        kind = rec["type"]
        if kind == "assistant":
            msgs.append(AssistantMessage(
                content=[TextBlock(text="")],
                model="claude-haiku-4-5-20251001",
                parent_tool_use_id=rec["parent_tool_use_id"],
                usage=rec["usage"],
                message_id=rec["message_id"],
                session_id=rec["session_id"],
            ))
        elif kind == "result":
            msgs.append(ResultMessage(
                subtype=rec["subtype"], duration_ms=0, duration_api_ms=0,
                is_error=rec["is_error"], num_turns=rec["num_turns"],
                session_id=rec["session_id"], usage=rec["usage"], result="ok",
            ))
        elif kind == "task_started":
            msgs.append(TaskStartedMessage(
                subtype="task_started", data=rec, task_id=rec["task_id"],
                description="d", uuid="u", session_id="s",
                tool_use_id=rec["tool_use_id"],
            ))
        elif kind == "task_notification":
            msgs.append(TaskNotificationMessage(
                subtype="task_notification", data=rec, task_id=rec["task_id"],
                status=rec["status"], output_file="/dev/null", summary="s",
                uuid="u", session_id="s", tool_use_id=rec["tool_use_id"],
                usage=rec["usage"],
            ))
    return msgs


def _replay(messages):
    async def _q(*args, **kwargs):
        for m in messages:
            yield m
    return _q


# ----------------------------------------------------------------------------
# Independently derived expectations.
#
# These are read STRAIGHT OFF the recorded artifact by two sources that are not
# the code under test: the SDK's own ResultMessage.usage (for the parent) and
# the CLI's own TaskUsage.total_tokens scalar (for the subagent). Nothing here
# is recomputed from the accounting logic being tested.
# ----------------------------------------------------------------------------
_RECORDS = json.loads(FIXTURE.read_text())
_RESULTS = [r["usage"] for r in _RECORDS if r["type"] == "result"]
PARENT_IN = sum(u["input_tokens"] for u in _RESULTS)                    # 35
PARENT_OUT = sum(u["output_tokens"] for u in _RESULTS)                  # 1281
PARENT_CACHE_READ = sum(u["cache_read_input_tokens"] for u in _RESULTS)      # 74002
PARENT_CACHE_CREATE = sum(u["cache_creation_input_tokens"] for u in _RESULTS)  # 7149
# The CLI's own rollup for the one subagent: in+out+cache_read+cache_creation.
SUBAGENT_TOTAL = sum(r["usage"]["total_tokens"]
                     for r in _RECORDS if r["type"] == "task_notification")  # 11067
GRAND_TOTAL = (PARENT_IN + PARENT_OUT + PARENT_CACHE_READ
               + PARENT_CACHE_CREATE + SUBAGENT_TOTAL)                       # 93534


async def test_the_recording_really_does_hide_subagent_spend(tmp_path, monkeypatch):
    """Premise check: the artifact must actually exhibit the bug, or every
    other test here is vacuous. The parent's ResultMessages must exclude the
    subagent's cache reads, and the subagent must have spent something."""
    subagent_streamed_cache_read = sum(
        r["usage"]["cache_read_input_tokens"] for r in _RECORDS
        if r["type"] == "assistant" and r["parent_tool_use_id"]
    )
    assert subagent_streamed_cache_read > 0, "recording has no subagent spend to find"
    assert PARENT_CACHE_READ == sum(
        {r["message_id"]: r["usage"]["cache_read_input_tokens"] for r in _RECORDS
         if r["type"] == "assistant" and not r["parent_tool_use_id"]}.values()
    ), "ResultMessage.usage should equal the PARENT-only streamed cache reads"
    assert subagent_streamed_cache_read not in (0, PARENT_CACHE_READ)
    # Same session id for both — session_id cannot discriminate.
    sessions = {r["session_id"] for r in _RECORDS if r["type"] == "assistant"}
    assert len(sessions) == 1, "subagent shares the parent's session_id"


async def test_ledger_totals_match_the_independently_derived_grand_total(
    tmp_path, monkeypatch,
):
    """The headline: what lands in the attempt ledger must equal
    (sum of every ResultMessage) + (the CLI's own subagent rollup)."""
    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    ledger = (result.tokens_used + result.cache_read_tokens
              + result.cache_creation_tokens)
    assert ledger == GRAND_TOTAL == 93_534
    # And the split is exact, not merely the total.
    assert result.cache_read_tokens == PARENT_CACHE_READ + 8_197
    assert result.cache_creation_tokens == PARENT_CACHE_CREATE + 2_852
    assert result.tokens_used == PARENT_IN + PARENT_OUT + 18


async def test_subagent_spend_is_reported_separately_too(tmp_path, monkeypatch):
    """The rollup is auditable: the subagent share is exposed on its own so a
    reader can tell the ledger grew for a REASON, not from a summation slip."""
    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    sub = (result.subagent_tokens_used + result.subagent_cache_read_tokens
           + result.subagent_cache_creation_tokens)
    assert sub == SUBAGENT_TOTAL == 11_067
    assert result.subagent_count == 1


async def test_every_result_message_counts_not_just_the_last(tmp_path, monkeypatch):
    """One `query()` emitted TWO successful ResultMessages (turns=3 then
    turns=1). The backend used to overwrite its totals on each one, so the
    first result's 53,821 cache reads were silently dropped. Sum, don't
    replace — with a single result this is identical to the old behaviour."""
    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert len(_RESULTS) == 2, "fixture must contain the multi-result shape"
    last_only = _RESULTS[-1]["cache_read_input_tokens"]
    assert result.cache_read_tokens > last_only
    assert result.cache_read_tokens == PARENT_CACHE_READ + 8_197


# ----------------------------------------------------------------------------
# Synthetic stream with DELIBERATE duplicate message_ids — the dedup mutant is
# meant to die here, loudly, with a number that is exactly double.
# ----------------------------------------------------------------------------
def _synthetic_duplicates():
    """One parent response repeated 3x, one subagent response repeated 2x.

    Hand-computed expectation, independent of the implementation:
      parent  : ResultMessage says in 100 + out 900 + cr 5,000 + cc 300 = 6,300
      subagent: TaskUsage.total_tokens = 2,222
      grand   : 8,522
    A naive sum would report the parent stream 3x and the subagent 2x.
    """
    usage_p = {"input_tokens": 100, "output_tokens": 7,
               "cache_read_input_tokens": 5_000, "cache_creation_input_tokens": 300}
    usage_s = {"input_tokens": 20, "output_tokens": 2,
               "cache_read_input_tokens": 2_000, "cache_creation_input_tokens": 150}
    msgs = []
    for _ in range(3):
        msgs.append(AssistantMessage(content=[TextBlock(text="")], model="m",
                                     parent_tool_use_id=None, usage=usage_p,
                                     message_id="msg_parent_1", session_id="sess"))
    msgs.append(TaskStartedMessage(subtype="task_started", data={}, task_id="t1",
                                   description="d", uuid="u", session_id="sess",
                                   tool_use_id="toolu_sub"))
    for _ in range(2):
        msgs.append(AssistantMessage(content=[TextBlock(text="")], model="m",
                                     parent_tool_use_id="toolu_sub", usage=usage_s,
                                     message_id="msg_sub_1", session_id="sess"))
    msgs.append(TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="t1", status="completed",
        output_file="/dev/null", summary="s", uuid="u", session_id="sess",
        tool_use_id="toolu_sub",
        usage={"total_tokens": 2_222, "tool_uses": 1, "duration_ms": 5}))
    msgs.append(ResultMessage(
        subtype="success", duration_ms=0, duration_api_ms=0, is_error=False,
        num_turns=2, session_id="sess", result="done",
        usage={"input_tokens": 100, "output_tokens": 900,
               "cache_read_input_tokens": 5_000,
               "cache_creation_input_tokens": 300}))
    return msgs


async def test_duplicate_message_ids_are_counted_once(tmp_path, monkeypatch):
    """Repeated message_ids must not inflate the bill. This is the mutation
    target: delete the dedup and the subagent share doubles."""
    monkeypatch.setattr(claude_backend, "query", _replay(_synthetic_duplicates()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    total = (result.tokens_used + result.cache_read_tokens
             + result.cache_creation_tokens)
    assert total == 8_522, f"expected 6300 parent + 2222 subagent, got {total}"
    assert result.subagent_cache_read_tokens == 2_000, "counted the subagent twice"
    assert result.subagent_cache_creation_tokens == 150
    assert result.subagent_tokens_used == 2_222 - 2_000 - 150


async def test_a_run_with_no_subagents_is_unchanged(tmp_path, monkeypatch):
    """No Task tool, no behaviour change — the rollup must be inert."""
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 700, "cache_creation_input_tokens": 60}
    msgs = [
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id=None, usage=usage,
                         message_id="msg_only", session_id="sess"),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage=usage),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.tokens_used == 15
    assert result.cache_read_tokens == 700
    assert result.cache_creation_tokens == 60
    assert result.subagent_count == 0
    assert result.subagent_tokens_used == 0


async def test_a_subagent_the_stream_never_showed_still_counts(tmp_path, monkeypatch):
    """The SDK warns that some tasks report only a terminal TaskUpdatedMessage
    and may emit no assistant messages we can see. When only the scalar rollup
    arrives, bank the whole scalar rather than losing the spend — undercounting
    is the failure mode this whole change exists to end."""
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t9",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_ghost"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t9",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_ghost",
            usage={"total_tokens": 4_444, "tool_uses": 1, "duration_ms": 5}),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_tokens_used == 4_444
    assert result.subagent_cache_read_tokens == 0
    assert (result.tokens_used + result.cache_read_tokens
            + result.cache_creation_tokens) == 2 + 10 + 4_444


async def test_the_mid_attempt_usage_events_are_deduped_too(tmp_path, monkeypatch):
    """The orchestrator's in-flight budget watch adds up every `usage` event it
    sees. The backend emitted one per assistant message, duplicates included,
    so the watch ran ~2.3x hot and could abort a healthy attempt early. Emit
    one event per message_id."""
    monkeypatch.setattr(claude_backend, "query", _replay(_synthetic_duplicates()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    events = []
    async for ev in backend.stream("go", cwd=tmp_path, max_turns=10):
        if ev.kind == "usage":
            events.append(ev)

    assert len(events) == 2, f"one per unique message_id, got {len(events)}"
    watched = sum(e.meta["tokens_used"] + e.meta["cache_read_tokens"]
                  for e in events)
    assert watched == (107 + 5_000) + (22 + 2_000)
