"""The Settings-pane GLOBAL-default seam, end to end.

Two prior attempts at "let the board pick the coder backend" died right here:
a picker that could submit a backend this install cannot actually run
(`e6736a61`), refused only at first dispatch instead of at the point of
choice. Every other test in this repo either checks the pure view-model
(`web/src/backendPanelView.test.mjs`), the `/api/config` fields the
per-task picker reads (`tests/test_api_config_scrub.py`), or the PER-TASK
override path (`tests/test_per_task_backend.py`). None of them proves the
GLOBAL-default write itself reaches a real orchestrator: PUT ->
`config.yaml`'s `worker.backend` -> a freshly reloaded `Config` ->
`build_orchestrator` (with NO per-task override) constructs the right
backend -> the attempt's "models" event names it. This module is that
proof, using `core.backend_settings.apply_backend_change` directly (the
exact function `PUT /api/config/coder-backend` calls — see
`api/app.py::api_set_coder_backend`), so there is no need to spin up the
ASGI app to exercise the real write-then-construct seam.

Uses the `local` backend because `make_backend`'s local branch never makes
a network call at construction (`tests/test_local_backend.py` — only
`assert_local_backend_mode` validates the URL's SHAPE), so this is a real,
resource-safe end-to-end run: no quota spent, no server needed.
"""
from __future__ import annotations

import pytest

from no_human.agent.backend import CLAUDE_PINNED_ROLES, LOCAL_CAPABILITIES, resolve_backend_name
from no_human.agent.claude_backend import ClaudeBackend
from no_human.config import load_config
from no_human.core.backend_settings import BackendSettingsError, apply_backend_change
from no_human.core.db import Store
from no_human.core.runtime import build_orchestrator

pytestmark = pytest.mark.real_backend


def _write_config(tmp_path, *, local_base_url=None, local_model=None):
    lines = []
    if local_base_url is not None or local_model is not None:
        lines.append("llm:")
        if local_base_url is not None:
            lines.append(f"  local_base_url: {local_base_url!r}")
        if local_model is not None:
            lines.append(f"  local_model: {local_model!r}")
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n" if lines else "")
    return path


async def test_settings_put_with_local_base_url_unset_is_refused_at_the_point_of_choice(tmp_path):
    """AC2's unset case, exercised through the SAME function the API's PUT
    handler calls (`core.backend_settings.apply_backend_change`) — never a
    frontend-invented rule."""
    config_path = _write_config(tmp_path)  # no llm.local_base_url at all
    cfg = load_config(config_path)

    with pytest.raises(BackendSettingsError, match="llm.local_base_url is not set"):
        apply_backend_change(
            {"backend": "local"}, running_cfg_data=cfg.data, config_path=config_path,
        )

    # Nothing was written: worker.backend on disk is still unset/default.
    assert resolve_backend_name(load_config(config_path).data, role="coder") == "claude"


async def test_settings_put_with_local_base_url_set_reaches_the_real_orchestrator(tmp_path):
    """AC2's positive case + AC3 (same check, no duplicate) + AC6 (the
    'models' event names the chosen backend), chained through the actual
    write -> reload -> construct -> emit sequence `_run_attempt` runs."""
    config_path = _write_config(
        tmp_path, local_base_url="http://localhost:8000", local_model="my-local-model",
    )
    cfg = load_config(config_path)

    payload, changes = apply_backend_change(
        {"backend": "local"}, running_cfg_data=cfg.data, config_path=config_path,
    )
    assert changes == {"backend": {"old": "claude", "new": "local"}}
    assert payload["current"] == "claude"  # the RUNNING process's view; unchanged until restart

    # The write is on disk now — a fresh load (what `nh start`/`nh serve` do
    # next boot, and what this test does in place of a restart) picks it up
    # as the GLOBAL default, with NO per-task override anywhere.
    reloaded = load_config(config_path)
    assert reloaded.data["worker"]["backend"] == "local"
    assert resolve_backend_name(reloaded.data, role="coder") == "local"

    store = await Store(tmp_path / "t.db").connect()
    events = []
    try:
        orch = build_orchestrator(reloaded, store, event_sink=events.append, task=None)

        assert isinstance(orch.backend, ClaudeBackend)
        assert orch.backend.model == "my-local-model"
        assert orch.backend.capabilities is LOCAL_CAPABILITIES

        # The exact two-line sequence `_run_attempt` runs (core/orchestrator.py
        # ~3987-4004), called directly so this test needs no full attempt/DB
        # row plumbing to observe the "models" event.
        models = orch._active_models()
        orch._emit_models(models)
    finally:
        await store.close()

    model_events = [e for e in events if e.get("kind") == "models"]
    assert len(model_events) == 1
    assert model_events[0]["models"]["coder"] == "my-local-model"


async def test_the_settings_written_config_still_pins_every_non_coder_role(tmp_path):
    """AC4, tied to THIS test's own config object (the one the Settings PUT
    actually produced) rather than only a fixture in another file: whatever
    the board submits for the coder role, `resolve_backend_name` must return
    'claude' for every pinned role against the exact same config."""
    config_path = _write_config(
        tmp_path, local_base_url="http://localhost:8000", local_model="my-local-model",
    )
    cfg = load_config(config_path)
    apply_backend_change(
        {"backend": "local"}, running_cfg_data=cfg.data, config_path=config_path,
    )
    reloaded = load_config(config_path)
    assert resolve_backend_name(reloaded.data, role="coder") == "local"

    for role in CLAUDE_PINNED_ROLES:
        assert resolve_backend_name(reloaded.data, role=role) == "claude", role


