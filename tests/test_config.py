"""Auth scrub + subscription-mode assertion — the load-bearing safety boundary."""

import os

import pytest

from no_human import config
from no_human.config import (
    AuthError,
    _atomic_write_text,
    assert_subscription_mode,
    load_config,
    load_env_token,
    scrub_metered_auth,
)


def test_scrub_removes_all_metered_vars(monkeypatch):
    for var in config.METERED_AUTH_VARS:
        monkeypatch.setenv(var, "x")
    report = scrub_metered_auth()
    assert set(report.removed) == set(config.METERED_AUTH_VARS)
    assert report.api_key_present is True
    for var in config.METERED_AUTH_VARS:
        assert var not in os.environ


def test_scrub_ignores_absent_vars(monkeypatch):
    for var in config.METERED_AUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    report = scrub_metered_auth()
    assert report.removed == []
    assert report.api_key_present is False


def test_assert_subscription_refuses_when_api_key_present(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        assert_subscription_mode(env_path=tmp_path / "nope.env")
    # scrubbed even though it raised — cannot fall through to metered billing
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_assert_subscription_requires_token(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(AuthError, match="No subscription token"):
        assert_subscription_mode(env_path=tmp_path / "nope.env")


def test_assert_subscription_succeeds_with_token(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    report = assert_subscription_mode(env_path=tmp_path / "nope.env")
    assert report.api_key_present is False


def test_load_env_token_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nCLAUDE_CODE_OAUTH_TOKEN="file-token"\n')
    assert load_env_token(env) == "file-token"
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "file-token"


def test_load_config_generates_default(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg = load_config(cfg_path)
    assert cfg_path.exists()
    assert cfg.primary_model == "claude-sonnet-5"
    assert cfg.review_model == "claude-opus-4-8"
    assert cfg["approval"]["auto_merge_on_approval"] is False
    # the metered key must never appear anywhere in the generated config
    assert "ANTHROPIC_API_KEY" not in cfg_path.read_text()


def test_load_config_rejects_api_key(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  ANTHROPIC_API_KEY: sk-ant-leak\n")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        load_config(cfg_path)


def test_atomic_write_text_uses_os_replace(tmp_path, monkeypatch):
    """Guard: _atomic_write_text must go through os.replace, not direct write."""
    target = tmp_path / "config.yaml"
    replaced = []
    real_replace = os.replace
    def spy_replace(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy_replace)
    _atomic_write_text(target, "key: value\n")
    assert target.read_text() == "key: value\n"
    assert len(replaced) == 1
    assert replaced[0][1] == str(target)
    assert replaced[0][0].endswith(".yaml.tmp")


def test_atomic_write_text_no_partial_read(tmp_path):
    """A concurrent reader never sees a half-written file."""
    target = tmp_path / "config.yaml"
    target.write_text("original")
    _atomic_write_text(target, "replaced content")
    assert target.read_text() == "replaced content"
    assert not target.with_suffix(".yaml.tmp").exists()


def test_no_fake_auto_pr_switch():
    """`git.auto_pr` was a config key nothing read — it looked like a safety
    switch and was not one. A fake off-switch is worse than none."""
    from no_human.config import DEFAULT_CONFIG
    assert "auto_pr" not in DEFAULT_CONFIG["git"]


def test_supervisor_has_its_own_model_key():
    """The supervisor rode on review_model, so it silently ran at the reviewer's
    tier — an inherited choice nobody made. CLAUDE.md #7 (amended) puts it on
    Sonnet 5."""
    from no_human.config import DEFAULT_CONFIG
    llm = DEFAULT_CONFIG["llm"]
    assert llm["supervisor_model"] == "claude-sonnet-5"
    assert llm["primary_model"] == "claude-sonnet-5"
    assert llm["review_model"] == "claude-opus-4-8"
    assert llm["planner_model"] == "claude-opus-4-8"


def test_utility_tier_exists_and_does_not_touch_the_four_gates():
    """CLAUDE.md #7 (amended 2026-07-09): a utility tier for advisory
    summarize/classify/distill jobs. It must never become a gate."""
    from no_human.config import DEFAULT_CONFIG
    llm = DEFAULT_CONFIG["llm"]
    assert llm["utility_model"] == "claude-haiku-4-5"
    for gate in ("primary_model", "planner_model", "review_model",
                 "supervisor_model"):
        assert llm[gate] != llm["utility_model"], (
            f"{gate} must not run on the utility tier"
        )


def test_utility_model_property_survives_a_config_that_predates_the_key():
    """~/.no_human/config.yaml is written once and deep-merged forever. A config
    frozen before the utility tier existed must still resolve the new default,
    not crash and not silently fall back to a gate model."""
    from pathlib import Path
    from no_human.config import Config, DEFAULT_CONFIG
    stale = Config(data={"llm": {"auth_mode": "subscription"}},
                   path=Path("/nonexistent/config.yaml"))
    assert stale.utility_model == DEFAULT_CONFIG["llm"]["utility_model"]
