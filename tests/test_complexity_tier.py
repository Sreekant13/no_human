"""C1.5: one explicit complexity tier per task, escalate-only, driving the
MoA fan-out, extended thinking, and the turn budget coherently — and visible
in task.context + /api/metrics so per-tier cost/quality is measured, not
assumed."""

from no_human.core.complexity import compute_tier, is_complex, store_tier
from no_human.core.task import Task, TaskStatus


def _task(**kw):
    defaults = dict(id="aaa", source="test", title="t",
                    status=TaskStatus.PENDING, acceptance_criteria=[])
    defaults.update(kw)
    return Task(**defaults)


# ---------------------------------------------------------------- compute --

def test_tiny_task_is_trivial():
    t = _task(title="Add a camelCase helper", description="small helper",
              acceptance_criteria=["works", "tested"])
    tier, signals = compute_tier(t)
    assert tier == "trivial" and signals == []


def test_one_signal_is_standard():
    t = _task(description="x" * 2500)
    tier, signals = compute_tier(t)
    assert tier == "standard" and signals == ["long-spec"]


def test_two_signals_is_complex():
    t = _task(description="x" * 2500)
    t.linked_repos = ["/other"]
    tier, signals = compute_tier(t)
    assert tier == "complex" and set(signals) == {"multi-repo", "long-spec"}


def test_decompose_verdict_alone_is_complex():
    t = _task()
    t.context = {"eval_result": {"verdict": "decompose"}}
    tier, _ = compute_tier(t)
    assert tier == "complex"


def test_spec_files_and_plan_size_are_signals():
    """The thinking gate's post-spec signals join the same tier — previously
    a task could get 3 Opus proposers but no thinking, or vice versa."""
    t = _task()
    t.context = {"spec": {"files_to_change": ["a", "b", "c", "d", "e"]},
                 "plan_size_warning": True}
    tier, signals = compute_tier(t)
    assert tier == "complex" and set(signals) == {"many-files", "large-plan"}


def test_enriched_criteria_do_not_raise_the_tier():
    t = _task(title="Add a helper", description="small")
    t.acceptance_criteria = [f"enriched {i}" for i in range(8)]
    t.context = {"original_criteria": ["works", "tested"]}
    tier, _ = compute_tier(t)
    assert tier == "trivial"


# ------------------------------------------------------------ store/upgrade --

def test_store_tier_is_escalate_only():
    t = _task()
    assert store_tier(t, "standard", ["long-spec"]) is True
    assert t.context["complexity_tier"] == "standard"
    # never downgrades
    assert store_tier(t, "trivial", []) is False
    assert t.context["complexity_tier"] == "standard"
    # upgrades
    assert store_tier(t, "complex", ["long-spec", "many-files"]) is True
    assert t.context["complexity_tier"] == "complex"
    assert is_complex(t)


# ---------------------------------------------------------------- metrics --

async def test_metrics_report_per_tier_cost_and_quality(tmp_path):
    from no_human.core.db import Store
    from no_human.core.metrics import compute_metrics

    store = await Store(tmp_path / "m.db").connect()
    try:
        t1 = Task.new("trivial one", repo_path="/r")
        t1.context = {"complexity_tier": "trivial"}
        await store.create_task(t1)
        t2 = Task.new("complex one", repo_path="/r")
        t2.context = {"complexity_tier": "complex"}
        await store.create_task(t2)

        a1 = await store.create_attempt(t1.id, 1)
        await store.update_attempt(a1, cache_read_tokens=500_000,
                                   tokens_used=4_000, status="succeeded")
        a2 = await store.create_attempt(t2.id, 1)
        await store.update_attempt(a2, cache_read_tokens=8_000_000,
                                   tokens_used=40_000, status="failed")

        m = await compute_metrics(store)
        by_tier = {row["tier"]: row for row in m["by_tier"]}
        assert by_tier["trivial"]["attempts"] == 1
        assert by_tier["trivial"]["cache_read"] == 500_000
        assert by_tier["complex"]["cache_read"] == 8_000_000
        assert by_tier["complex"]["succeeded"] == 0
        assert by_tier["trivial"]["succeeded"] == 1
    finally:
        await store.close()


def test_raised_min_signals_raises_the_complex_bar():
    """An operator raising min_signals above 2 (a documented cost knob) must
    not be silently overridden by the tier's hardcoded bar."""
    t = _task(description="x" * 2500)
    t.linked_repos = ["/other"]  # 2 signals
    tier, _ = compute_tier(t, {"min_signals": 3})
    assert tier == "standard"
    # 0/1 remain gate-level fan-out overrides, not tier definitions
    tier0, _ = compute_tier(t, {"min_signals": 0})
    assert tier0 == "complex"
