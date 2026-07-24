"""SCRUM-34: split proposal generator — utility-model, single-turn, strict
parse. Any failure degrades to None; it never blocks or retries beyond one
parse retry, and it never touches the task/store."""

import logging

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.config import DEFAULT_CONFIG
from no_human.core.task import Task
from no_human.intake.split_proposal import generate_split_proposal

VALID_BLOCK = """
SPLIT_JSON_START
[
  {"title": "Extract the parser", "description": "Pull the parsing logic into its own module. It currently lives inline in the orchestrator. This isolates it for reuse.", "contract": "Exposes parse(text) -> Result; no other module changes."},
  {"title": "Add the validator", "description": "Add strict validation on top of the parser output. It rejects malformed input early. This keeps downstream code simple.", "contract": "Depends on parse() from sub-task 1; exposes validate(result) -> bool."},
  {"title": "Wire the call site", "description": "Wire the new parser and validator into the existing call site. Replace the old inline logic. Keep behavior identical for valid input.", "contract": "Depends on sub-tasks 1 and 2; no new public interface."}
]
SPLIT_JSON_END
"""


def _task(title="Big task", description="A task that grew too large",
          criteria=None):
    t = Task.new(title, description=description)
    t.acceptance_criteria = criteria or ["do the thing"]
    return t


def _result(text: str) -> AgentResult:
    return AgentResult(final_text=text, num_turns=1, is_error=False,
                        tokens_used=0, session_id="s", stop_reason="end_turn")


class _ScriptedBackend:
    """Returns each entry of ``script`` in order on successive ``run`` calls."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def __call__(self, *a, **k):
        pass

    async def run(self, prompt, **kwargs):
        self.calls += 1
        text = self._script[min(self.calls - 1, len(self._script) - 1)]
        return _result(text)


class _CapturingBackend:
    """Records the model it was constructed with."""

    seen = []

    def __init__(self, *, model, readonly=False, **_):
        _CapturingBackend.seen.append(model)

    async def run(self, prompt, **kwargs):
        return _result(VALID_BLOCK)


class _RaisingBackend:
    async def run(self, prompt, **kwargs):
        raise ValueError("boom")


async def test_valid_2_to_4_drafts_returns_text():
    be = _ScriptedBackend([VALID_BLOCK])
    out = await generate_split_proposal(_task(), backend=be)
    assert out is not None
    assert "Extract the parser" in out
    assert "Add the validator" in out
    assert "Wire the call site" in out
    assert "Contract" in out
    assert be.calls == 1


async def test_defaults_to_utility_model(monkeypatch):
    _CapturingBackend.seen = []
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend", _CapturingBackend
    )
    await generate_split_proposal(_task())
    assert _CapturingBackend.seen == [DEFAULT_CONFIG["llm"]["utility_model"]]
    assert _CapturingBackend.seen[0] != DEFAULT_CONFIG["llm"]["primary_model"]
    assert _CapturingBackend.seen[0] != DEFAULT_CONFIG["llm"]["review_model"]


async def test_no_block_retries_once_then_none():
    be = _ScriptedBackend(["not a block at all", "still garbage"])
    out = await generate_split_proposal(_task(), backend=be)
    assert out is None
    assert be.calls == 2


async def test_wrong_count_returns_none():
    one_draft = """SPLIT_JSON_START
[{"title": "Only one", "description": "One sentence only here. Two now. Three now.", "contract": "c"}]
SPLIT_JSON_END"""
    five_drafts = "SPLIT_JSON_START\n[" + ", ".join(
        '{"title": "T%d", "description": "One. Two. Three.", "contract": "c"}' % i
        for i in range(5)
    ) + "]\nSPLIT_JSON_END"

    be1 = _ScriptedBackend([one_draft])
    assert await generate_split_proposal(_task(), backend=be1) is None

    be2 = _ScriptedBackend([five_drafts])
    assert await generate_split_proposal(_task(), backend=be2) is None


async def test_bad_sentence_count_returns_none():
    too_few = """SPLIT_JSON_START
[
  {"title": "A", "description": "Only one sentence.", "contract": "c"},
  {"title": "B", "description": "One. Two. Three.", "contract": "c"}
]
SPLIT_JSON_END"""
    too_many = """SPLIT_JSON_START
[
  {"title": "A", "description": "One. Two. Three. Four. Five. Six. Seven.", "contract": "c"},
  {"title": "B", "description": "One. Two. Three.", "contract": "c"}
]
SPLIT_JSON_END"""

    be1 = _ScriptedBackend([too_few])
    assert await generate_split_proposal(_task(), backend=be1) is None

    be2 = _ScriptedBackend([too_many])
    assert await generate_split_proposal(_task(), backend=be2) is None


async def test_missing_contract_returns_none():
    missing = """SPLIT_JSON_START
[
  {"title": "A", "description": "One. Two. Three."},
  {"title": "B", "description": "One. Two. Three.", "contract": "c"}
]
SPLIT_JSON_END"""
    be = _ScriptedBackend([missing])
    assert await generate_split_proposal(_task(), backend=be) is None


async def test_backend_exception_returns_none_and_logs_typename(caplog):
    with caplog.at_level(logging.WARNING, logger="no_human.split_proposal"):
        out = await generate_split_proposal(_task(), backend=_RaisingBackend())
    assert out is None
    assert "ValueError" in caplog.text
