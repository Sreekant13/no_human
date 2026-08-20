"""Unit coverage for the `local` coding backend (worker.backend: "local").

Part 1 (already on main) built `config.assert_local_backend_mode` — the
`llm.local_base_url` safety boundary — but never wired anything to it. This
module covers part 2: `make_backend`'s `local` branch constructs the EXISTING
`ClaudeBackend` with a per-subprocess `extra_env` dict, never `os.environ`,
and never leaking into another role's session.

Every test that inspects `ClaudeBackend.extra_env` / `.cli_path` /
`.capabilities` needs the REAL class, not `conftest.py`'s autouse hermetic
stub (`_hermetic_sdk` patches `no_human.agent.claude_backend.ClaudeBackend`
to `_HermeticUtilityBackend` on every test that doesn't opt out) — hence
`pytestmark = pytest.mark.real_backend` for the whole module. Nothing here
spawns a real subprocess: every test stops at construction or at
`ClaudeBackend._options()`, which builds the `ClaudeAgentOptions` object
without starting the CLI.
"""

import pytest

pytestmark = pytest.mark.real_backend

from no_human.agent.backend import (
    LOCAL_BACKEND_FALLBACK_API_KEY,
    LOCAL_CAPABILITIES,
    BackendUnavailable,
    _local_child_env,
    make_backend,
    resolve_backend_name,
)
from no_human.agent.claude_backend import CLAUDE_CAPABILITIES, ClaudeBackend
from no_human.config import LOCAL_LLM_API_KEY_VAR, AuthError, read_env_var_value
from no_human.core import pricing

LOCAL_CFG = {
    "worker": {"backend": "local"},
    "llm": {
        "local_base_url": "http://localhost:8000",
        "local_model": "my-local-model",
    },
}


# --------------------------------------------------------------------------- #
# LOCAL_CAPABILITIES — the honest contract.                                    #
# --------------------------------------------------------------------------- #


def test_local_capabilities_declared_correctly():
    assert LOCAL_CAPABILITIES.name == "local"
    assert LOCAL_CAPABILITIES.blocks_tool_calls is True
    assert LOCAL_CAPABILITIES.post_tool_hooks is True
    assert LOCAL_CAPABILITIES.session_resume is True
    assert LOCAL_CAPABILITIES.subagents is True
    assert LOCAL_CAPABILITIES.skills is True
    assert LOCAL_CAPABILITIES.thinking_budget is False
    assert LOCAL_CAPABILITIES.incremental_usage is True
    assert LOCAL_CAPABILITIES.cache_creation_accounting is False
    assert LOCAL_CAPABILITIES.native_max_turns is True


# --------------------------------------------------------------------------- #
# AC1 — local-mode construction injects EXACTLY the three env entries.         #
# --------------------------------------------------------------------------- #


def test_local_mode_injects_exactly_three_env_entries():
    backend = make_backend(model="claude-sonnet-5", config=LOCAL_CFG)

    assert isinstance(backend, ClaudeBackend)
    assert set(backend.extra_env) == {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    }
    assert backend.extra_env["ANTHROPIC_BASE_URL"] == "http://localhost:8000"
    assert backend.extra_env["ANTHROPIC_API_KEY"] == LOCAL_BACKEND_FALLBACK_API_KEY
    assert backend.extra_env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_local_mode_uses_local_model_id():
    backend = make_backend(model="claude-sonnet-5", config=LOCAL_CFG)
    assert backend.model == "my-local-model"


def test_local_mode_declares_local_capabilities():
    backend = make_backend(model="claude-sonnet-5", config=LOCAL_CFG)
    assert backend.capabilities is LOCAL_CAPABILITIES
    assert backend.capabilities is not CLAUDE_CAPABILITIES


def test_local_mode_env_reaches_the_built_options(tmp_path):
    """The three entries actually flow into `ClaudeAgentOptions.env` — the
    field the SDK transport merges into the real subprocess's environment
    (`connect()` in `subprocess_cli.py`) — alongside the pre-existing compact-
    window override, not instead of it."""
    backend = make_backend(
        model="claude-sonnet-5", config=LOCAL_CFG, role="coder", readonly=False,
    )
    opts = backend._options(tmp_path, max_turns=10)

    assert opts.env["ANTHROPIC_BASE_URL"] == "http://localhost:8000"
    assert opts.env["ANTHROPIC_API_KEY"] == LOCAL_BACKEND_FALLBACK_API_KEY
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    # The pre-existing compact-window override still fires for a coder backend
    # — the local branch is additive, not a replacement of that mechanism.
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in opts.env


