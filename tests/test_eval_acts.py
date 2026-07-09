"""P2: the orchestrator ACTS on the intake evaluator verdict (enrich/clarify)
instead of only annotating it — so no human is needed mid-flight."""

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.intake import evaluator as ev
from no_human.intake.evaluator import EvalResult, EvalVerdict
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


async def test_enrich_adopts_criteria_and_preserves_original(store, tmp_path):
    t = Task.new("do a thing", repo_path="/r")
    t.acceptance_criteria = ["original one"]
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(
        verdict=EvalVerdict.ENRICH,
        enriched_criteria=["sharper A", "sharper B"],
    )
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == ["sharper A", "sharper B"]
    assert got.context["original_criteria"] == ["original one"]


async def test_clarify_records_assumptions(store, tmp_path, monkeypatch):
    async def _fake_resolve(title, description, criteria, *, backend=None, model=None):
        return ["assume the header is X-Instance-Id"]
    monkeypatch.setattr(ev, "resolve_assumptions", _fake_resolve)

    t = Task.new("ambiguous task", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(verdict=EvalVerdict.CLARIFY)
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.context["assumptions"] == ["assume the header is X-Instance-Id"]


async def test_accept_verdict_is_noop(store, tmp_path):
    t = Task.new("clear task", repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.ACCEPT))

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == ["keep me"]
    assert "assumptions" not in (got.context or {})
    assert "original_criteria" not in (got.context or {})
