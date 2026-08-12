"""The server and the CLI must build the coder orchestrator through the same
factory (audit A8/X2, 2026-08-11): `cli/commands.py` built via
`make_backend(..., role="coder")`, honouring `worker.backend`, while
`api/app.py` hardcoded `ClaudeBackend` in its own closure — a task run through
the server/GUI could not use the configured backend while the same task via
`nh` could. `core/runtime.py:build_orchestrator` is now the ONE construction
site both paths delegate to.

The lifespan harness below is copied (not imported) from
`tests/test_frozen_snapshot_guard.py::_boot_real_worker` / `_unwind_real_worker`
— that file's own docstring explains why: a local re-implementation of
`lifespan`'s wiring drifts from production silently, so the PROVEN pattern
boots the real `lifespan` on a throwaway `FastAPI()`, seaming only
`Scheduler.run_forever`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from no_human.agent.backend import resolve_backend_name
from no_human.agent.claude_backend import ClaudeBackend
from no_human.agent.codex_backend import CodexBackend
from no_human.core.db import Store
from no_human.core.runtime import build_orchestrator


class _WorkerBoot:
    app = None
    cm = None
    task = None
    started = None


async def _boot_real_worker(monkeypatch, tmp_path, tag, *, config_mutator=None):
    """Run the PRODUCTION `lifespan`, seaming ONLY `Scheduler.run_forever`.

    `config_mutator`, if given, is called with the freshly-loaded `Config`
    before `lifespan` observes it — this is how a test sets `worker.backend`
    without touching the on-disk file `lifespan` itself would load.
    """
    import importlib

    from fastapi import FastAPI

    app_mod = importlib.import_module("no_human.api.app")
    from no_human.config import load_config
    from no_human.core import scheduler as sched_mod

    cfg = load_config(tmp_path / f"{tag}-config.yaml")
    cfg.data["database"]["path"] = str(tmp_path / f"{tag}.db")
    if config_mutator is not None:
        config_mutator(cfg)
    monkeypatch.setattr(app_mod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS",
                       os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", ""))

    boot = _WorkerBoot()
    boot.started = asyncio.Event()

    async def _seam(self, *, stop, poll_interval=10.0):
        boot.task = asyncio.current_task()
        boot.started.set()
        await stop.wait()

    monkeypatch.setattr(sched_mod.Scheduler, "run_forever", _seam)
    boot.app = FastAPI()
    boot.cm = app_mod.lifespan(boot.app)
    await boot.cm.__aenter__()
    await asyncio.wait_for(boot.started.wait(), timeout=5)
    return boot


async def _unwind_real_worker(boot):
    with contextlib.suppress(BaseException):
        await boot.cm.__aexit__(None, None, None)
    store = getattr(boot.app.state, "store", None)
    if store is not None:
        with contextlib.suppress(Exception):
            await store.close()


async def test_the_server_worker_builds_the_configured_coding_backend(
        tmp_path, monkeypatch):
    """RED-FIRST: the server used to hardcode ClaudeBackend in `_orch_factory`,
    so `worker.backend: codex` was honoured by `nh` but silently ignored by
    the server/GUI path — a live divergence (audit A8/X2)."""

    def _use_codex(cfg):
        cfg.data.setdefault("worker", {})["backend"] = "codex"

    boot = await _boot_real_worker(monkeypatch, tmp_path, "codex",
                                   config_mutator=_use_codex)
    try:
        cfg = boot.app.state.config
        # Instrument check first: the test cannot pass vacuously if the
        # config key is silently ignored upstream of the factory.
        assert resolve_backend_name(cfg.data) == "codex"

        orch = boot.app.state.scheduler.factory()
        assert isinstance(orch.backend, CodexBackend), (
            "the server built the wrong backend class for the configured "
            "`worker.backend` — the GUI path diverged from the CLI path")
    finally:
        await _unwind_real_worker(boot)


@pytest.mark.real_backend
async def test_the_server_worker_still_defaults_to_the_claude_backend(
        tmp_path, monkeypatch):
    """Default-unchanged guard: an operator who sets nothing must still get
    exactly the incumbent ClaudeBackend, with the same construction args the
    server always passed.

    Marked `real_backend` (as `test_codex_backend.py` does for the same
    assertion shape) so the autouse `_hermetic_sdk` fixture in conftest.py
    does not swap `ClaudeBackend` for `_HermeticUtilityBackend` — construction
    only, `.run()` is never called, so nothing here reaches the real API."""
    boot = await _boot_real_worker(monkeypatch, tmp_path, "default")
    try:
        cfg = boot.app.state.config
        orch = boot.app.state.scheduler.factory()
        assert isinstance(orch.backend, ClaudeBackend)
        assert orch.backend.model == cfg.primary_model
        assert orch.reviewer is not None
        assert orch.backend.forbidden_paths == cfg["safety"]["forbidden_paths"]
    finally:
        await _unwind_real_worker(boot)


@pytest.mark.real_backend
async def test_cli_and_server_build_the_same_orchestrator_shape(
        tmp_path, monkeypatch):
    """Parity: both entry points delegate to the same `build_orchestrator`,
    so they must produce the same shape of orchestrator over the same
    config — same backend class, and every optional collaborator wired.

    Marked `real_backend` for the same reason as the default-guard test
    above: both sides build a real (uncalled) `ClaudeBackend`."""
    boot = await _boot_real_worker(monkeypatch, tmp_path, "parity")
    try:
        cfg = boot.app.state.config
        server_orch = boot.app.state.scheduler.factory()

        store = await Store(cfg.db_path).connect()
        try:
            cli_orch = build_orchestrator(cfg, store)
        finally:
            await store.close()

        assert type(cli_orch.backend) is type(server_orch.backend)
        for orch in (cli_orch, server_orch):
            assert orch.reviewer is not None
            assert orch.context_gatherer is not None
            assert orch.learning_queue is not None
    finally:
        await _unwind_real_worker(boot)
