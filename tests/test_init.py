"""Tests for `nh init` — the guided first-run setup.

Each test operates on isolated tmp dirs and patched HOME so we never touch the
real ``~/.no_human/`` or environment variables. CLI commands call asyncio.run()
internally so tests must be synchronous.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.cli.init_cmd import (
    _append_env,
    _check_tool,
    _env_has_key,
    _metered_key_in_env,
    ensure_home_dir,
)


# --------------------------------------------------------------------------- #
# Unit tests for init_cmd helpers                                              #
# --------------------------------------------------------------------------- #


def test_check_tool_found():
    """git should be found on any dev machine."""
    ver = _check_tool("git", "--version")
    assert ver is not None
    assert "git" in ver.lower()


def test_check_tool_not_found():
    """A non-existent binary returns None."""
    assert _check_tool("__no_such_binary_xyz__", "--version") is None


def test_ensure_home_dir_creates(tmp_path):
    nh_home = tmp_path / ".no_human"
    with patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home):
        created = ensure_home_dir()
    assert created is True
    assert nh_home.exists()
    assert (nh_home.stat().st_mode & 0o777) == 0o700


def test_ensure_home_dir_idempotent(tmp_path):
    nh_home = tmp_path / ".no_human"
    nh_home.mkdir(mode=0o700)
    with patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home):
        created = ensure_home_dir()
    assert created is False


def test_ensure_home_dir_fixes_permissions(tmp_path):
    nh_home = tmp_path / ".no_human"
    nh_home.mkdir(mode=0o755)
    with patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home):
        ensure_home_dir()
    assert (nh_home.stat().st_mode & 0o777) == 0o700


def test_env_has_key_true(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CLAUDE_CODE_OAUTH_TOKEN=abc123\n")
    with patch("no_human.cli.init_cmd.ENV_PATH", env):
        assert _env_has_key("CLAUDE_CODE_OAUTH_TOKEN") is True


def test_env_has_key_false(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER_KEY=val\n")
    with patch("no_human.cli.init_cmd.ENV_PATH", env):
        assert _env_has_key("CLAUDE_CODE_OAUTH_TOKEN") is False


def test_env_has_key_no_file(tmp_path):
    env = tmp_path / ".env"
    with patch("no_human.cli.init_cmd.ENV_PATH", env):
        assert _env_has_key("CLAUDE_CODE_OAUTH_TOKEN") is False


def test_env_has_key_empty_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n")
    with patch("no_human.cli.init_cmd.ENV_PATH", env):
        assert _env_has_key("CLAUDE_CODE_OAUTH_TOKEN") is False


def test_append_env_creates_file(tmp_path):
    env = tmp_path / ".env"
    nh_home = tmp_path / ".no_human"
    nh_home.mkdir(mode=0o700)
    with (
        patch("no_human.cli.init_cmd.ENV_PATH", env),
        patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home),
    ):
        _append_env("MY_KEY", "my_value")
    assert env.exists()
    assert "MY_KEY=my_value" in env.read_text()
    assert (env.stat().st_mode & 0o777) == 0o600


def test_append_env_replaces_existing(tmp_path):
    env = tmp_path / ".env"
    nh_home = tmp_path / ".no_human"
    nh_home.mkdir(mode=0o700)
    env.write_text("MY_KEY=old_value\nOTHER=keep\n")
    with (
        patch("no_human.cli.init_cmd.ENV_PATH", env),
        patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home),
    ):
        _append_env("MY_KEY", "new_value")
    content = env.read_text()
    assert "MY_KEY=new_value" in content
    assert "old_value" not in content
    assert "OTHER=keep" in content


def test_append_env_no_duplicate(tmp_path):
    env = tmp_path / ".env"
    nh_home = tmp_path / ".no_human"
    nh_home.mkdir(mode=0o700)
    env.write_text("# comment\nKEY1=a\n")
    with (
        patch("no_human.cli.init_cmd.ENV_PATH", env),
        patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home),
    ):
        _append_env("KEY2", "b")
    content = env.read_text()
    assert "KEY1=a" in content
    assert "KEY2=b" in content
    assert content.count("KEY1") == 1


def test_metered_key_detects_anthropic_api_key():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
        assert _metered_key_in_env() == "ANTHROPIC_API_KEY"


def test_metered_key_none_when_clean():
    env_clean = {k: v for k, v in os.environ.items() if k not in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS", "AWS_BEARER_TOKEN_BEDROCK",
    )}
    with patch.dict(os.environ, env_clean, clear=True):
        assert _metered_key_in_env() is None


# --------------------------------------------------------------------------- #
# Integration test: nh init with non-interactive input                         #
# --------------------------------------------------------------------------- #


def test_init_noninteractive_existing_setup(tmp_path, monkeypatch):
    """When config + token exist, `nh init` succeeds without prompts for steps 1-4.
    Step 5 (onboard) is declined via input."""
    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    config_path = nh_home / "config.yaml"
    config_path.write_text("llm:\n  auth_mode: subscription\n")
    env_path = nh_home / ".env"
    env_path.write_text("CLAUDE_CODE_OAUTH_TOKEN=test_token\n")
    env_path.chmod(0o600)

    import no_human.cli.init_cmd as init_mod
    import no_human.cli.commands as cmd_mod

    monkeypatch.setattr(init_mod, "NO_HUMAN_HOME", nh_home)
    monkeypatch.setattr(init_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)

    # Patch load_config for the ensure_config check.
    class _FakeCfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        db_path = nh_home / "test.db"
        data: dict = {}
        def get(self, key, default=None):
            return self.data.get(key, default)
        def __getitem__(self, key):
            return self.data[key]

    monkeypatch.setattr(init_mod, "load_config", lambda **kw: _FakeCfg())

    runner = CliRunner()
    # Input: "n" to decline onboarding.
    result = runner.invoke(cli, ["init"], input="n\n", catch_exceptions=False)
    assert result.exit_code == 0
    assert "no_human initialized" in result.output or "no_human" in result.output


def test_init_shows_prerequisites(tmp_path, monkeypatch):
    """Init step 1 should show git as found."""
    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    config_path = nh_home / "config.yaml"
    config_path.write_text("llm:\n  auth_mode: subscription\n")
    env_path = nh_home / ".env"
    env_path.write_text("CLAUDE_CODE_OAUTH_TOKEN=test_token\n")
    env_path.chmod(0o600)

    import no_human.cli.init_cmd as init_mod

    monkeypatch.setattr(init_mod, "NO_HUMAN_HOME", nh_home)
    monkeypatch.setattr(init_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)
    monkeypatch.setattr(init_mod, "load_config", lambda **kw: None)

    runner = CliRunner()
    result = runner.invoke(cli, ["init"], input="n\n", catch_exceptions=False)
    # Should show git as found.
    assert "git" in result.output.lower()
