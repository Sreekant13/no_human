"""B2: Tests for the intake grill — response parsing and step flow."""

import pytest

from no_human.intake.grill import (
    GrillQuestion,
    GrillResult,
    parse_grill_response,
)


# --------------------------------------------------------------------------- #
# parse_grill_response                                                         #
# --------------------------------------------------------------------------- #


class TestParseGrillResponse:
    def test_question_from_json_block(self):
        text = (
            'I explored the repo and found the following.\n\n'
            '```json\n'
            '{"type": "question", "question": "Which module?", '
            '"suggestions": ["A: foo", "B: bar"]}\n'
            '```'
        )
        result = parse_grill_response(text, round_n=1, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.question == "Which module?"
        assert result.suggestions == ["A: foo", "B: bar"]
        assert result.round == 1

    def test_done_from_json_block(self):
        text = (
            '```json\n'
            '{"type": "done", "title": "Add caching", '
            '"description": "Add Redis caching to the API", '
            '"acceptance_criteria": ["GET /items cached for 60s", '
            '"cache invalidated on POST"]}\n'
            '```'
        )
        result = parse_grill_response(text, round_n=3, qa_history=[{"q": "x", "a": "y"}])
        assert isinstance(result, GrillResult)
        assert result.title == "Add caching"
        assert result.description == "Add Redis caching to the API"
        assert len(result.acceptance_criteria) == 2
        assert result.qa_log == [{"q": "x", "a": "y"}]

    def test_raw_json_no_fences(self):
        text = '{"type": "question", "question": "Scope?", "suggestions": ["A: narrow"]}'
        result = parse_grill_response(text, round_n=2, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.question == "Scope?"

    def test_fallback_on_garbage(self):
        result = parse_grill_response("random text no json", round_n=1, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.round == 1
        # Should produce a fallback question
        assert "detail" in result.question.lower() or "describe" in result.question.lower()

    def test_multiple_json_blocks_uses_last(self):
        text = (
            '```json\n{"type": "question", "question": "first?"}\n```\n'
            'Actually, let me reconsider.\n'
            '```json\n{"type": "done", "title": "T", "description": "D", '
            '"acceptance_criteria": ["AC1"]}\n```'
        )
        result = parse_grill_response(text, round_n=2, qa_history=[])
        assert isinstance(result, GrillResult)
        assert result.title == "T"

    def test_done_with_empty_criteria(self):
        text = '```json\n{"type": "done", "title": "T", "description": "D", "acceptance_criteria": []}\n```'
        result = parse_grill_response(text, round_n=1, qa_history=[])
        assert isinstance(result, GrillResult)
        assert result.acceptance_criteria == []

    def test_question_missing_suggestions(self):
        text = '```json\n{"type": "question", "question": "What?"}\n```'
        result = parse_grill_response(text, round_n=1, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.suggestions == []


# --------------------------------------------------------------------------- #
# grill_step (with a fake backend)                                             #
# --------------------------------------------------------------------------- #


class FakeBackend:
    """Minimal backend that returns a canned response."""

    def __init__(self, final_text: str):
        self._text = final_text

    async def run(self, prompt, *, cwd, max_turns, effort=None):
        class _Result:
            final_text = self._text
        return _Result()


@pytest.mark.asyncio
async def test_grill_step_returns_question():
    from no_human.intake.grill import grill_step

    backend = FakeBackend(
        '```json\n{"type": "question", "question": "Which DB?", '
        '"suggestions": ["A: SQLite", "B: Postgres"]}\n```'
    )
    result = await grill_step("Add caching", None, None, [], backend)
    assert isinstance(result, GrillQuestion)
    assert result.question == "Which DB?"
    assert result.round == 1


@pytest.mark.asyncio
async def test_grill_step_returns_result():
    from no_human.intake.grill import grill_step

    backend = FakeBackend(
        '```json\n{"type": "done", "title": "Add SQLite cache", '
        '"description": "Cache API responses in SQLite", '
        '"acceptance_criteria": ["GET cached", "POST invalidates"]}\n```'
    )
    qa = [{"question": "Which DB?", "answer": "SQLite"}]
    result = await grill_step("Add caching", None, None, qa, backend)
    assert isinstance(result, GrillResult)
    assert result.title == "Add SQLite cache"
    assert len(result.acceptance_criteria) == 2


@pytest.mark.asyncio
async def test_grill_step_force_done_on_max_rounds():
    """When max_rounds is reached and agent still asks a question,
    the engine forces a GrillResult so the pipeline isn't blocked."""
    from no_human.intake.grill import grill_step

    backend = FakeBackend(
        '```json\n{"type": "question", "question": "More?"}\n```'
    )
    qa = [{"question": f"Q{i}", "answer": f"A{i}"} for i in range(4)]
    result = await grill_step("Fix X", None, None, qa, backend, max_rounds=5)
    assert isinstance(result, GrillResult)
    assert result.title == "Fix X"


@pytest.mark.asyncio
async def test_grill_step_prompt_includes_qa_history():
    """Verify the Q&A history is included in the prompt sent to the backend."""
    captured_prompts = []

    class CapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None):
            captured_prompts.append(prompt)
            class _R:
                final_text = '```json\n{"type": "done", "title": "T", "description": "D", "acceptance_criteria": ["AC"]}\n```'
            return _R()

    from no_human.intake.grill import grill_step

    qa = [{"question": "Scope?", "answer": "Narrow"}]
    await grill_step("Fix X", "desc", None, qa, CapturingBackend())
    assert "Q1: Scope?" in captured_prompts[0]
    assert "A1: Narrow" in captured_prompts[0]


# --------------------------------------------------------------------------- #
# API model round-trip                                                         #
# --------------------------------------------------------------------------- #


def test_api_models_round_trip():
    from no_human.api.models import GrillQuestionOut, GrillResultOut, GrillStepRequest

    req = GrillStepRequest(
        title="Fix X", description="Longer", repo_path="/tmp",
        qa_history=[{"question": "Q", "answer": "A"}],
    )
    assert req.title == "Fix X"
    assert len(req.qa_history) == 1

    q = GrillQuestionOut(question="Which?", suggestions=["A: x"], round=1)
    assert q.type == "question"

    r = GrillResultOut(title="T", description="D", acceptance_criteria=["AC"])
    assert r.type == "done"
