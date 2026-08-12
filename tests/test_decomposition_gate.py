"""A2 (2026-08-11): the decomposition feature gate (config
``decomposition.enabled``, default False) was checked at ``_drive`` but
``_generate_plan`` called ``_apply_decomposition`` UNCONDITIONALLY — a
planner that spontaneously emitted the DECOMPOSE_PLAN marker got its verdict
persisted to ``task.context``, a "compound task detected" event emitted, and
the raw marker JSON returned as the implementation plan (exempted from the
usual "no plan sections -> unusable" check) even though nothing downstream
ever reads that context key with the gate off. This silently degraded the
task to a plan-less run while the board claimed compound detection.

These tests pin: gate off treats a marker-bearing plan as an ordinary plan
(unusable if it has no ``##`` sections), writes nothing to context, and never
fires the compound event; gate on is byte-identical to prior behaviour.
"""

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

from tests.test_e2e_orchestrator import (
    FakeBackend,
    MoAFakeBackend,
    PromptCapturingPlannerBackend,
    _SAMPLE_PLAN,
    _config,
    _moa_config,
    _planning_config,
    bare_repo,  # noqa: F401 -- fixture
    store,  # noqa: F401 -- fixture
)

from unittest.mock import patch as _patch

# Same marker blob as tests/test_e2e_orchestrator.py:2953-2959 — a bare
# decomposition verdict with no ordinary ``##`` plan sections.
DECOMPOSE_TEXT = (
    "DECOMPOSE_PLAN_START\n```json\n"
    '{"decompose": true, "justification": "multi-concern", "subtasks": '
    '[{"title": "A", "kind": "feature", "description": "d", '
    '"acceptance_criteria": ["x"], "depends_on": [], "repo_path": "."}]}\n'
    "```\nDECOMPOSE_PLAN_END\n"
)


async def test_gate_off_marker_plan_does_not_persist_context(bare_repo, tmp_path, store):
    """decomposition.enabled absent (default off): a marker-only plan must
    not write task.context['decomposition'], in memory or in the store."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("compound task", repo_path=str(bare_repo))
    await store.create_task(t)
    backend = PromptCapturingPlannerBackend(DECOMPOSE_TEXT)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert "decomposition" not in (t.context or {})
    reread = await store.get_task(t.id)
    assert "decomposition" not in (reread.context or {})


async def test_gate_off_emits_no_compound_event(bare_repo, tmp_path, store):
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("compound task", repo_path=str(bare_repo))
    await store.create_task(t)
    backend = PromptCapturingPlannerBackend(DECOMPOSE_TEXT)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert not any("compound task detected" in e.get("text", "") for e in events)
    assert not any(e.get("kind") == "state" and e.get("status") == "compound_parent"
                   for e in events)


async def test_gate_off_marker_only_plan_is_unusable(bare_repo, tmp_path, store):
    """A bare DECOMPOSE_PLAN block has no ``##`` sections, so with the gate
    off it must be judged exactly like any other section-less plan: unusable,
    not returned as the implementation plan."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("compound task", repo_path=str(bare_repo))
    await store.create_task(t)
    backend = PromptCapturingPlannerBackend(DECOMPOSE_TEXT)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("unusable" in e.get("text", "") for e in planning_events)


def test_plan_is_unusable_exemption_follows_the_gate(tmp_path):
    cfg = _planning_config(tmp_path)
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = cfg.data

    assert orch._plan_is_unusable(DECOMPOSE_TEXT) is True
    assert orch._plan_is_unusable("") is True

    cfg.data["decomposition"] = {"enabled": True}
    assert orch._plan_is_unusable(DECOMPOSE_TEXT) is False
    assert orch._plan_is_unusable("") is True


async def test_gate_off_marker_with_sections_is_used_as_a_plan(bare_repo, tmp_path, store):
    """A plan that carries BOTH the marker and real ## sections is still an
    ordinary, usable plan with the gate off — pins "ordinary plan", not
    "always unavailable"."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("compound task", repo_path=str(bare_repo))
    await store.create_task(t)
    plan_text = DECOMPOSE_TEXT + "\n" + _SAMPLE_PLAN
    backend = PromptCapturingPlannerBackend(plan_text)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == plan_text.strip()
    assert "decomposition" not in (t.context or {})
    assert not any("compound task detected" in e.get("text", "") for e in events)


async def test_gate_on_behaviour_unchanged(bare_repo, tmp_path, store):
    cfg = _planning_config(tmp_path)
    cfg.data["decomposition"] = {"enabled": True}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("compound task", repo_path=str(bare_repo))
    await store.create_task(t)
    backend = PromptCapturingPlannerBackend(DECOMPOSE_TEXT)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == DECOMPOSE_TEXT.strip()
    assert t.context["decomposition"]["decompose"] is True
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("compound task detected" in e.get("text", "") for e in planning_events)


async def test_moa_gate_off_does_not_short_circuit(bare_repo, tmp_path, store):
    """With the gate off, a proposer that emits the decomposition marker must
    not short-circuit MoA: the aggregator still runs (4 prompts total: 3
    proposers + 1 aggregator), and no ctx write / compound event fires. Guards
    the AttributeError the gate would otherwise introduce when
    ``_apply_decomposition`` returns None but the caller still calls
    ``decomp.get(...)``."""
    cfg = _moa_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("compound task", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={
            "minimal-first": _SAMPLE_PLAN,
            "risk-first": DECOMPOSE_TEXT,
            "test-first": _SAMPLE_PLAN,
        },
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result
    assert len(fake.prompts) == 4  # 3 proposers + aggregator, not a 3-prompt short-circuit
    assert "decomposition" not in (t.context or {})
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert not any("compound task detected" in e.get("text", "") for e in planning_events)


def test_decomposition_default_is_off(tmp_path):
    cfg = _config(tmp_path)
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = cfg.data
    assert orch._decompose_children_enabled() is False
