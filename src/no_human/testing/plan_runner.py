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
import os
import subprocess
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
        if self.error:
            return False  # exception or missing credentials
        if self.deferred:
            return True  # not a failure; will be checked later
        if self.result is None:
            return True  # skipped
        return self.result.ok

    @property
    def summary(self) -> str:
        if self.error:
            return f"{self.layer_name}: ERROR — {self.error}"
        if self.deferred:
            return f"{self.layer_name}: deferred (wake-gated)"
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

        # --- PR-C: credential resolution --------------------------------- #
        resolved_env, missing = _resolve_credentials(layer)
        if missing:
            lr = LayerResult(
                layer_name=layer.name, gating=layer.gating,
                error=f"MISSING_ACCESS: required env vars not set: {', '.join(missing)}",
            )
            lr.deferred = True  # human must supply creds first
            result.layer_results.append(lr)
            if on_layer_done:
                on_layer_done(layer, lr)
            log.warning(
                "layer %s skipped — missing credentials: %s (never log values)",
                layer.name, ", ".join(missing),
            )
            if layer.gating == Gating.BLOCKING:
                break
            continue

        # --- PR-C: branch freshness check -------------------------------- #
        if layer.repo and layer.branch:
            _check_branch_freshness(cwd, layer, task_repo)

        try:
            test_result = run_tests(
                cwd, cmd, cwd=cwd, timeout=layer.timeout,
                env=resolved_env or None,
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


# --------------------------------------------------------------------------- #
# PR-C helpers: credential resolution + branch freshness                       #
# --------------------------------------------------------------------------- #


def _resolve_credentials(
    layer: TestLayer,
) -> tuple[dict[str, str], list[str]]:
    """Resolve ``env`` and ``secret_ref`` for a layer.

    Returns (merged_env, missing_names). *merged_env* contains the layer's
    ``env`` dict plus any ``secret_ref`` vars read from ``os.environ``.
    *missing_names* lists ``secret_ref`` entries that are absent from the
    environment — the caller must NOT run the layer when this is non-empty.

    Values are never logged.
    """
    merged: dict[str, str] = dict(layer.env)  # static overrides
    missing: list[str] = []
    for name in layer.secret_ref:
        val = os.environ.get(name)
        if val is None:
            missing.append(name)
        else:
            merged[name] = val
    return merged, missing


def _check_branch_freshness(
    layer_cwd: Path, layer: TestLayer, task_repo: Path,
) -> None:
    """Warn if the integration-test repo's branch is behind the code repo.

    Compares the HEAD SHA of *task_repo* with the merge-base of
    *layer.branch* in *layer_cwd*. If the branch doesn't contain the code
    repo's HEAD, the test results may be stale.

    This is advisory only — it logs a warning but never blocks execution.
    """
    if not (layer_cwd / ".git").is_dir():
        return
    try:
        code_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=task_repo, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not code_head:
            return
        # Check if the code HEAD is an ancestor of the layer branch.
        rc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", code_head, layer.branch or "HEAD"],
            cwd=layer_cwd, capture_output=True, timeout=10,
        ).returncode
        if rc != 0:
            log.warning(
                "layer %s: branch %r in %s may be stale — "
                "code HEAD %s is not an ancestor; consider refreshing",
                layer.name, layer.branch, layer_cwd, code_head[:8],
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("branch freshness check skipped for %s: %s", layer.name, exc)
