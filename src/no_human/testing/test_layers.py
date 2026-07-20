"""Test-layer model for cross-repo layered test execution (Phase 6a).

A test layer describes one tier of testing (unit → integration → E2E) that may
live in a different repo/branch than the code being changed. Layers are ordered
by ``depends_on`` and have explicit gating (blocking vs advisory).

The topology lives on the **Project** (not a single repo profile) because it
spans repos — e.g. analytics-export code in the analytics-export repo,
integration tests in a separate analytics-export-tests repo, E2E in a
GitLab pipeline.

Each per-repo ProfileProject still holds the fast local unit ``test_cmd``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Runner(str, Enum):
    """Where the tests execute."""
    LOCAL = "local"   # subprocess in the orchestrator's env
    CI = "ci"         # triggered via the CIBackend


class Gating(str, Enum):
    """How the layer gates the task."""
    BLOCKING = "blocking"     # must pass before review/PR
    ADVISORY = "advisory"     # results attached to PR, human decides
    WAKE_GATED = "wake_gated" # parks task, wakes on green (existing mechanism)


@dataclass
class TestLayer:
    """A single testing tier in a project's test plan."""

    name: str                          # e.g. "unit", "integration", "e2e"
    command: str                       # test command (e.g. "uv run pytest -q")
    repo: str | None = None            # repo path; None → use task's repo_path
    branch: str | None = None          # fixed scratch branch, "match_pr", or None
    runner: Runner = Runner.LOCAL
    gating: Gating = Gating.BLOCKING
    timeout: int = 300                 # seconds
    human_gated: bool = False          # requires human approval before running
    wake_hint: str | None = None       # wake condition for WAKE_GATED layers
    depends_on: list[str] = field(default_factory=list)  # layer names to run first
    ci: dict[str, Any] = field(default_factory=dict)     # CI config (reuses existing)
    env: dict[str, str] = field(default_factory=dict)    # env-var name→value to inject
    secret_ref: list[str] = field(default_factory=list)  # required env var names (must exist)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "repo": self.repo,
            "branch": self.branch,
            "runner": self.runner.value,
            "gating": self.gating.value,
            "timeout": self.timeout,
            "human_gated": self.human_gated,
            "wake_hint": self.wake_hint,
            "depends_on": self.depends_on,
            "ci": self.ci,
            "env": self.env,
            "secret_ref": self.secret_ref,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TestLayer":
        return cls(
            name=d["name"],
            command=d.get("command", ""),
            repo=d.get("repo"),
            branch=d.get("branch"),
            runner=Runner(d.get("runner", "local")),
            gating=Gating(d.get("gating", "blocking")),
            timeout=int(d.get("timeout", 300)),
            human_gated=bool(d.get("human_gated", False)),
            wake_hint=d.get("wake_hint"),
            depends_on=d.get("depends_on", []),
            ci=d.get("ci", {}),
            env=d.get("env", {}),
            secret_ref=d.get("secret_ref", []),
        )


@dataclass
class TestPlan:
    """Project-level ordered list of test layers."""

    layers: list[TestLayer] = field(default_factory=list)

    def ordered(self) -> list[TestLayer]:
        """Return layers in dependency order (topological sort).

        Layers with no dependencies come first. A cycle falls back to the
        original insertion order (best-effort, never crash).
        """
        by_name = {layer.name: layer for layer in self.layers}
        visited: set[str] = set()
        result: list[TestLayer] = []

        def _visit(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            layer = by_name.get(name)
            if not layer:
                return
            for dep in layer.depends_on:
                _visit(dep)
            result.append(layer)

        for layer in self.layers:
            _visit(layer.name)
        return result

    def blocking_layers(self) -> list[TestLayer]:
        """Layers that must pass before review (gating=blocking)."""
        return [l for l in self.ordered() if l.gating == Gating.BLOCKING]

    def advisory_layers(self) -> list[TestLayer]:
        """Layers whose results are attached to PR but don't block."""
        return [l for l in self.ordered() if l.gating == Gating.ADVISORY]

    def to_json(self) -> str:
        return json.dumps([l.to_dict() for l in self.layers])

    @classmethod
    def from_json(cls, raw: str) -> "TestPlan":
        data = json.loads(raw) if raw else []
        return cls(layers=[TestLayer.from_dict(d) for d in data])

    @classmethod
    def from_profile(cls, profile) -> "TestPlan":
        """Bootstrap a test plan from a single-repo ProjectProfile.

        Creates a unit layer from ``test_cmd`` and optionally an integration
        layer from ``integration_test_cmd``.
        """
        layers: list[TestLayer] = []
        if getattr(profile, "test_cmd", None):
            layers.append(TestLayer(
                name="unit",
                command=profile.test_cmd,
                repo=getattr(profile, "repo_path", None),
                runner=Runner.LOCAL,
                gating=Gating.BLOCKING,
                timeout=120,
            ))
        if getattr(profile, "integration_test_cmd", None):
            layers.append(TestLayer(
                name="integration",
                command=profile.integration_test_cmd,
                repo=getattr(profile, "repo_path", None),
                runner=Runner.LOCAL,
                gating=Gating.WAKE_GATED,
                timeout=600,
                depends_on=["unit"],
            ))
        return cls(layers=layers)
