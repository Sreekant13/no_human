"""The grill answering pass's cost proportionality (2026-08-19 funnel
forensics: the exploring session is the utility lane's cost center, ~3-4x the
08-10 baseline; on the docs tier the pass burned more than that tier's entire
baseline spend, twice failing with "all answerable questions left unanswered").

Two remedies, pinned here: the exploring session's turn budget scales with the
question count (one question has never needed eight turns of probing), and a
prose-only task skips the exploring session entirely — the tool-less emit that
was the FALLBACK becomes the primary, under the same never-fabricate contract
(source "assumption", no invented citations).
"""

import pytest

from no_human.intake.evaluator import GrillQA, grill_spec


class _TurnsCapture:
    """Captures each call's max_turns and prompt; returns a canned block."""

    def __init__(self, final_text):
        self._text = final_text
        self.turns: list[int] = []
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd=None, max_turns=None, **kw):
        self.turns.append(max_turns)
        self.prompts.append(prompt)
        text = self._text

        class _R:
            final_text = text
        return _R()


def _answers(i=0):
    return ("GRILL_ANSWERS_START\n"
            f'{{"answers": [{{"i": {i}, "answer": "the default", '
            '"source": "assumption"}]}\n'
            "GRILL_ANSWERS_END").replace("}}]}", "}]}")


def _qs(n):
    return [GrillQA(question=f"q{k}?", decision_it_changes=f"d{k}")
            for k in range(n)]


@pytest.mark.asyncio
async def test_one_question_gets_a_small_probe_budget(tmp_path):
    be = _TurnsCapture(_answers())
    qa = await grill_spec("t", "d", ["c1"], tmp_path, backend=be,
                          questions=_qs(1))
    assert qa is not None
    assert be.turns == [4], "1 answerable question ⇒ 2 + 2×1 turns, not 8"


@pytest.mark.asyncio
async def test_many_questions_keep_the_eight_turn_ceiling(tmp_path):
    be = _TurnsCapture(_answers())
    await grill_spec("t", "d", ["c1"], tmp_path, backend=be, questions=_qs(5))
    assert be.turns == [8], "the ceiling is unchanged — scaling only shrinks"


@pytest.mark.asyncio
async def test_probe_false_is_a_single_tool_less_emit(tmp_path):
    """The proportionality rung: no exploring session at all — one 2-turn
    tool-less call whose prompt carries the same never-fabricate contract the
    fallback always had."""
    be = _TurnsCapture(_answers())
    qa = await grill_spec("t", "d", ["c1"], tmp_path, backend=be,
                          questions=_qs(1), probe=False)
    assert qa is not None and qa[0].answer == "the default"
    assert be.turns == [2], "exactly one cheap call, no probe session"
    assert "Do NOT use tools" in be.prompts[0]
    assert "assumption" in be.prompts[0]
    assert "have not read" in be.prompts[0]   # the no-invented-citations rule


@pytest.mark.asyncio
async def test_probe_false_failure_still_returns_questions_unanswered(tmp_path):
    """Never fabricate: a failed tool-less primary leaves the questions
    unanswered (plus the existing bounded retry), exactly like the probing
    path's failure mode — it does not invent answers."""
    be = _TurnsCapture("no block here at all")
    qa = await grill_spec("t", "d", ["c1"], tmp_path, backend=be,
                          questions=_qs(1), probe=False)
    assert qa is not None
    assert not qa[0].answer, "no answer was fabricated"
    assert len(be.turns) <= 2, "bounded: at most primary + one recovery"


@pytest.mark.asyncio
async def test_prose_only_task_grills_without_probes(tmp_path, monkeypatch):
    """The orchestrator's rung: a task whose named files are all non-executed
    prose — but which missed the trivial fast path on criteria count — grills
    with probe=False; a task naming code keeps the probing grill."""
    from no_human.config import load_config
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task
    from no_human.notify.slack import SlackNotifier
    import no_human.intake.evaluator as _ev

    calls = []

    async def fake_grill_spec(*a, **kw):
        calls.append(kw.get("probe"))
        return None

    monkeypatch.setattr(_ev, "grill_spec", fake_grill_spec)
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(_FakeStore(), cfg.data, object(),
                        SlackNotifier(None))

    prose = Task.new("fix the number in README.md line 7", repo_path="/r",
                     description="README.md says 100; docs/guide.md says 200; "
                                 "make them agree",
                     kind="feature")
    prose.acceptance_criteria = ["a", "b", "c", "d"]     # too many for trivial
    await orch._run_intake_grill(prose)

    code = Task.new("fix the limit in store.py", repo_path="/r",
                    description="store.py declares MAX_ITEMS", kind="bugfix")
    await orch._run_intake_grill(code)

    assert calls == [False, True], (
        "prose-only ⇒ probe-less grill; code-naming ⇒ probing grill")


class _FakeStore:
    async def update_task(self, task):
        return None
