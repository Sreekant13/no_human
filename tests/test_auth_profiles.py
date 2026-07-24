"""Auth profiles: which subscription pays for a run (M0.0).

The property under test is a billing-safety one. A named profile must resolve
its own token or fail loudly — never fall back to another subscription's token,
never leak a token value into a name-only surface.
"""

import os

import pytest

from no_human import config
from no_human.config import (
    DEFAULT_AUTH_PROFILE,
    SUBSCRIPTION_TOKEN_VAR,
    AuthError,
    active_auth_profile,
    assert_subscription_mode,
    available_auth_profiles,
    load_config,
    load_env_token,
    profile_token_var,
    set_auth_profile,
)
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

PERSONAL_VAR = "CLAUDE_CODE_OAUTH_TOKEN_PERSONAL"
ENTERPRISE_VAR = "CLAUDE_CODE_OAUTH_TOKEN_ENTERPRISE"


@pytest.fixture(autouse=True)
def _isolate_auth_env(monkeypatch):
    """No token variable and no recorded profile leaks between tests."""
    for var in (SUBSCRIPTION_TOKEN_VAR, PERSONAL_VAR, ENTERPRISE_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_ACTIVE_AUTH_PROFILE", None)


def test_profile_token_var_maps_default_to_the_unsuffixed_variable():
    assert profile_token_var(DEFAULT_AUTH_PROFILE) == SUBSCRIPTION_TOKEN_VAR
    assert profile_token_var("personal") == PERSONAL_VAR
    assert profile_token_var("ENTERPRISE") == ENTERPRISE_VAR


def test_named_profile_exports_its_own_token(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        f"{SUBSCRIPTION_TOKEN_VAR}=default-tok\n{PERSONAL_VAR}=personal-tok\n"
    )
    assert load_env_token(env, profile="personal") == "personal-tok"
    # exactly one token is exported, and it is the SDK's canonical variable
    assert os.environ[SUBSCRIPTION_TOKEN_VAR] == "personal-tok"
    assert active_auth_profile() == "personal"


def test_named_profile_without_a_token_never_falls_back_to_another(tmp_path):
    """The billing-safety property: a silent fallback would bill the wrong
    subscription. Reverting to `token = ... or os.environ[SUBSCRIPTION_TOKEN_VAR]`
    makes this test fail with 'default-tok' exported under the personal name."""
    env = tmp_path / ".env"
    env.write_text(f"{SUBSCRIPTION_TOKEN_VAR}=default-tok\n")
    with pytest.raises(AuthError, match="profile 'personal' has no token"):
        load_env_token(env, profile="personal")
    assert os.environ.get(SUBSCRIPTION_TOKEN_VAR) != "personal-tok"
    assert active_auth_profile() is None


def test_default_profile_still_reads_the_unsuffixed_variable(tmp_path):
    """Backwards compatibility: the pre-profile .env keeps working untouched."""
    env = tmp_path / ".env"
    env.write_text(f'# comment\n{SUBSCRIPTION_TOKEN_VAR}="file-token"\n')
    assert load_env_token(env) == "file-token"
    assert active_auth_profile() == DEFAULT_AUTH_PROFILE


def test_default_profile_returns_none_when_no_token_anywhere(tmp_path):
    assert load_env_token(tmp_path / "absent.env") is None
    assert active_auth_profile() is None


def test_env_file_wins_over_an_inherited_profile_token(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONAL_VAR, "inherited")
    env = tmp_path / ".env"
    env.write_text(f"{PERSONAL_VAR}=curated\n")
    assert load_env_token(env, profile="personal") == "curated"


def test_inherited_profile_token_is_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONAL_VAR, "inherited")
    assert load_env_token(tmp_path / "absent.env", profile="personal") == "inherited"


def test_available_profiles_lists_names_never_values(tmp_path, monkeypatch):
    monkeypatch.setenv(ENTERPRISE_VAR, "enterprise-tok")
    env = tmp_path / ".env"
    env.write_text(
        f"{SUBSCRIPTION_TOKEN_VAR}=default-tok\n"
        f"{PERSONAL_VAR}=personal-tok\n"
        "SSO_PASSWORD=hunter2\n"
    )
    profiles = available_auth_profiles(env)
    assert profiles == ["default", "enterprise", "personal"]
    for secret in ("default-tok", "personal-tok", "enterprise-tok", "hunter2"):
        assert not any(secret in p for p in profiles)


