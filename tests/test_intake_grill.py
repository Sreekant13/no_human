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


# ------------- answering-pass OUTCOME telemetry (2026-08-06 live) ----------- #
#
# The defect these cover is not "the answering pass fails" — it is that it
# failed INVISIBLY. Every branch of the pass sits under an advisory `except`
# whose contract (never block a task) is correct and unchanged, so before the
# outcome sink the suite was exactly as green at a 0% answer rate as at 100%.
# Live on 2026-08-06, in two independent runs: "emitted no block", "Expecting
# value: line 1 column 1 (char 0)", "all N answerable question(s) left
# unanswered". Nothing counted any of it.

_BAD_BLOCK = (  # the regex MATCHES; the contents are not JSON
    "GRILL_ANSWERS_START\nsure! here are the answers: q0 -> src/app.py\n"
    "GRILL_ANSWERS_END"
)
_NON_OBJECT_BLOCK = (  # valid JSON, wrong shape — data.get() would explode
    "GRILL_ANSWERS_START\n[{\"i\": 0, \"answer\": \"x\"}]\nGRILL_ANSWERS_END"
)


class _Sink:
    """Records what the production code classified this pass as."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, outcome, fields):
        from no_human.intake.evaluator import GRILL_ANSWERING_OUTCOMES
        assert outcome in GRILL_ANSWERING_OUTCOMES, outcome
        self.calls.append((outcome, fields))

    @property
    def outcome(self) -> str:
        assert len(self.calls) == 1, f"expected ONE outcome, got {self.calls}"
        return self.calls[0][0]

    @property
    def fields(self) -> dict:
        assert len(self.calls) == 1, f"expected ONE outcome, got {self.calls}"
        return self.calls[0][1]


@pytest.mark.asyncio
async def test_outcome_parsed_first_try_is_recorded(tmp_path):
    from no_human.intake.evaluator import grill_spec

    sink = _Sink()
    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=_ScriptedBackend([_ANSWERS_BLOCK]),
                          questions=_questions(), outcome_sink=sink)
    assert sink.outcome == "parsed_first_try"
    # The number that matters is not just "it parsed" — it is how many
    # answers LANDED. A parsed block that applied nothing is the live
    # "all N answerable question(s) left unanswered" symptom.
    assert sink.fields == {"answers_applied": 1, "answerable": 1,
                           "timed_out": False}
    assert qa[0].answer.startswith("src/app.py:42")


@pytest.mark.asyncio
async def test_outcome_parsed_after_tool_less_retry_is_recorded(tmp_path):
    from no_human.intake.evaluator import grill_spec

    sink = _Sink()
    await grill_spec("t", "d", [], tmp_path,
                     backend=_ScriptedBackend(["ran out of turns",
                                               _ANSWERS_BLOCK]),
                     questions=_questions(), outcome_sink=sink)
    assert sink.outcome == "parsed_after_tool_less_retry"
    assert sink.fields["answers_applied"] == 1


@pytest.mark.asyncio
async def test_outcome_no_block_after_retry_is_recorded(tmp_path):
    from no_human.intake.evaluator import grill_spec

    sink = _Sink()
    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=_ScriptedBackend(["no block", "still none"]),
                          questions=_questions(), outcome_sink=sink)
    assert sink.outcome == "no_block_after_retry"
    assert sink.fields == {"answers_applied": 0, "answerable": 1,
                           "timed_out": False}
    assert qa[0].answer == ""  # unanswered, never fabricated


@pytest.mark.asyncio
async def test_outcome_error_is_recorded_when_the_backend_raises(tmp_path):
    from no_human.intake.evaluator import grill_spec

    class _Boom:
        async def run(self, *a, **k):
            raise RuntimeError("down")

    sink = _Sink()
    qa = await grill_spec("t", "d", [], tmp_path, backend=_Boom(),
                          questions=_questions(), outcome_sink=sink)
    assert sink.outcome == "error"
    assert sink.fields["error"] == "RuntimeError"
    assert qa[0].answer == ""


@pytest.mark.asyncio
async def test_a_broken_outcome_sink_never_changes_the_answers(tmp_path):
    """Instrumentation may degrade the record; it may not change what the
    advisory call DOES (same rule _record_usage already holds to)."""
    from no_human.intake.evaluator import grill_spec

    def _explode(outcome, fields):
        raise RuntimeError("sink down")

    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=_ScriptedBackend([_ANSWERS_BLOCK]),
                          questions=_questions(), outcome_sink=_explode)
    assert qa is not None and qa[0].answer.startswith("src/app.py:42")
    assert qa[0].source == "repo-evidence"


@pytest.mark.asyncio
async def test_no_outcome_is_recorded_when_no_answering_pass_runs(tmp_path):
    """An absent pass is not a failed one. Every question carved out → no
    session is spent, so nothing may land in the denominator."""
    from no_human.intake.evaluator import GrillQA, grill_spec

    sink = _Sink()
    gated = [GrillQA(question="Rotate the credential?",
                     decision_it_changes="auth", carve_out="access")]
    await grill_spec("t", "d", [], tmp_path, backend=_ScriptedBackend([]),
                     questions=gated, outcome_sink=sink)
    assert sink.calls == []


# ---- matched-but-unparseable: the branch with NO recovery before this ------ #

@pytest.mark.asyncio
async def test_unparseable_block_is_retried_and_can_recover(tmp_path):
    """Live 2026-08-06: "grill answering failed ... Expecting value: line 1
    column 1 (char 0)" — the regex MATCHED, the contents were not JSON. The old
    code branched on `if not m` only, so this case skipped the retry entirely
    and raised out of loads_lenient into the advisory handler with zero
    recovery attempts."""
    from no_human.intake.evaluator import grill_spec

    sink = _Sink()
    be = _ScriptedBackend([_BAD_BLOCK, _ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", [], tmp_path, backend=be,
                          questions=_questions(), outcome_sink=sink)
    assert len(be.prompts) == 2, "the unparseable block must trigger a retry"
    assert sink.outcome == "parsed_after_tool_less_retry"
    assert qa[0].answer.startswith("src/app.py:42")
    # The retry says what was ACTUALLY wrong, not the turns-exhausted story.
    p = be.prompts[1].lower()
    assert "not valid json" in p and "strict json" in p
    assert "ran out of turns" not in p
    # Same invariant as the blockless fallback: a tool-less session has read
    # nothing, so it may NEVER stamp repo-evidence — even though the scripted
    # block claims exactly that.
    assert qa[0].source == "assumption"


@pytest.mark.asyncio
async def test_unparseable_twice_is_classified_and_bounded(tmp_path):
    from no_human.intake.evaluator import grill_spec

    sink = _Sink()
    be = _ScriptedBackend([_BAD_BLOCK, _BAD_BLOCK, _ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", [], tmp_path, backend=be,
                          questions=_questions(), outcome_sink=sink)
    # BOUNDED: exactly one recovery call, so the pass still bills at most two
    # sessions — the third scripted text is never reached.
    assert len(be.prompts) == 2
    assert sink.outcome == "unparseable_after_retry"
    assert qa[0].answer == ""


@pytest.mark.asyncio
async def test_a_block_that_parses_to_a_non_object_is_unparseable(tmp_path):
    """Valid JSON of the wrong shape: `data.get("answers")` on a list is an
    AttributeError one line later, i.e. the same failure wearing a different
    exception."""
    from no_human.intake.evaluator import grill_spec

    sink = _Sink()
    be = _ScriptedBackend([_NON_OBJECT_BLOCK, _ANSWERS_BLOCK])
    qa = await grill_spec("t", "d", [], tmp_path, backend=be,
                          questions=_questions(), outcome_sink=sink)
    assert len(be.prompts) == 2
    assert sink.outcome == "parsed_after_tool_less_retry"
    assert qa[0].answer.startswith("src/app.py:42")


@pytest.mark.asyncio
async def test_parse_helper_separates_no_block_from_unparseable():
    """The two failures have different fixes; conflating them is how the
    unparseable case shipped with no recovery at all."""
    from no_human.intake.evaluator import _parse_answers_block

    assert _parse_answers_block("nothing here") == (None, "no_block")
    assert _parse_answers_block(_BAD_BLOCK) == (None, "unparseable")
    assert _parse_answers_block(_NON_OBJECT_BLOCK) == (None, "unparseable")
    data, why = _parse_answers_block(_ANSWERS_BLOCK)
    assert why == "" and data["answers"][0]["i"] == 0


# --------- parsed-but-answered-NOTHING, and the questions pass (r2) --------- #
#
# An independent review of the outcome-telemetry branch found the metric was a
# PARSE rate wearing an ANSWER rate's name, and proved it with a mutant that
# SURVIVED the whole suite: replacing the real counter with a parse-only proxy
# (`answers_applied = 1 if data is not None else 0`) changed nothing anywhere.
# Every existing test above happened to pin a case where the two agree — the
# parsed cases applied exactly 1 answer, the unparsed ones applied 0. The one
# case that separates them is the LIVE symptom this work exists for: a block
# that parses cleanly and applies ZERO answers ("all N answerable question(s)
# left unanswered", 2026-08-06).

_EMPTY_ANSWERS_BLOCK = (  # parses perfectly; contains no answers at all
    "GRILL_ANSWERS_START\n" + json.dumps({"answers": []}) + "\nGRILL_ANSWERS_END"
)
_BLANK_ANSWER_BLOCK = (  # parses; the answer texts are empty, so none apply
    "GRILL_ANSWERS_START\n"
    + json.dumps({"answers": [{"i": 0, "answer": "", "source": "assumption"}]})
    + "\nGRILL_ANSWERS_END"
)
_WRONG_INDEX_BLOCK = (  # parses; answers only the CARVE-OUT, which is ignored
    "GRILL_ANSWERS_START\n"
    + json.dumps({"answers": [{"i": 1, "answer": "yes, rotate it",
                               "source": "assumption"}]})
    + "\nGRILL_ANSWERS_END"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [_EMPTY_ANSWERS_BLOCK, _BLANK_ANSWER_BLOCK,
                                   _WRONG_INDEX_BLOCK])
async def test_a_pass_that_parses_and_applies_nothing_reports_zero(
        tmp_path, block):
    """The case the outcome alone cannot express: PARSED, and zero answers
    landed. `answers_applied` must be the real count, not "did it parse".

    Three shapes reach it — an empty answers list, blank answer text, and an
    answer aimed at a carve-out index the pass is forbidden to apply — because
    the counter has to survive all three, not just the tidy one.
    """
    from no_human.intake.evaluator import grill_spec

    sink = _Sink()
    qa = await grill_spec("t", "d", [], tmp_path,
                          backend=_ScriptedBackend([block]),
                          questions=_questions(), outcome_sink=sink)
    # It PARSED — so the outcome split says "healthy" and cannot be the guard.
    assert sink.outcome == "parsed_first_try"
    assert sink.fields == {"answers_applied": 0, "answerable": 1,
                           "timed_out": False}
    assert qa[0].answer == ""       # nothing landed, nothing fabricated
    assert qa[1].answer.startswith("HUMAN-GATED")   # carve-out untouched


@pytest.mark.asyncio
async def test_answers_applied_counts_every_answer_not_just_one(tmp_path):
    """The other half of the same guard: a proxy that reports 1 whenever the
    block parsed is also wrong upward. Two answerable questions, both
    answered, must report 2."""
    from no_human.intake.evaluator import GrillQA, grill_spec

    qs = [GrillQA(question="q0", decision_it_changes="d0"),
          GrillQA(question="q1", decision_it_changes="d1")]
    block = ("GRILL_ANSWERS_START\n"
             + json.dumps({"answers": [
                 {"i": 0, "answer": "a0", "source": "assumption"},
                 {"i": 1, "answer": "a1", "source": "assumption"}]})
             + "\nGRILL_ANSWERS_END")
    sink = _Sink()
    await grill_spec("t", "d", [], tmp_path,
                     backend=_ScriptedBackend([block]),
                     questions=qs, outcome_sink=sink)
    assert sink.fields["answers_applied"] == 2
    assert sink.fields["answerable"] == 2


@pytest.mark.asyncio
async def test_a_timed_out_answering_pass_is_no_block_and_says_so(tmp_path):
    """GRILL_ANSWERING_OUTCOMES used to comment that `error` covered a
    timeout. It does not: `_bounded_run` swallows the TimeoutError and hands
    back a `final_text=""` sentinel, so the pass books `no_block_after_retry`
    — and that is the MOST EXPENSIVE failure there is (max_turns=8, spend
    unrecoverable) sitting in a bucket whose documentation denied it existed.

    This pins the corrected reading: the bucket stays `no_block_after_retry`
    (a timed-out session genuinely emitted no block) and the timeout stays
    countable through the `timed_out` field.
    """
    import asyncio

    from no_human.intake import evaluator as ev
    from no_human.intake.evaluator import grill_spec

    class _Hangs:
        async def run(self, *a, **k):
            await asyncio.sleep(10)

    sink = _Sink()
    orig = ev._LLM_TIMEOUT_S
    ev._LLM_TIMEOUT_S = 0.01
    try:
        qa = await grill_spec("t", "d", [], tmp_path, backend=_Hangs(),
                              questions=_questions(), outcome_sink=sink)
    finally:
        ev._LLM_TIMEOUT_S = orig
    assert sink.outcome == "no_block_after_retry"   # NOT "error"
    assert sink.fields["timed_out"] is True
    assert sink.fields["answers_applied"] == 0
    assert qa[0].answer == ""


# The QUESTIONS pass: the uncovered half of the same defect class. It carried
# the identical unguarded parse the answers pass was fixed for — `loads_lenient`
# outside any try, then `data.get("questions")` on whatever came back — and it
# was uninstrumented, so a malformed block produced no event of any kind.

class _QSink(_Sink):
    def __call__(self, outcome, fields):
        from no_human.intake.evaluator import GRILL_QUESTIONS_OUTCOMES
        assert outcome in GRILL_QUESTIONS_OUTCOMES, outcome
        self.calls.append((outcome, fields))


_BAD_QUESTIONS_BLOCK = (  # regex MATCHES, contents are not JSON
    "GRILL_JSON_START\nsure! here are some questions\nGRILL_JSON_END"
)
_NON_OBJECT_QUESTIONS_BLOCK = (  # valid JSON, wrong shape — .get() explodes
    "GRILL_JSON_START\n[{\"question\": \"q\"}]\nGRILL_JSON_END"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [_BAD_QUESTIONS_BLOCK,
                                 _NON_OBJECT_QUESTIONS_BLOCK])
async def test_a_malformed_questions_block_cannot_raise(tmp_path, bad):
    """It used to. `loads_lenient` sat outside the try, so an invalid block
    raised ValueError and a non-object raised AttributeError straight into
    the blanket handler — no retry, no event, no way to tell either from a
    backend outage."""
    from no_human.intake.evaluator import generate_grill_questions

    sink = _QSink()
    be = _ScriptedBackend([bad, bad])
    qa = await generate_grill_questions("t", "d", [], backend=be,
                                        outcome_sink=sink)
    assert qa is None
    # It also gets the recovery attempt the answers pass gets — the old code
    # branched on "no block" only, so this class skipped the retry entirely.
    assert len(be.prompts) == 2
    assert sink.outcome == "unparseable_after_retry"
    assert sink.fields["questions"] == 0


@pytest.mark.asyncio
async def test_a_malformed_questions_block_recovers_on_the_retry(tmp_path):
    """The recovery is not theoretical: a bad first block followed by a good
    one yields questions, and says it needed the retry."""
    from no_human.intake.evaluator import generate_grill_questions

    sink = _QSink()
    be = _ScriptedBackend([_BAD_QUESTIONS_BLOCK, _QUESTIONS_BLOCK])
    qa = await generate_grill_questions("t", "d", [], backend=be,
                                        outcome_sink=sink)
    assert qa and sink.outcome == "parsed_after_retry"
    assert sink.fields["questions"] == len(qa)


@pytest.mark.asyncio
async def test_the_questions_pass_reports_every_outcome_exactly_once(tmp_path):
    """One event per pass that runs, whatever it did — including the happy
    path, so the metric has a denominator."""
    from no_human.intake.evaluator import generate_grill_questions

    for texts, expect in (
        ([_QUESTIONS_BLOCK], "parsed_first_try"),
        (["nothing", "still nothing"], "no_block_after_retry"),
        # Parsed, and produced no usable questions: not the same failure as
        # never parsing, and it would otherwise be filed under a success.
        (["GRILL_JSON_START\n{}\nGRILL_JSON_END"], "empty_after_parse"),
        (['GRILL_JSON_START\n{"questions": [{"decision_it_changes": "d"}]}'
          "\nGRILL_JSON_END"], "empty_after_parse"),
    ):
        sink = _QSink()
        await generate_grill_questions("t", "d", [], outcome_sink=sink,
                                       backend=_ScriptedBackend(texts))
        assert sink.outcome == expect, texts


@pytest.mark.asyncio
async def test_the_questions_pass_records_error_when_the_backend_raises():
    from no_human.intake.evaluator import generate_grill_questions

    class _Boom:
        async def run(self, *a, **k):
            raise RuntimeError("down")

    sink = _QSink()
    qa = await generate_grill_questions("t", "d", [], backend=_Boom(),
                                        outcome_sink=sink)
    assert qa is None
    assert sink.outcome == "error" and sink.fields["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_a_broken_questions_sink_never_changes_the_questions():
    """Same contract as the answering sink: instrumentation may degrade the
    record, it may not change what the advisory call DOES."""
    from no_human.intake.evaluator import generate_grill_questions

    def _explode(outcome, fields):
        raise RuntimeError("sink down")

    qa = await generate_grill_questions(
        "t", "d", [], backend=_ScriptedBackend([_QUESTIONS_BLOCK]),
        outcome_sink=_explode)
    assert qa and qa[0].question.startswith("Which of the 4")


@pytest.mark.asyncio
async def test_questions_parse_helper_separates_no_block_from_unparseable():
    from no_human.intake.evaluator import _parse_questions_block

    assert _parse_questions_block("nothing here") == (None, "no_block")
    assert _parse_questions_block(_BAD_QUESTIONS_BLOCK) == (None, "unparseable")
    assert (_parse_questions_block(_NON_OBJECT_QUESTIONS_BLOCK)
            == (None, "unparseable"))
    data, why = _parse_questions_block(_QUESTIONS_BLOCK)
    assert why == "" and isinstance(data.get("questions"), list)


# ---- r3: two NEW questions-pass behaviours that shipped unguarded ---------- #
# Both were live and correct on this branch and both survived the whole author
# suite when broken, on a branch whose entire purpose is making a regression
# visible. Mutants re-run in the commit message.

@pytest.mark.asyncio
async def test_a_timed_out_questions_pass_says_so():
    """The questions pass threads `timed_out` onto its outcome record, exactly
    as the answering pass does — and that was unguarded: a mutant deleting BOTH
    assignments (first call and retry) left 50 tests passing.

    It matters for the same reason it matters on the answering side: the
    timeout sentinel is `final_text=""`, indistinguishable by its text from
    "the model replied with nothing", and the two have opposite fixes. The
    bucket is deliberately `no_block_after_retry` for both, so the FIELD is
    the only thing that can tell them apart.
    """
    import asyncio

    from no_human.intake import evaluator as ev
    from no_human.intake.evaluator import generate_grill_questions

    class _Hangs:
        async def run(self, *a, **k):
            await asyncio.sleep(10)

    sink = _QSink()
    orig = ev._LLM_TIMEOUT_S
    ev._LLM_TIMEOUT_S = 0.01
    try:
        qa = await generate_grill_questions("t", "d", [], backend=_Hangs(),
                                            outcome_sink=sink)
    finally:
        ev._LLM_TIMEOUT_S = orig
    assert qa is None
    assert sink.outcome == "no_block_after_retry"   # NOT "error"
    assert sink.fields["timed_out"] is True
    assert sink.fields["questions"] == 0


@pytest.mark.asyncio
async def test_a_timed_out_first_call_still_says_so_when_the_retry_succeeds():
    """The other assignment: `timed_out` is OR-ed across the two calls, so a
    first call that timed out is still reported when the retry recovers.
    Without it, the most expensive half of the pass goes unbilled in the
    metric precisely when the pass looks healthy."""
    import asyncio

    from no_human.intake import evaluator as ev
    from no_human.intake.evaluator import generate_grill_questions

    class _HangsThenAnswers:
        def __init__(self):
            self.calls = 0

        async def run(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)
            return _result(_QUESTIONS_BLOCK)

    be = _HangsThenAnswers()
    sink = _QSink()
    orig = ev._LLM_TIMEOUT_S
    ev._LLM_TIMEOUT_S = 0.01
    try:
        qa = await generate_grill_questions("t", "d", [], backend=be,
                                            outcome_sink=sink)
    finally:
        ev._LLM_TIMEOUT_S = orig
    assert qa and be.calls == 2
    assert sink.outcome == "parsed_after_retry"
    assert sink.fields["timed_out"] is True


@pytest.mark.asyncio
async def test_unparseable_then_blockless_is_still_unparseable_after_retry():
    """`elif why2 == "unparseable" or why == "unparseable"` — the second half
    of that clause was unguarded on the questions side: dropping it left 50
    tests passing, because every existing case has the SAME `why` on both
    tries.

    The shape that separates them: try 1 emits a block that is not JSON, try 2
    emits no block at all. The pass DID produce a malformed block, so the
    prompt is the thing to fix; filing it as `no_block_after_retry` would point
    the reader at the turn budget instead. (The identical clause on the
    ANSWERING side is pre-existing and covered elsewhere.)
    """
    from no_human.intake.evaluator import generate_grill_questions

    be = _ScriptedBackend([_BAD_QUESTIONS_BLOCK, "sorry, nothing to add"])
    sink = _QSink()
    qa = await generate_grill_questions("t", "d", [], backend=be,
                                        outcome_sink=sink)
    assert qa is None
    assert len(be.prompts) == 2                    # it did get its retry
    assert sink.outcome == "unparseable_after_retry"
    assert sink.fields["questions"] == 0


def test_the_parsed_subset_is_declared_and_agrees_with_the_enum():
    """r3: the answer-rate denominator is `GRILL_ANSWERING_PARSED_OUTCOMES`.

    The landed commit message claimed the subset was "derived from the enum …
    so an outcome added later cannot silently fall out of the denominator". It
    was derived by `o.startswith("parsed")` — a naming convention. This pins
    the convention in BOTH directions instead of asserting it in prose, so the
    two drifts that can empty or shrink the denominator fail here:

      * a member declared parsed that is not in the enum at all (a rename);
      * the enum and the prefix disagreeing either way — a new `parsed_*`
        outcome not enrolled, or a member enrolled without the prefix.

    It cannot catch a future parsing outcome named `recovered_*` AND left out
    of the declaration; nothing can, short of the enum carrying the semantics.
    What it does buy is that the convention now has to be broken deliberately.
    """
    from no_human.intake.evaluator import (
        GRILL_ANSWERING_OUTCOMES, GRILL_ANSWERING_PARSED_OUTCOMES as PARSED,
    )

    assert PARSED, "an empty denominator reports a permanent zero answer rate"
    assert set(PARSED) <= set(GRILL_ANSWERING_OUTCOMES)
    assert (set(PARSED)
            == {o for o in GRILL_ANSWERING_OUTCOMES if o.startswith("parsed")})


@pytest.mark.asyncio
async def test_every_outcome_the_answering_pass_emits_after_parsing_is_in_it(
        tmp_path):
    """Non-vacuity for the pin above: the declaration is checked against what
    the REAL pass emits, not only against how the enum is spelled. Parsing
    shapes must land inside the subset; non-parsing shapes must land outside
    it, or the denominator would count passes with no answers to apply."""
    from no_human.intake.evaluator import (
        GRILL_ANSWERING_PARSED_OUTCOMES as PARSED, grill_spec,
    )

    good = ("GRILL_ANSWERS_START\n"
            + json.dumps({"answers": [{"i": 0, "answer": "a",
                                       "source": "assumption"}]})
            + "\nGRILL_ANSWERS_END")
    bad = "GRILL_ANSWERS_START\nnot json at all\nGRILL_ANSWERS_END"

    for texts, parsed in (
        ([good], True),                    # parsed_first_try
        (["ran out of turns", good], True),  # parsed_after_tool_less_retry
        ([bad, bad], False),               # unparseable_after_retry
        (["nothing", "still nothing"], False),  # no_block_after_retry
    ):
        sink = _Sink()
        await grill_spec("t", "d", [], tmp_path,
                         backend=_ScriptedBackend(texts),
                         questions=_questions(), outcome_sink=sink)
        assert (sink.outcome in PARSED) is parsed, (texts, sink.outcome)
