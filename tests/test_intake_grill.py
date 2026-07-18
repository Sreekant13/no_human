"""Intake grill (§6 operator directive 2026-07-17): every task gets the
clarifying questions a real requester would be asked — answered by a human
when present, by repo-evidenced reversible assumptions when unattended.

Hermetic: fake backends only, mirroring test_evaluator_prompt.py's pattern.
"""

from __future__ import annotations

import json

import pytest

from no_human.agent.claude_backend import AgentResult


def _result(text: str) -> AgentResult:
    return AgentResult(final_text=text, num_turns=1, is_error=False,
                       tokens_used=10, session_id="s", stop_reason="end_turn")


class _ScriptedBackend:
    """Returns scripted final_texts in order; records prompts and cwds."""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self.prompts: list[str] = []
        self.cwds: list = []

    async def run(self, prompt, *, cwd=None, **kwargs):
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        self.kwargs = getattr(self, "kwargs", [])
        self.kwargs.append(kwargs)
        return _result(self._texts.pop(0))


_QUESTIONS_BLOCK = (
    "thinking...\n"
    "GRILL_JSON_START\n"
    + json.dumps({"questions": [
        {"question": "Which of the 4 candidate files should carry the fix?",
         "decision_it_changes": "target file",
         "carve_out": "none"},
        {"question": "May I rotate the expired credential?",
         "decision_it_changes": "auth flow",
         "carve_out": "access"},
    ]})
    + "\nGRILL_JSON_END\n"
)


# ------------------------- question generation ----------------------------- #

@pytest.mark.asyncio
async def test_generate_parses_questions_with_carve_outs():
    from no_human.intake.evaluator import generate_grill_questions

    be = _ScriptedBackend([_QUESTIONS_BLOCK])
    qa = await generate_grill_questions("t", "d", ["c1"], backend=be)

    assert qa is not None and len(qa) == 2
    assert qa[0].question.startswith("Which of the 4")
    assert qa[0].decision_it_changes == "target file"
    assert qa[0].carve_out == "none"
    assert qa[0].answer == ""
    assert qa[1].carve_out == "access"
    # The prompt carried the spec and the EVPI rubric.
    assert "t" in be.prompts[0]
    assert "decision" in be.prompts[0].lower()


@pytest.mark.asyncio
async def test_generate_unparseable_returns_none_and_never_raises():
    from no_human.intake.evaluator import generate_grill_questions

    # One-element script: the retry pops an exhausted list (IndexError),
    # exercising the retry-call-raises → None path — the production
    # backend-error-on-retry case. Both-replies-blockless lives in
    # test_generate_gives_up_after_one_retry.
    assert await generate_grill_questions(
        "t", "d", [], backend=_ScriptedBackend(["no block here"])) is None

    class _Boom:
        async def run(self, *a, **k):
            raise RuntimeError("backend down")

    assert await generate_grill_questions("t", "d", [], backend=_Boom()) is None


@pytest.mark.asyncio
async def test_generate_caps_at_eight_questions():
    from no_human.intake.evaluator import generate_grill_questions

    many = {"questions": [
        {"question": f"q{i}", "decision_it_changes": f"d{i}", "carve_out": "none"}
        for i in range(12)
    ]}
    be = _ScriptedBackend([
        "GRILL_JSON_START\n" + json.dumps(many) + "\nGRILL_JSON_END"])
    qa = await generate_grill_questions("t", "d", [], backend=be)
    assert qa is not None and len(qa) == 8


# ------------------------- repo-evidenced answering ------------------------ #

def _questions():
    from no_human.intake.evaluator import GrillQA
    return [
        GrillQA(question="Which file carries the fix?",
                decision_it_changes="target file"),
        GrillQA(question="May I rotate the expired credential?",
                decision_it_changes="auth flow", carve_out="access"),
    ]


_ANSWERS_BLOCK = (
    "GRILL_ANSWERS_START\n"
    + json.dumps({"answers": [
        {"i": 0, "answer": "src/app.py:42 — the only formatBytes caller",
         "source": "repo-evidence"},
    ]})
    + "\nGRILL_ANSWERS_END"
)


@pytest.mark.asyncio
async def test_grill_spec_answers_from_repo_and_gates_carve_outs(tmp_path):
    from no_human.intake.evaluator import grill_spec

    be = _ScriptedBackend([_ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", ["c1"], tmp_path,
                          backend=be, questions=_questions())

    assert qa is not None and len(qa) == 2
    # The answering session ran IN THE REPO (resolve_assumptions is
    # repo-blind, cwd=tempdir — the grill must not be).
    assert be.cwds == [tmp_path]
    assert qa[0].answer.startswith("src/app.py:42")
    assert qa[0].source == "repo-evidence"
    # Carve-out never self-answered, and never sent to the model.
    assert qa[1].carve_out == "access"
    assert qa[1].answer == "HUMAN-GATED: not self-answerable"
    assert qa[1].source == ""
    assert "credential" not in be.prompts[0]


@pytest.mark.asyncio
async def test_grill_spec_failure_returns_questions_unanswered(tmp_path):
    from no_human.intake.evaluator import grill_spec

    class _Boom:
        async def run(self, *a, **k):
            raise RuntimeError("down")

    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=_Boom(), questions=_questions())
    assert qa is not None and len(qa) == 2
    assert qa[0].answer == ""  # unanswered, not fabricated

    qa2 = await grill_spec("t", "d", [], tmp_path,
                           backend=_ScriptedBackend(["garbage"]),
                           questions=_questions())
    assert qa2 is not None and qa2[0].answer == ""