def test_local_mode_oauth_token_is_emptied_even_if_the_process_has_one(monkeypatch):
    """A local run must never carry the operator's real OAuth token to a
    third-party server — regardless of what happens to be exported in the
    parent process's own environment."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-should-not-leak")
    backend = make_backend(model="claude-sonnet-5", config=LOCAL_CFG)
    assert backend.extra_env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


# --------------------------------------------------------------------------- #
# AC1 (continued) — a PINNED role from the SAME config injects NONE.           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "role", ["reviewer", "planner", "supervisor", "utility", "intake"])
def test_pinned_role_from_same_config_injects_no_env(role):
    """`worker.backend: local` only ever reaches `role="coder"`
    (`CLAUDE_PINNED_ROLES`) — every other role must resolve to the plain
    `claude` branch and carry an EMPTY `extra_env`, from the exact same config
    dict a coder call on this config would turn into a local backend."""
    assert resolve_backend_name(LOCAL_CFG, role=role) == "claude"

    backend = make_backend(model="claude-opus-5", config=LOCAL_CFG, role=role)

    assert backend.extra_env == {}
    assert backend.cli_path is None
    assert backend.capabilities is CLAUDE_CAPABILITIES


# --------------------------------------------------------------------------- #
# Default construction — byte-identical to today.                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cfg", [None, {}, {"worker": {}}, {"worker": {"backend": "claude"}}])
def test_default_construction_is_byte_identical_to_today(cfg):
    """No config, an absent `worker.backend`, and an explicit `"claude"` all
    take the pre-existing branch and must construct a `ClaudeBackend` with the
    SAME defaults it always had: no injected env, no CLI override, the
    reference capability record."""
    backend = make_backend(model="claude-sonnet-5", config=cfg)

    assert isinstance(backend, ClaudeBackend)
    assert backend.model == "claude-sonnet-5"
    assert backend.extra_env == {}
    assert backend.cli_path is None
    assert backend.capabilities is CLAUDE_CAPABILITIES


def test_construction_with_no_extra_env_or_cli_path_kwargs_is_unchanged():
    """Direct `ClaudeBackend(...)` construction — the path every existing
    caller (reviewer, planner, supervisor, utility, advisory, the claude
    branch of `make_backend`) uses — is unaffected by the new constructor
    kwargs: they default to "inject nothing"."""
    backend = ClaudeBackend(model="claude-opus-5")
    assert backend.extra_env == {}
    assert backend.cli_path is None
    assert backend.capabilities is CLAUDE_CAPABILITIES


# --------------------------------------------------------------------------- #
# AC3 — assert_local_backend_mode's refusals reach the factory.                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_llm_cfg", [
    {"local_model": "m"},  # local_base_url missing entirely
    {"local_model": "m", "local_base_url": ""},
    {"local_model": "m", "local_base_url": "http://example.com"},  # DNS name
    {"local_model": "m", "local_base_url": "http://8.8.8.8"},  # public IP
    {"local_model": "m", "local_base_url": "http://user:pass@localhost:8000"},
    {"local_model": "m", "local_base_url": "ftp://localhost:8000"},
])
def test_local_base_url_refusals_reach_the_factory(bad_llm_cfg):
    cfg = {"worker": {"backend": "local"}, "llm": bad_llm_cfg}
    with pytest.raises(AuthError):
        make_backend(model="claude-sonnet-5", config=cfg)


def test_local_requires_a_model_id_and_does_not_crash_pricing():
    """An empty/missing `llm.local_model` is a clean `BackendUnavailable`, not
    a crash — and once a model id IS set, it flows straight through to
    `core/pricing.py`'s existing unknown-pricing path (no edit to that
    module): the id is named rather than swallowed."""
    cfg_no_model = {
        "worker": {"backend": "local"},
        "llm": {"local_base_url": "http://localhost:8000"},
    }
    with pytest.raises(BackendUnavailable):
        make_backend(model="claude-sonnet-5", config=cfg_no_model)

    pricing._reset_unknown_pricing_models()
    backend = make_backend(model="claude-sonnet-5", config=LOCAL_CFG)
    pricing.output_extra_weight(backend.model)
    assert "my-local-model" in pricing.unknown_pricing_models()
    pricing._reset_unknown_pricing_models()


# --------------------------------------------------------------------------- #
# ANTHROPIC_API_KEY: LOCAL_LLM_API_KEY from .env, else the literal fallback.    #
# --------------------------------------------------------------------------- #


def test_local_child_env_prefers_env_file_key(isolated_env_file):
    isolated_env_file.write_text(f"{LOCAL_LLM_API_KEY_VAR}=my-server-key\n")
    isolated_env_file.chmod(0o600)

    env = _local_child_env({"local_base_url": "http://localhost:8000"})

    assert env["ANTHROPIC_API_KEY"] == "my-server-key"


def test_local_child_env_falls_back_when_env_file_has_no_key(isolated_env_file):
    env = _local_child_env({"local_base_url": "http://localhost:8000"})
    assert env["ANTHROPIC_API_KEY"] == LOCAL_BACKEND_FALLBACK_API_KEY


def test_make_backend_local_reads_the_key_through_read_env_var_value(
        isolated_env_file):
    """End-to-end through the factory, not just the helper: the key that
    lands in `extra_env["ANTHROPIC_API_KEY"]` is exactly what
    `read_env_var_value(LOCAL_LLM_API_KEY_VAR)` returns."""
    isolated_env_file.write_text(f"{LOCAL_LLM_API_KEY_VAR}=from-the-env-file\n")
    isolated_env_file.chmod(0o600)

    backend = make_backend(model="claude-sonnet-5", config=LOCAL_CFG)

    assert backend.extra_env["ANTHROPIC_API_KEY"] == "from-the-env-file"
    assert read_env_var_value(LOCAL_LLM_API_KEY_VAR) == "from-the-env-file"


# --------------------------------------------------------------------------- #
# llm.local_cli_path plumbs to the backend's CLI-path parameter.               #
# --------------------------------------------------------------------------- #


def test_local_cli_path_plumbs_through():
    cfg = {
        "worker": {"backend": "local"},
        "llm": {
            "local_base_url": "http://localhost:8000",
            "local_model": "my-local-model",
            "local_cli_path": "/opt/local-claude/claude",
        },
    }
    backend = make_backend(model="claude-sonnet-5", config=cfg)
    assert backend.cli_path == "/opt/local-claude/claude"


def test_local_cli_path_null_means_sdk_bundled():
    backend = make_backend(model="claude-sonnet-5", config=LOCAL_CFG)
    assert backend.cli_path is None


# --------------------------------------------------------------------------- #
# config.read_env_var_value — the one additive config.py helper.               #
# --------------------------------------------------------------------------- #


def test_read_env_var_value_does_not_export_to_os_environ(isolated_env_file):
    import os

    isolated_env_file.write_text("SOME_LOCAL_VAR=abc123\n")
    isolated_env_file.chmod(0o600)

    assert read_env_var_value("SOME_LOCAL_VAR") == "abc123"
    assert "SOME_LOCAL_VAR" not in os.environ


def test_read_env_var_value_env_file_wins_over_process_env(
        isolated_env_file, monkeypatch):
    monkeypatch.setenv("SOME_LOCAL_VAR", "from-process-env")
    isolated_env_file.write_text("SOME_LOCAL_VAR=from-env-file\n")
    isolated_env_file.chmod(0o600)

    assert read_env_var_value("SOME_LOCAL_VAR") == "from-env-file"


def test_read_env_var_value_falls_back_to_process_env(
        isolated_env_file, monkeypatch):
    monkeypatch.setenv("SOME_LOCAL_VAR", "from-process-env")
    assert read_env_var_value("SOME_LOCAL_VAR") == "from-process-env"


def test_read_env_var_value_absent_returns_none(isolated_env_file):
    assert read_env_var_value("SOME_LOCAL_VAR_THAT_IS_NOT_SET") is None


def test_read_env_var_value_guards_metered_auth_vars():
    with pytest.raises(AuthError):
        read_env_var_value("ANTHROPIC_API_KEY")
    with pytest.raises(AuthError):
        read_env_var_value("ANTHROPIC_BASE_URL")


# --------------------------------------------------------------------------- #
# unknown coding backend error message names 'local'.                          #
# --------------------------------------------------------------------------- #


def test_unknown_backend_error_names_local_as_supported():
    cfg = {"worker": {"backend": "not-a-real-backend"}}
    with pytest.raises(BackendUnavailable) as excinfo:
        make_backend(model="claude-sonnet-5", config=cfg)
    assert "'local'" in str(excinfo.value)
