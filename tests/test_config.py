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
    # CircleCI is a view over `ci.*`, like github_actions/gitlab/jenkins — it
    # has no `integrations.circleci` block. It used to, holding `enabled` +
    # `org_slug` + `project`, and NOTHING read any of the three: the block
    # rendered an on/off toggle and an onboarding form that governed nothing
    # while the panel claimed CircleCI was the active CI backend.
    assert "circleci" not in cfg["integrations"]
    # ...and the block it moved to exists, off by default, with the key the
    # CircleCI backend is built from.
    assert cfg["ci"]["enabled"] is False
    assert cfg["ci"]["project"] == ""


def test_load_config_null_integrations_section_does_not_crash(tmp_path):
    # Deep-merge shadowing trap: `integrations:` set to null in the user file
    # replaces the whole default dict — loading must not crash, and the
    # registry must tolerate the null section.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("integrations:\nllm:\n  auth_mode: subscription\n")
    cfg = load_config(cfg_path)
    assert cfg.data.get("integrations") is None  # shadowed to null, as documented
    from no_human.integrations import list_integrations
    assert len(list_integrations(cfg.data)) == 9  # registry tolerates it


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
    tier — an inherited choice nobody made. The supervisor is a sparse,
    single-turn course-corrector, so it has its own tier: Sonnet 5."""
    from no_human.config import DEFAULT_CONFIG
    llm = DEFAULT_CONFIG["llm"]
    assert llm["supervisor_model"] == "claude-sonnet-5"
    assert llm["primary_model"] == "claude-sonnet-5"
    assert llm["review_model"] == "claude-opus-4-8"
    assert llm["planner_model"] == "claude-opus-5"


def test_utility_tier_exists_and_does_not_touch_the_four_gates():
    """A fourth, advisory tier for summarize/classify/distill jobs, kept off
    the implement/plan/review/supervise path. It must never become a gate."""
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


def test_a_loaded_config_never_aliases_the_global_defaults(tmp_path):
    """A config.yaml that omits a section used to hand back DEFAULT_CONFIG's own
    nested dict: `_deep_merge` copied only the TOP level, so
    `merged["server"] is DEFAULT_CONFIG["server"]`. Any caller that then wrote
    into its own config — the nightly eval sets `server.port` for its isolated
    instance — silently re-pointed the DEFAULT for the whole process. Measured
    2026-08-10: DEFAULT_CONFIG["server"]["port"] 8420 -> 8431, which surfaced as
    a README-claims failure in an unrelated test."""
    from no_human.config import DEFAULT_CONFIG, load_config

    home = tmp_path / "home"
    home.mkdir()
    # No `server:` block — the shape that aliased.
    (home / "config.yaml").write_text("llm:\n  review_model: claude-opus-5\n")

    before = DEFAULT_CONFIG["server"]["port"]
    cfg = load_config(home / "config.yaml")

    assert cfg.data["server"] is not DEFAULT_CONFIG["server"], (
        "the loaded config aliases DEFAULT_CONFIG's nested dict")
    cfg.data["server"]["port"] = before + 11
    assert DEFAULT_CONFIG["server"]["port"] == before, (
        "writing to a loaded config mutated the global defaults")
    # ...and the user's own override still wins over the default.
    assert cfg.data["llm"]["review_model"] == "claude-opus-5"


# --------------------------------------------------------------------------- #
# Reviewer session windows (2026-08-11)                                        #
# --------------------------------------------------------------------------- #

def test_the_review_window_defaults_have_exactly_one_source_of_truth():
    """DEFAULT_CONFIG and the reviewer's own fallback constants must be the same
    numbers. Two spellings of a default is how a knob silently stops matching
    the code it configures; this is the guard that catches the drift. Break
    either side and this reddens."""
    from no_human.config import DEFAULT_CONFIG
    from no_human.review import reviewer as rv

    assert DEFAULT_CONFIG["llm"]["review_timeout_seconds"] == rv._REVIEW_TIMEOUT
    assert (DEFAULT_CONFIG["llm"]["code_review_timeout_seconds"]
            == rv._CODE_REVIEW_TIMEOUT)
    # The measured numbers themselves (see the constant's comment): above the
    # 1357s worst review round observed 2026-08-11, not the old 600s wall that
    # sat below the ~1078s mean.
    assert rv._REVIEW_TIMEOUT == 1500
    assert rv._CODE_REVIEW_TIMEOUT == 1800


def test_an_absurdly_small_review_window_is_clamped_to_the_floor():
    """A window under the floor is not a tuning choice, it is a typo: every
    review would time out twice and every task would escalate unreviewed. Clamp
    (loudly) rather than raise — a bad number in one knob must not make the
    whole install unloadable."""
    from no_human.config import (
        REVIEW_TIMEOUT_FLOOR_S,
        code_review_timeout_seconds,
        review_timeout_seconds,
    )

    assert review_timeout_seconds({"llm": {"review_timeout_seconds": 5}}) == (
        REVIEW_TIMEOUT_FLOOR_S)
    assert review_timeout_seconds({"llm": {"review_timeout_seconds": 0}}) == (
        REVIEW_TIMEOUT_FLOOR_S)
    assert review_timeout_seconds({"llm": {"review_timeout_seconds": -30}}) == (
        REVIEW_TIMEOUT_FLOOR_S)
    assert code_review_timeout_seconds(
        {"llm": {"code_review_timeout_seconds": 1}}) == REVIEW_TIMEOUT_FLOOR_S
    # At the floor exactly: honoured, not clamped away.
    assert review_timeout_seconds(
        {"llm": {"review_timeout_seconds": REVIEW_TIMEOUT_FLOOR_S}}
    ) == REVIEW_TIMEOUT_FLOOR_S


def test_a_nonnumeric_review_window_falls_back_to_the_default():
    """YAML hands back whatever was typed. A string, a null or a bool is not a
    number of seconds — take the measured default rather than crash or, worse,
    hand `asyncio.wait_for` something it will reject at review time."""
    from no_human.config import DEFAULT_CONFIG, review_timeout_seconds

    default = DEFAULT_CONFIG["llm"]["review_timeout_seconds"]
    for bad in ("25 minutes", None, True, [1500], {"s": 1500}):
        assert review_timeout_seconds({"llm": {"review_timeout_seconds": bad}}) == (
            default), bad
    # Absent key, absent section, empty dict — all the same default.
    assert review_timeout_seconds({"llm": {}}) == default
    assert review_timeout_seconds({}) == default


def test_config_exposes_the_review_windows_as_properties():
    """The Config object is what every production call site holds, so the knob
    has to be readable from it the way `review_model` is."""
    from no_human.config import Config, DEFAULT_CONFIG

    cfg = Config(data={"llm": {"review_timeout_seconds": 1200,
                               "code_review_timeout_seconds": 2400}},
                 path=None)
    assert cfg.review_timeout_seconds == 1200
    assert cfg.code_review_timeout_seconds == 2400

    empty = Config(data={"llm": {}}, path=None)
    assert empty.review_timeout_seconds == (
        DEFAULT_CONFIG["llm"]["review_timeout_seconds"])
    assert empty.code_review_timeout_seconds == (
        DEFAULT_CONFIG["llm"]["code_review_timeout_seconds"])


def test_a_nonfinite_review_window_is_rejected_rather_than_crashing_the_install(
        tmp_path, caplog):
    """`.inf`, `.nan` and `-.inf` are LEGAL YAML floats.

    Found in adversarial review of the config-driven window. They are real
    `float`s, so `isinstance(raw, (int, float))` waves them through; `nan < 60`
    is False so the floor branch does not catch them either; and `int(inf)` /
    `int(nan)` then raise OverflowError / ValueError — out of a property that
    every orchestrator construction reads (cli/commands.py `_build_orchestrator`)
    and out of `load_config` itself. One typo in one knob would make the install
    unloadable, which is the exact invariant `_timeout_knob` states twice and
    the reason it clamps instead of raising. Driven through `yaml.safe_load`
    and `load_config`, not through hand-built floats: the point is that YAML
    produces these from ordinary-looking text.
    """
    import logging
    import math

    import yaml as _yaml

    from no_human.config import DEFAULT_CONFIG, load_config, review_timeout_seconds

    default = DEFAULT_CONFIG["llm"]["review_timeout_seconds"]
    for literal in (".inf", ".nan", "-.inf"):
        data = _yaml.safe_load(f"llm:\n  review_timeout_seconds: {literal}\n")
        raw = data["llm"]["review_timeout_seconds"]
        # The instrument first: if YAML ever stopped producing a non-finite
        # float here, the rest of this test would be checking nothing.
        assert isinstance(raw, float) and not math.isfinite(raw), (literal, raw)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="no_human.config"):
            assert review_timeout_seconds(data) == default, literal
        assert any("review_timeout_seconds" in r.getMessage()
                   for r in caplog.records), (literal, caplog.records)

    # End to end, the way the crash was reproduced: a config.yaml on disk ->
    # load_config -> the property every reviewer construction reads.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "llm:\n  review_timeout_seconds: .inf\n  code_review_timeout_seconds: .nan\n")
    cfg = load_config(home / "config.yaml")
    assert cfg.review_timeout_seconds == default
    assert cfg.code_review_timeout_seconds == (
        DEFAULT_CONFIG["llm"]["code_review_timeout_seconds"])

    # ...and the reviewer that reads it still builds, which is what "unloadable"
    # actually meant.
    from no_human.review.reviewer import AdversarialReviewer

    class _B:
        model = "claude-opus-5"

    reviewer = AdversarialReviewer.from_config(cfg.data, backend=_B())
    assert reviewer._timeout == default


def test_the_config_floor_never_inverts_the_retry_window():
    """`_agent_review` halves a timed-out round's window but floors it at
    `_REVIEW_MIN_RETRY_TIMEOUT`. If the config floor sat BELOW that floor, a
    configured window in between would give round TWO a bigger window than
    round one — an inversion of the rule that a hang must escalate sooner, not
    later. Aligning the two removes the case rather than documenting it."""
    from no_human.config import REVIEW_TIMEOUT_FLOOR_S
    from no_human.review.reviewer import _REVIEW_MIN_RETRY_TIMEOUT

    assert REVIEW_TIMEOUT_FLOOR_S >= _REVIEW_MIN_RETRY_TIMEOUT, (
        f"a configured window between {_REVIEW_MIN_RETRY_TIMEOUT} and "
        f"{REVIEW_TIMEOUT_FLOOR_S} would grow on retry instead of shrinking")