@pytest.mark.asyncio
async def test_grill_spec_generates_when_not_injected(tmp_path):
    from no_human.intake.evaluator import grill_spec

    be = _ScriptedBackend([_QUESTIONS_BLOCK, _ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", [], tmp_path, backend=be)
    assert qa is not None and len(qa) == 2
    assert qa[0].answer.startswith("src/app.py:42")


# ---------------------- answering robustness (v10 drill) ------------------- #

@pytest.mark.asyncio
async def test_answers_prompt_demands_the_block_and_reserves_a_turn():
    """v10 drill: 2/2 budget burns had EMPTY answers — the 8-turn session
    spent itself exploring and never emitted GRILL_ANSWERS. The prompt must
    make the block non-negotiable and reserve the final turn for it."""
    from no_human.intake.evaluator import _GRILL_ANSWERS_PROMPT
    p = _GRILL_ANSWERS_PROMPT.lower()
    assert "final turn" in p
    assert "required" in p
    assert "never end" in p


@pytest.mark.asyncio
async def test_grill_spec_retries_empty_answering_once(tmp_path):
    """One retry on a blockless answering reply — the v10 failure mode."""
    from no_human.intake.evaluator import grill_spec

    be = _ScriptedBackend(["exploring... ran out of turns", _ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=be, questions=_questions())
    assert len(be.prompts) == 2
    assert qa[0].answer.startswith("src/app.py:42")


@pytest.mark.asyncio
async def test_grill_spec_gives_up_after_one_retry(tmp_path):
    from no_human.intake.evaluator import grill_spec

    be = _ScriptedBackend(["no block", "still no block"])
    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=be, questions=_questions())
    assert len(be.prompts) == 2
    assert qa is not None and qa[0].answer == ""


# ------------------ question-gen robustness (v11 live signal) --------------- #

@pytest.mark.asyncio
async def test_generate_retries_blockless_reply_once():
    """v11 live occurrence: the 1-shot question-gen pass ended without a
    GRILL_JSON block (same silent single-emit failure class #125 fixed for
    the ANSWERS pass — v10 proved that class 6/6 lethal). One retry, same
    prompt/backend."""
    from no_human.intake.evaluator import generate_grill_questions

    be = _ScriptedBackend(["rambling, no block", _QUESTIONS_BLOCK])
    qa = await generate_grill_questions("t", "d", [], backend=be)
    assert len(be.prompts) == 2
    assert qa is not None and len(qa) == 2


@pytest.mark.asyncio
async def test_generate_gives_up_after_one_retry():
    from no_human.intake.evaluator import generate_grill_questions

    be = _ScriptedBackend(["no block", "still no block"])
    qa = await generate_grill_questions("t", "d", [], backend=be)
    assert len(be.prompts) == 2
    assert qa is None

# ---------------- answering fallback (v11 live root cause) ----------------- #

@pytest.mark.asyncio
async def test_answering_retry_is_a_toolless_final_attempt(tmp_path):
    """v11 live root cause: in content-rich repos the 8-turn answering
    session exhausts ALL turns exploring and the SDK returns an error result
    ('Reached maximum number of turns') — a blind same-shape resample fails
    deterministically (0/6 recovery in v11). The retry must change strategy:
    tool-less, tiny turn budget, emit-now with assumption-grade answers."""
    from no_human.intake.evaluator import grill_spec

    be = _ScriptedBackend([
        "Claude Code returned an error result: Reached maximum number of turns (8)",
        _ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=be, questions=_questions())
    assert len(be.prompts) == 2
    # The fallback is a different strategy, not a resample.
    assert be.kwargs[1].get("max_turns", 99) <= 2
    assert "do not use tools" in be.prompts[1].lower()
    assert "assumption" in be.prompts[1].lower()
    # r1 blocking finding: the fallback must forbid uncited citations.
    assert "have not read in this session" in be.prompts[1].lower()
    # It recovers the answer TEXT (data, fenced downstream)...
    assert qa[0].answer.startswith("src/app.py:42")
    # ...but a tool-less session can never stamp repo-evidence: even though
    # the scripted block claims "repo-evidence", the source is DEMOTED.
    assert qa[0].source == "assumption"


@pytest.mark.asyncio
async def test_first_pass_answers_keep_their_claimed_source(tmp_path):
    """The demotion applies ONLY to the fallback path — a first-pass session
    that actually explored keeps its repo-evidence label."""
    from no_human.intake.evaluator import grill_spec

    be = _ScriptedBackend([_ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=be, questions=_questions())
    assert len(be.prompts) == 1
    assert qa[0].source == "repo-evidence"
