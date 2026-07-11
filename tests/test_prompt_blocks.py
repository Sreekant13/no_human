"""EH4 step 1: the pure prompt blocks extracted from the orchestrator."""

from types import SimpleNamespace

from no_human.core.prompt_blocks import build_profile_block, build_rules_block


def test_rules_block_core_discipline_always_present():
    r = build_rules_block("", "", None)
    assert "NEVER SKIP A TASK" in r
    assert "SMALLEST change" in r
    assert "Rank every decision 1–10" in r
    # No test command → the generic fallback line, not the EARLY VERIFICATION one.
    assert "Run the project's test suite" in r
    assert "EARLY VERIFICATION" not in r


def test_rules_block_test_cmd_switches_to_early_verification():
    r = build_rules_block("uv run pytest -q", "", None)
    assert "Run unit tests with: uv run pytest -q" in r
    assert "EARLY VERIFICATION" in r
    assert "Run the project's test suite" not in r


def test_rules_block_ci_line_only_without_integration_cmd():
    # CI line appears when there's a CI runner and NO local integration cmd.
    with_ci = build_rules_block("", "", "gitlab")
    assert "Remote CI (gitlab) will run" in with_ci
    # …but is suppressed when a local integration command is given instead.
    with_integ = build_rules_block("", "uv run pytest tests/integration", "gitlab")
    assert "Remote CI (gitlab) will run" not in with_integ
    assert "Integration tests run on GitLab CI" in with_integ
    # …and absent entirely with no CI at all.
    assert "Remote CI" not in build_rules_block("", "", None)


def test_profile_block_empty_when_no_profile():
    assert build_profile_block(None) == ""


def test_profile_block_lists_the_repo_commands():
    prof = SimpleNamespace(
        ecosystem="python-pytest", test_cmd="uv run pytest -q",
        integration_test_cmd="", install_cmd="uv sync", lint_cmd="",
        ci={"enabled": True, "backend": "gitlab", "project": "x/y"},
    )
    b = build_profile_block(prof)
    assert b.startswith("Project profile (confirmed):")
    assert "Ecosystem: python-pytest" in b
    assert "Unit test command: uv run pytest -q" in b
    assert "Install command: uv sync" in b
    assert "Remote CI: gitlab (x/y)" in b
    assert "Lint command" not in b   # empty lint_cmd omitted
    assert b.endswith("\n\n")
