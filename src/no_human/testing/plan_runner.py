"""Execute a TestPlan's layers in dependency order (PR4: cross-repo layered test execution).

Each layer is a ``TestLayer`` from ``test_layers.py``.  Layers run in
topological order (``TestPlan.ordered()``).  Blocking layers fail the task on
first failure.  Advisory layers record results but never fail the task.
Wake-gated layers are deferred (the orchestrator parks and wakes on green via
the existing ``ci_terminal_on:`` mechanism).

Cross-repo support: when a layer specifies ``repo`` different from the
task's working directory, tests run in that repo's path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .runner import TestRunResult, run_tests
from .test_layers import Gating, Runner, TestLayer, TestPlan

log = logging.getLogger("no_human.testing")


@dataclass
class LayerResult:
    """Outcome of running a single test layer."""
    layer_name: str
    gating: Gating
    result: TestRunResult | None = None   # None → skipped / deferred
    deferred: bool = False                # True for WAKE_GATED layers
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.deferred:
            return True  # not a failure; will be checked later
        if self.error:
            return False  # exception during execution
        if self.result is None:
            return True  # skipped
        return self.result.ok

    @property
    def summary(self) -> str:
        if self.deferred:
            return f"{self.layer_name}: deferred (wake-gated)"
        if self.error:
            return f"{self.layer_name}: ERROR — {self.error}"
        if self.result is None:
            return f"{self.layer_name}: skipped"
        status = "PASS" if self.result.ok else "FAIL"
        return f"{self.layer_name}: {status} — {self.result.summary}"


@dataclass
class PlanResult:
    """Aggregate outcome of running all layers in a test plan."""
    layer_results: list[LayerResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if no blocking layer failed."""
        return all(
            lr.ok for lr in self.layer_results
            if lr.gating == Gating.BLOCKING
        )

    @property
    def has_deferred(self) -> bool:
        return any(lr.deferred for lr in self.layer_results)

    @property
    def summary(self) -> str:
        parts = [lr.summary for lr in self.layer_results]
        return "; ".join(parts)


def run_test_plan(
    plan: TestPlan,
    task_repo: Path,
    *,
    on_layer_start: Callable[[TestLayer], None] | None = None,
    on_layer_done: Callable[[TestLayer, LayerResult], None] | None = None,
    fallback_cmd: str | None = None,
) -> PlanResult:
    """Run all layers in *plan* in dependency order.

    *task_repo* is the working directory for layers that don't specify their own
    ``repo``.  *fallback_cmd* is used for layers with empty ``command`` (from
    profile's test_cmd).

    Called from a thread (``asyncio.to_thread``), so all I/O is synchronous.
    """
    result = PlanResult()
    ordered = plan.ordered()

    if not ordered:
        return result

    for layer in ordered:
        if on_layer_start:
            on_layer_start(layer)

        # Wake-gated layers are deferred — the orchestrator parks and wakes.
        if layer.gating == Gating.WAKE_GATED:
            lr = LayerResult(
                layer_name=layer.name, gating=layer.gating, deferred=True,
            )
            result.layer_results.append(lr)
            if on_layer_done:
                on_layer_done(layer, lr)
            continue

        # CI layers are also deferred (the CIBackend handles them).
        if layer.runner == Runner.CI:
            lr = LayerResult(
                layer_name=layer.name, gating=layer.gating, deferred=True,
            )
            result.layer_results.append(lr)
            if on_layer_done:
                on_layer_done(layer, lr)
            continue

        # Resolve the working directory.
        cwd = Path(layer.repo) if layer.repo else task_repo
        cmd = layer.command or fallback_cmd

        try:
            test_result = run_tests(
                cwd, cmd, cwd=cwd, timeout=layer.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            lr = LayerResult(
                layer_name=layer.name, gating=layer.gating,
                error=str(exc),
            )
            result.layer_results.append(lr)
            if on_layer_done:
                on_layer_done(layer, lr)
            # A blocking layer error is a failure.
            if layer.gating == Gating.BLOCKING:
                break
            continue

        lr = LayerResult(
            layer_name=layer.name, gating=layer.gating, result=test_result,
        )
        result.layer_results.append(lr)
        if on_layer_done:
            on_layer_done(layer, lr)

        # Stop on first blocking failure.
        if layer.gating == Gating.BLOCKING and not test_result.ok:
            break

    return result