async def test_setting_local_fields_and_switching_in_one_request_bootstraps_local(tmp_path):
    """The gap this feature closes: a fresh install has no llm.local_base_url,
    so 'local' is unavailable and cannot be switched to. Sending the two fields
    ALONGSIDE the backend in one PUT must write them FIRST, so the availability
    check then passes and the switch lands — no hand-edit of config.yaml."""
    config_path = _write_config(tmp_path)  # nothing on disk
    cfg = load_config(config_path)

    payload, changes = apply_backend_change(
        {
            "backend": "local",
            "local_model": "my-local-model",
            "local_base_url": "http://localhost:8000",
        },
        running_cfg_data=cfg.data,
        config_path=config_path,
    )
    assert changes == {
        "local_model": {"old": "", "new": "my-local-model"},
        "local_base_url": {"old": "", "new": "http://localhost:8000"},
        "backend": {"old": "claude", "new": "local"},
    }
    reloaded = load_config(config_path)
    assert reloaded.data["worker"]["backend"] == "local"
    assert reloaded.data["llm"]["local_model"] == "my-local-model"
    assert reloaded.data["llm"]["local_base_url"] == "http://localhost:8000"
    # The GET payload now prefills the fields with those values.
    assert payload["local_fields"]["backend"] == "local"
    by_key = {f["key"]: f["value"] for f in payload["local_fields"]["fields"]}
    assert by_key == {
        "local_model": "my-local-model",
        "local_base_url": "http://localhost:8000",
    }


async def test_editing_only_the_local_fields_needs_no_backend_key(tmp_path):
    """Already on 'local', retuning the model id alone: a body with just the
    changed field (no "backend") is accepted and writes only that field."""
    config_path = _write_config(
        tmp_path, local_base_url="http://localhost:8000", local_model="old-model",
    )
    cfg = load_config(config_path)
    payload, changes = apply_backend_change(
        {"local_model": "new-model"},
        running_cfg_data=cfg.data,
        config_path=config_path,
    )
    assert changes == {"local_model": {"old": "old-model", "new": "new-model"}}
    reloaded = load_config(config_path)
    assert reloaded.data["llm"]["local_model"] == "new-model"
    assert reloaded.data["llm"]["local_base_url"] == "http://localhost:8000"


async def test_a_public_local_base_url_is_refused_with_nothing_written(tmp_path):
    """The URL safety boundary (loopback/RFC1918 only) is enforced BEFORE any
    write — a public host is refused and config.yaml is left untouched."""
    config_path = _write_config(tmp_path)
    cfg = load_config(config_path)
    with pytest.raises(BackendSettingsError, match="public/routable"):
        apply_backend_change(
            {"backend": "local", "local_base_url": "http://8.8.8.8:8000",
             "local_model": "m"},
            running_cfg_data=cfg.data,
            config_path=config_path,
        )
    # Nothing landed: no worker.backend, no llm.local_base_url.
    reloaded = load_config(config_path)
    assert resolve_backend_name(reloaded.data, role="coder") == "claude"
    assert not (reloaded.data.get("llm") or {}).get("local_base_url")


async def test_empty_body_is_refused(tmp_path):
    """Neither a backend nor a local field: one operator-facing sentence."""
    config_path = _write_config(tmp_path)
    cfg = load_config(config_path)
    with pytest.raises(BackendSettingsError, match="backend"):
        apply_backend_change({}, running_cfg_data=cfg.data, config_path=config_path)


def test_a_fourth_supported_backends_entry_shows_up_in_the_options_with_no_code_change(
    monkeypatch, tmp_path,
):
    """AC1's proof at the Python/API layer — the mirror of
    `backendPanelView.test.mjs`'s "a fourth SUPPORTED_BACKENDS entry shows up
    with no change to this file" test on the JS side. `backend_payload`'s
    options list must be driven ENTIRELY by `SUPPORTED_BACKENDS` (both the
    tuple `agent.backend` defines and the name `core.backend_settings`
    imported from it), with no length-3 assumption baked in anywhere between
    them. Monkeypatching both bindings to add a temporary 4th name and
    confirming it appears — available, since `assert_task_backend_usable` has
    no branch for an unrecognised name and therefore never raises for it — is
    the same proof `describe_backend`'s own docstring makes in prose."""
    import no_human.agent.backend as backend_module
    import no_human.core.backend_settings as backend_settings_module

    extended = backend_module.SUPPORTED_BACKENDS + ("stub-fourth",)
    monkeypatch.setattr(backend_module, "SUPPORTED_BACKENDS", extended)
    monkeypatch.setattr(backend_settings_module, "SUPPORTED_BACKENDS", extended)

    config_path = _write_config(tmp_path)
    cfg = load_config(config_path)
    payload = backend_settings_module.backend_payload(cfg.data, config_path)

    ids = [o["id"] for o in payload["options"]]
    assert ids == ["claude", "codex", "local", "stub-fourth"]
    fourth = next(o for o in payload["options"] if o["id"] == "stub-fourth")
    assert fourth["available"] is True
    assert fourth["reason"] == ""
