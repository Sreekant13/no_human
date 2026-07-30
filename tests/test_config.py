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
    assert cfg.review_model == "claude-opus-5"
    assert cfg["approval"]["auto_merge_on_approval"] is False
    # the metered key must never appear anywhere in the generated config
    assert "ANTHROPIC_API_KEY" not in cfg_path.read_text()


def test_load_config_tolerates_deprecated_tracker_block(tmp_path):
    """An old config still carrying a ``tracker:`` section (the removed TRACKER
    integration) must load with a single deprecation warning and be ignored —
    never crash, and never leak the dead section into the resolved config."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "tracker:\n  enabled: true\n  boards: [SPRINT1]\n"
        "concurrency:\n  workers: 2\n"
    )
    with pytest.warns(DeprecationWarning, match="tracker"):
        cfg = load_config(cfg_path)
    # ignored — not carried into the resolved config
    assert "tracker" not in cfg.data
    # the rest of the user's config still applies
    assert cfg["concurrency"]["workers"] == 2


def test_load_config_has_integrations_defaults(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg["integrations"]["jira"]["enabled"] is False
    assert "project_key" in cfg["integrations"]["jira"]
    assert cfg["integrations"]["circleci"]["enabled"] is False


def test_load_config_null_integrations_section_does_not_crash(tmp_path):
    # Deep-merge shadowing trap: `integrations:` set to null in the user file
    # replaces the whole default dict — loading must not crash, and the
    # registry must tolerate the null section.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("integrations:\nllm:\n  auth_mode: subscription\n")
    cfg = load_config(cfg_path)
    assert cfg.data.get("integrations") is None  # shadowed to null, as documented
    from no_human.integrations import list_integrations
    assert len(list_integrations(cfg.data)) == 6  # registry tolerates it


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
    assert llm["review_model"] == "claude-opus-5"
    assert llm["planner_model"] == "claude-opus-5"


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


def test_ci_gate_block_ships_disabled_and_topology_free():
    """The CI_GATE gate ships disabled AND carries no deployment topology.

    A packaged build serves the effective config over ``/api/config``, so any
    project id, cluster name or job path left in these defaults is readable by
    whoever installs the app. The operator supplies them in
    ``~/.no_human/config.yaml`` on their own machine instead.
    """
    from no_human.config import DEFAULT_CONFIG
    s = DEFAULT_CONFIG["ci_gate"]
    assert s["enabled"] is False
    # Empty, not merely different: gate.py's eligibility check requires
    # enabled + repos + project_id, so these make the gate inert rather than
    # misconfigured.
    assert s["project_id"] is None
    assert s["hostname"] == ""
    assert s["repos"] == []
    assert s["variables"] == {}
    assert s["kubeconfig"] == ""
    assert s["enrich_job_url"] == ""
    assert s["jenkins_controller"] == ""
    assert s["registry_prefix"] == ""
    assert s["ref"] == "main"
    # Generic, and still carries the required interpolation point.
    assert s["namespace_template"] == "ci-gate-pr{pr_number}"
    assert "{pr_number}" in s["namespace_template"]
    # Bounded send-back loop cap lives with the other blocker bounds.
    assert DEFAULT_CONFIG["blockers"]["max_ci_gate_fix_rounds"] == 3


def test_per_edit_lint_is_on_by_default():
    """W1.3: SWE-agent's biggest ACI win — a non-parsing edit costs one hook
    round, not a failed attempt. Safe as default: no-op without a confirmed
    lint command, fail-open on linter timeout."""
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["hooks"]["per_edit_lint"] is True


def test_stuck_active_watchdog_default():
    """The stuck-active watchdog threshold ships in DEFAULT_CONFIG (40 min,
    above the 30-min test timeout) — discoverable and tunable."""
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["blockers"]["stuck_active_minutes"] == 40
