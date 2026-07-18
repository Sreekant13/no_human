"""§6 directive: EVERY task passes the intake grill before planning —
orchestrator wiring. Mirrors test_eval_acts.py's harness."""

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.intake import evaluator as ev
from no_human.intake.evaluator import GrillQA
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path, events, cfg_overlay=None):
    cfg = load_config(tmp_path / "config.yaml")
    if cfg_overlay:
        cfg.data.update(cfg_overlay)
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)


def _qa():
    return [
        GrillQA(question="Which file?", decision_it_changes="target",
                answer="src/x.py:1", source="repo-evidence"),
        GrillQA(question="Rotate the credential?", decision_it_changes="auth",
                answer="HUMAN-GATED: not self-answerable", carve_out="access"),
    ]


async def test_grill_runs_for_every_task_and_persists_qa(
        store, tmp_path, monkeypatch):
    seen = {}

    async def _fake_grill(title, description, criteria, repo_path, *,
                          backend=None, model=None, questions=None):
        seen["repo_path"] = repo_path
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/repo/x")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)

    got = await store.get_task(t.id)
    qa = got.context["intake_qa"]
    assert len(qa) == 2 and qa[0]["answer"] == "src/x.py:1"
    assert seen["repo_path"] == "/repo/x"
    kinds = [e.get("kind") for e in events]
    assert "intake_grill" in kinds
    grill_ev = events[kinds.index("intake_grill")]
    assert "1 human-gated" in grill_ev["text"]


async def test_grill_skipped_after_interactive_grill(store, tmp_path, monkeypatch):
    called = []

    async def _fake_grill(*a, **k):  # pragma: no cover
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    t.context = {"grill_complete": True}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])
    await orch._run_intake_grill(t)
    assert called == []


async def test_grill_config_gate_off(store, tmp_path, monkeypatch):
    called = []

    async def _fake_grill(*a, **k):  # pragma: no cover
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path, [], cfg_overlay={"intake": {"grill": False}})
    await orch._run_intake_grill(t)
    assert called == []


async def test_grill_failure_never_breaks_the_task(store, tmp_path, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("grill down")
    monkeypatch.setattr(ev, "grill_spec", _boom)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)  # must not raise
    got = await store.get_task(t.id)
    assert "intake_qa" not in (got.context or {})
    assert any(e.get("kind") == "advisory" for e in events)


async def test_retry_keeps_prior_qa_and_never_regrills(store, tmp_path, monkeypatch):
    """`nh task retry` resets to PENDING and re-walks the spine — the prior
    Q&A stands; the two grill sessions are never re-spent (review r1 f2)."""
    called = []

    async def _fake_grill(*a, **k):  # pragma: no cover
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    t.context = {"intake_qa": [{"question": "old?", "decision_it_changes": "",
                                "answer": "kept", "source": "human",
                                "carve_out": "none"}]}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])
    await orch._run_intake_grill(t)
    assert called == []
    got = await store.get_task(t.id)
    assert got.context["intake_qa"][0]["answer"] == "kept"


async def test_malformed_none_intake_section_degrades_advisory(
        store, tmp_path, monkeypatch):
    """`intake:` as an empty YAML section resolves to None — the gate must
    degrade advisory, never AttributeError the task (review r1 f4)."""
    called = []

    async def _fake_grill(*a, **k):
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events, cfg_overlay={"intake": None})
    await orch._run_intake_grill(t)  # must not raise
    # None-section means "no override" → grill stays on (default True).
    assert called == [1]


async def test_all_empty_answers_emit_an_advisory(store, tmp_path, monkeypatch):
    """v10 drill: the answering failure was silent (log-only) — it must be an
    advisory event so doctor/task_events see it (the anti-silent-death rule)."""
    from no_human.intake.evaluator import GrillQA

    async def _fake_grill(*a, **k):
        return [GrillQA(question="q?", decision_it_changes="d")]  # unanswered
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)
    adv = [e for e in events if e.get("kind") == "advisory"]
    assert any("unanswered" in e.get("text", "") for e in adv)
    # The QA is still persisted (unanswered beats absent).
    got = await store.get_task(t.id)
    assert got.context["intake_qa"][0]["question"] == "q?"
