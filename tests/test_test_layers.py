"""Tests for the test-layer model (Phase 6a)."""

import json

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
