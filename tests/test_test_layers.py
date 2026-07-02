"""Tests for the test-layer model (Phase 6a)."""

import json
from pathlib import Path

import pytest

from no_human.testing.test_layers import (
    Gating,
    Runner,
    TestLayer,
    TestPlan,
)
from no_human.project_model import Project


def test_layer_roundtrip():
    layer = TestLayer(
        name="unit", command="pytest -q", repo="/code",
        runner=Runner.LOCAL, gating=Gating.BLOCKING, timeout=120,
    )
    d = layer.to_dict()
    restored = TestLayer.from_dict(d)
    assert restored.name == "unit"
    assert restored.runner == Runner.LOCAL
    assert restored.gating == Gating.BLOCKING
    assert restored.timeout == 120


def test_plan_ordering():
    plan = TestPlan(layers=[
        TestLayer(name="e2e", command="make e2e", depends_on=["integration"]),
        TestLayer(name="unit", command="pytest"),
        TestLayer(name="integration", command="mvn verify", depends_on=["unit"]),
    ])
    ordered = plan.ordered()
    names = [l.name for l in ordered]
    assert names == ["unit", "integration", "e2e"]


def test_plan_blocking_and_advisory():
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest", gating=Gating.BLOCKING),
        TestLayer(name="e2e", command="make e2e", gating=Gating.ADVISORY),
    ])
    assert len(plan.blocking_layers()) == 1
    assert plan.blocking_layers()[0].name == "unit"
    assert len(plan.advisory_layers()) == 1
    assert plan.advisory_layers()[0].name == "e2e"


def test_plan_json_roundtrip():
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest"),
        TestLayer(name="integration", command="mvn verify", depends_on=["unit"]),
    ])
    raw = plan.to_json()
    restored = TestPlan.from_json(raw)
    assert len(restored.layers) == 2
    assert restored.layers[1].depends_on == ["unit"]


def test_plan_empty():
    plan = TestPlan.from_json("[]")
    assert plan.layers == []
    assert plan.ordered() == []


def test_from_profile():
    class FakeProfile:
        test_cmd = "uv run pytest"
        integration_test_cmd = "mvn verify"
        repo_path = "/code"

    plan = TestPlan.from_profile(FakeProfile())
    assert len(plan.layers) == 2
    assert plan.layers[0].name == "unit"
    assert plan.layers[1].name == "integration"
    assert plan.layers[1].depends_on == ["unit"]


def test_project_test_plan():
    layers = [
        TestLayer(name="unit", command="pytest").to_dict(),
        TestLayer(name="e2e", command="make e2e", depends_on=["unit"]).to_dict(),
    ]
    p = Project.new("test-project")
    p.test_layers = json.dumps(layers)
    plan = p.test_plan
    assert len(plan.layers) == 2
    ordered = plan.ordered()
    assert ordered[0].name == "unit"
    assert ordered[1].name == "e2e"


# --------------------------------------------------------------------------- #
# PR4: plan_runner tests                                                       #
# --------------------------------------------------------------------------- #

from no_human.testing.plan_runner import LayerResult, PlanResult, run_test_plan


def test_plan_runner_blocking_pass(tmp_path):
    """A plan with a passing blocking layer succeeds."""
    # Create a trivial test script.
    (tmp_path / "test.sh").write_text("#!/bin/sh\necho '1 passed'\nexit 0\n")
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh test.sh", gating=Gating.BLOCKING),
    ])
    result = run_test_plan(plan, tmp_path)
    assert result.ok
    assert len(result.layer_results) == 1
    assert result.layer_results[0].result.ok


def test_plan_runner_blocking_fail_stops(tmp_path):
    """A failing blocking layer stops execution; subsequent layers are skipped."""
    (tmp_path / "fail.sh").write_text("#!/bin/sh\necho '1 failed'\nexit 1\n")
    (tmp_path / "pass.sh").write_text("#!/bin/sh\necho '1 passed'\nexit 0\n")
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh fail.sh", gating=Gating.BLOCKING),
        TestLayer(name="e2e", command="sh pass.sh", gating=Gating.BLOCKING, depends_on=["unit"]),
    ])
    result = run_test_plan(plan, tmp_path)
    assert not result.ok
    assert len(result.layer_results) == 1  # e2e was skipped


def test_plan_runner_advisory_fail_does_not_fail_plan(tmp_path):
    """Advisory layer failure doesn't fail the overall plan."""
    (tmp_path / "pass.sh").write_text("#!/bin/sh\necho '1 passed'\nexit 0\n")
    (tmp_path / "fail.sh").write_text("#!/bin/sh\necho '1 failed'\nexit 1\n")
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh pass.sh", gating=Gating.BLOCKING),
        TestLayer(name="lint", command="sh fail.sh", gating=Gating.ADVISORY),
    ])
    result = run_test_plan(plan, tmp_path)
    assert result.ok  # advisory failure doesn't block
    assert len(result.layer_results) == 2
    assert not result.layer_results[1].result.ok  # lint did fail


def test_plan_runner_wake_gated_deferred(tmp_path):
    """Wake-gated layers are deferred, not executed."""
    plan = TestPlan(layers=[
        TestLayer(name="ci", command="unused", gating=Gating.WAKE_GATED),
    ])
    result = run_test_plan(plan, tmp_path)
    assert result.ok
    assert result.has_deferred
    assert result.layer_results[0].deferred


def test_plan_runner_cross_repo(tmp_path):
    """A layer with a different repo runs in that repo's directory."""
    code_repo = tmp_path / "code"
    test_repo = tmp_path / "tests"
    code_repo.mkdir()
    test_repo.mkdir()
    (test_repo / "run.sh").write_text("#!/bin/sh\necho '5 passed'\nexit 0\n")
    plan = TestPlan(layers=[
        TestLayer(
            name="integration",
            command="sh run.sh",
            repo=str(test_repo),
            gating=Gating.BLOCKING,
        ),
    ])
    result = run_test_plan(plan, code_repo)
    assert result.ok
    assert result.layer_results[0].result.passed == 5


def test_plan_runner_callbacks(tmp_path):
    """on_layer_start and on_layer_done callbacks fire."""
    (tmp_path / "ok.sh").write_text("#!/bin/sh\nexit 0\n")
    starts, dones = [], []
    plan = TestPlan(layers=[
        TestLayer(name="unit", command="sh ok.sh", gating=Gating.BLOCKING),
    ])
    run_test_plan(
        plan, tmp_path,
        on_layer_start=lambda l: starts.append(l.name),
        on_layer_done=lambda l, lr: dones.append(lr.summary),
    )
    assert starts == ["unit"]
    assert len(dones) == 1


@pytest.mark.asyncio
async def test_find_project_by_repo(tmp_path):
    """Store.find_project_by_repo finds a project by its repo_paths."""
    from no_human.core.db import Store
    store = await Store(tmp_path / "test.db").connect()
    p = Project.new("my-project", repo_paths=["/home/user/repo1", "/home/user/repo2"])
    await store.create_project(p)

    found = await store.find_project_by_repo("/home/user/repo1")
    assert found is not None
    assert found.name == "my-project"

    not_found = await store.find_project_by_repo("/home/user/other")
    assert not_found is None
    await store.close()