def test_available_profiles_ignores_empty_tokens(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{SUBSCRIPTION_TOKEN_VAR}=\n{PERSONAL_VAR}=tok\n")
    assert available_auth_profiles(env) == ["personal"]


def test_assert_subscription_mode_honours_the_profile(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{PERSONAL_VAR}=personal-tok\n")
    report = assert_subscription_mode(env_path=env, profile="personal")
    assert report.api_key_present is False
    assert os.environ[SUBSCRIPTION_TOKEN_VAR] == "personal-tok"


def test_assert_subscription_mode_still_scrubs_the_api_key_under_a_profile(
    monkeypatch, tmp_path
):
    """The scrub semantics must survive the profile plumbing untouched."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    env = tmp_path / ".env"
    env.write_text(f"{PERSONAL_VAR}=personal-tok\n")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        assert_subscription_mode(env_path=env, profile="personal")
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_default_config_pins_the_default_profile():
    assert config.DEFAULT_CONFIG["llm"]["auth_profile"] == DEFAULT_AUTH_PROFILE


def test_config_predating_auth_profiles_resolves_the_default(tmp_path):
    """The frozen-config trap: a config.yaml written before this key existed
    must still resolve `default`, not None."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_mode: subscription\n")
    assert load_config(cfg_path).data["llm"]["auth_profile"] == DEFAULT_AUTH_PROFILE


def test_bootstrap_survives_an_llm_block_commented_out(tmp_path, monkeypatch):
    """config.yaml is hand-edited. A bare `llm:` with its body commented out
    deep-merges to None; reading it as `config["llm"].get(...)` raises
    AttributeError before the auth error the operator needs to see."""
    from no_human.cli import commands as cmd_mod

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  # auth_mode: subscription\nserver:\n  port: 8420\n")
    cfg = load_config(cfg_path)
    assert cfg.data["llm"] is None

    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    seen = {}
    monkeypatch.setattr(
        cmd_mod, "assert_subscription_mode", lambda **kw: seen.update(kw)
    )
    cmd_mod._bootstrap()
    # _bootstrap passes both the profile and the billing mode; a bare/commented
    # `llm:` block yields the default subscription mode without raising.
    assert seen == {"profile": None, "auth_mode": "subscription"}


# --------------------------- set_auth_profile ------------------------------ #


def test_set_auth_profile_preserves_comments_and_round_trips(tmp_path):
    """A safe_load/safe_dump round-trip would delete the operator's comments —
    including the one warning that pinning models here shadowed real defaults."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "server:\n"
        "  port: 8420\n"
        "llm:\n"
        "  # Model IDs are intentionally NOT pinned here — config.py owns them.\n"
        "  auth_mode: subscription\n"
        "git:\n"
        "  branch_prefix: no-human/\n"
    )
    assert set_auth_profile("Personal", cfg_path) == "personal"

    text = cfg_path.read_text()
    assert "# Model IDs are intentionally NOT pinned here" in text
    assert "branch_prefix: no-human/" in text
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "personal"
    assert load_config(cfg_path).data["server"]["port"] == 8420


def test_set_auth_profile_replaces_an_existing_pin(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_profile: personal\n  auth_mode: subscription\n")
    set_auth_profile("enterprise", cfg_path)
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "enterprise"
    assert cfg_path.read_text().count("auth_profile:") == 1


def test_set_auth_profile_adds_the_llm_block_when_absent(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("server:\n  port: 8420\n")
    set_auth_profile("personal", cfg_path)
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "personal"


def test_set_auth_profile_edits_only_the_llm_block(tmp_path):
    """A key named auth_profile under another section must not be hijacked."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "ci_gate:\n  auth_profile: untouched\nllm:\n  auth_mode: subscription\n"
    )
    set_auth_profile("personal", cfg_path)
    data = load_config(cfg_path).data
    assert data["ci_gate"]["auth_profile"] == "untouched"
    assert data["llm"]["auth_profile"] == "personal"


@pytest.mark.parametrize(
    "bad",
    ["personal\nserver:\n  port: 9999", "../etc", "a b", "", "-lead", "x: y"],
)
def test_set_auth_profile_rejects_names_that_could_inject_yaml(tmp_path, bad):
    """The write is a text edit, so the value must not be able to add structure."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_mode: subscription\n")
    before = cfg_path.read_text()
    with pytest.raises(AuthError, match="invalid auth profile name"):
        set_auth_profile(bad, cfg_path)
    assert cfg_path.read_text() == before


# ------------------- attribution: every burn names its payer ---------------- #


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


class _StubBackend:
    model = "claude-sonnet-5"


def _orchestrator(store, tmp_path, events):
    return Orchestrator(
        store,
        load_config(tmp_path / "config.yaml").data,
        _StubBackend(),
        SlackNotifier(None),
        event_sink=events.append,
    )


async def test_models_event_names_the_paying_subscription(store, tmp_path, monkeypatch):
    """Every burn must be attributable to a subscription — by name, never token."""
    monkeypatch.setattr(config, "_ACTIVE_AUTH_PROFILE", "personal")
    events = []
    orch = _orchestrator(store, tmp_path, events)
    orch._emit_models({"coder": "claude-sonnet-5"})

    (event,) = [e for e in events if e["kind"] == "models"]
    assert event["auth_profile"] == "personal"
    assert "auth=personal" in event["text"]


async def test_models_event_omits_the_profile_when_none_was_exported(
    store, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "_ACTIVE_AUTH_PROFILE", None)
    events = []
    orch = _orchestrator(store, tmp_path, events)
    orch._emit_models({"coder": "claude-sonnet-5"})

    (event,) = [e for e in events if e["kind"] == "models"]
    assert event["auth_profile"] is None
    assert "auth=" not in event["text"]


async def test_attempt_row_records_the_paying_subscription(store):
    """Reverting the `auth_profile=` stamp on update_attempt leaves this NULL,
    and a burn becomes unattributable after the fact."""
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)
    await store.update_attempt(attempt_id, auth_profile="enterprise")

    (row,) = await store.list_attempts(task.id)
    assert row["auth_profile"] == "enterprise"
