"""Public issue #5: `--backend` on a task is a real per-task coder switch.

Before: `nh task add --backend` was `click.Choice(["claude"])`, the value was
only displayed on the board, and the coder always ran on `worker.backend`.
After: the value routes THAT task's coder through `make_backend`'s override
seam; every other role stays pinned to Claude; unknown names are refused at
intake (CLI choice, HTTP 422) instead of surfacing as BackendUnavailable on
the first attempt.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from no_human.agent.backend import SUPPORTED_BACKENDS, make_backend
from no_human.agent.codex_backend import CodexBackend
from no_human.cli.commands import cli, task_add
from no_human.core.runtime import build_orchestrator, task_backend_override


class _Task:
    def __init__(self, config):
        self.config = config


def test_the_override_reads_the_task_and_normalises_it():
    assert task_backend_override(None) is None
    assert task_backend_override(_Task({})) is None
    assert task_backend_override(_Task({"backend": ""})) is None
    assert task_backend_override(_Task({"backend": " Codex "})) == "codex"
    assert task_backend_override(object()) is None  # no config attribute at all


def test_the_cli_choice_is_the_factory_tuple_not_a_hardcoded_list():
    (opt,) = [p for p in task_add.params if p.name == "backend"]
    assert tuple(opt.type.choices) == SUPPORTED_BACKENDS
    assert "codex" in SUPPORTED_BACKENDS and "claude" in SUPPORTED_BACKENDS
    out = CliRunner().invoke(cli, ["task", "add", "--help"]).output
    assert "--backend" in out and "THIS task's coder" in out


def test_make_backend_refuses_an_unknown_name_and_names_the_flag():
    from no_human.agent.backend import BackendUnavailable
    with pytest.raises(BackendUnavailable) as e:
        make_backend(model="m", backend="bogus")
    assert "'codex'" in str(e.value) and "--backend" in str(e.value)


async def test_the_factory_routes_this_tasks_coder_to_the_task_backend(tmp_path, monkeypatch):
    """RED at the main tip: build_orchestrator ignored task.config['backend']
    and built the global `worker.backend` (Claude) for a task that asked for
    codex."""
    from no_human.config import load_config
    from no_human.core.db import Store
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr("no_human.agent.codex_backend.find_codex_cli", lambda explicit=None: "/stub/codex")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.data.get("worker", {}).get("backend", "claude") == "claude"
    store = await Store(tmp_path / "t.db").connect()
    try:
        orch = build_orchestrator(cfg, store, task=_Task({"backend": "codex"}))
        assert isinstance(orch.backend, CodexBackend)
        # The reviewer is not the coder: it stays on Claude whatever the task
        # asked for (CLAUDE_PINNED_ROLES) — the review must not come from the
        # model that wrote the code.
        assert type(orch.reviewer._backend).__name__ != "CodexBackend"
    finally:
        await store.close()


@pytest.mark.real_backend
async def test_a_task_with_no_opinion_still_follows_worker_backend(tmp_path):
    from no_human.agent.claude_backend import ClaudeBackend
    from no_human.config import load_config
    from no_human.core.db import Store
    cfg = load_config(tmp_path / "config.yaml")
    store = await Store(tmp_path / "t.db").connect()
    try:
        for task in (None, _Task({}), _Task({"backend": None})):
            orch = build_orchestrator(cfg, store, task=task)
            assert isinstance(orch.backend, ClaudeBackend)
    finally:
        await store.close()


async def test_a_per_task_codex_run_gets_the_credential_preflight_at_construction(tmp_path, monkeypatch):
    """Review finding B1: the global `worker.backend: codex` path loads
    OPENAI_API_KEY from ~/.no_human/.env at `nh start`; a per-task codex on a
    claude-default install skipped that and died at its first coder turn. Now
    the same assertion runs when the orchestrator is built, and names the
    flag rather than the config key."""
    from no_human.config import AuthError, load_config
    from no_human.core.db import Store
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("no_human.config.ENV_PATH", tmp_path / "no-such.env")
    cfg = load_config(tmp_path / "config.yaml")
    store = await Store(tmp_path / "t.db").connect()
    try:
        with pytest.raises(AuthError) as e:
            build_orchestrator(cfg, store, task=_Task({"backend": "codex"}))
        assert "--backend" in str(e.value) and "OPENAI_API_KEY" in str(e.value)
    finally:
        await store.close()


async def test_a_per_task_codex_run_without_the_cli_is_refused_at_construction(tmp_path, monkeypatch):
    from no_human.agent.backend import BackendUnavailable
    from no_human.config import load_config
    from no_human.core.db import Store
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr("no_human.agent.codex_backend.find_codex_cli", lambda explicit=None: None)
    cfg = load_config(tmp_path / "config.yaml")
    store = await Store(tmp_path / "t.db").connect()
    try:
        with pytest.raises(BackendUnavailable, match="--backend codex"):
            build_orchestrator(cfg, store, task=_Task({"backend": "codex"}))
    finally:
        await store.close()


async def test_a_per_task_codex_subscription_run_is_preflighted_too(
        tmp_path, monkeypatch):
    """The dispatcher-aware twin of the two tests above: a task opts into
    codex while the install's `llm.codex_auth_mode` is `subscription`, and
    the per-task preflight must route through `assert_codex_mode` (not a
    hardcoded api_key check) — refusing when no live ChatGPT session is
    found, and never calling `codex login` or reading `~/.codex/auth.json`
    itself to find out."""
    from no_human.config import AuthError, load_config
    from no_human.core.db import Store
    import no_human.agent.codex_backend as cx

    (tmp_path / "config.yaml").write_text(
        "llm:\n  codex_auth_mode: subscription\n")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.data["llm"]["codex_auth_mode"] == "subscription"

    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(False, "none"))
    store = await Store(tmp_path / "t.db").connect()
    try:
        with pytest.raises(AuthError, match="codex login"):
            build_orchestrator(cfg, store, task=_Task({"backend": "codex"}))
    finally:
        await store.close()

    # And the positive path: a live ChatGPT session lets construction proceed
    # to the (separately-tested) CLI-presence check, not the credential one.
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(True, "chatgpt"))
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/stub/codex")
    store2 = await Store(tmp_path / "t2.db").connect()
    try:
        orch = build_orchestrator(cfg, store2, task=_Task({"backend": "codex"}))
        assert isinstance(orch.backend, CodexBackend)
        assert orch.backend.auth_mode == "subscription"
    finally:
        await store2.close()


def test_the_factory_ignores_an_override_for_every_non_coder_role():
    """Review finding N4: the Claude pin must be the factory's, not the one
    call site's — `make_backend(role="reviewer", backend="codex")` used to
    return Codex."""
    from no_human.agent.backend import CLAUDE_PINNED_ROLES
    for role in CLAUDE_PINNED_ROLES:
        be = make_backend(model="m", role=role, backend="codex", readonly=True)
        assert not isinstance(be, CodexBackend), role
