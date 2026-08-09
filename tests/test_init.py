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
    _version_too_old,
    check_prerequisites,
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


def test_nh_init_uses_the_HARDENED_writer_not_a_third_implementation(tmp_path):
    """`nh init` writes the OAuth token. It must go through the one guarded
    writer, not its own copy.

    This exact hardening shipped DEAD: the fixed `_append_env` was added while
    the old one was left in place further down the module, and Python binds the
    last definition — so `nh init` kept writing the token with
    `write_text` + `chmod`, leaving it world-readable for the window between
    the two calls, and kept parsing with `splitlines()` while the reader used
    `split("\\n")`.

    Refusing an embedded separator is the behaviour ONLY the guarded writer
    has, so it distinguishes the two implementations rather than trusting that
    the right one is bound.
    """
    from no_human.config import AuthError

    env = tmp_path / ".env"
    nh_home = tmp_path / ".no_human"
    nh_home.mkdir(mode=0o700)
    with (
        patch("no_human.cli.init_cmd.ENV_PATH", env),
        patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home),
    ):
        _append_env("CLAUDE_CODE_OAUTH_TOKEN", "good-token")
        assert (env.stat().st_mode & 0o777) == 0o600

        # U+2028 is a line break to `splitlines()` but not to `split("\n")`:
        # the old writer stored it happily and the reader then saw a forged
        # second line. The guarded writer refuses it.
        for sep in ("\u2028", "\n", "\x85", "\x00"):
            with pytest.raises(AuthError):
                _append_env("CLAUDE_CODE_OAUTH_TOKEN",
                            f"tok{sep}ANTHROPIC_API_KEY=sk-planted")

    # The refused writes changed nothing.
    assert env.read_text() == "CLAUDE_CODE_OAUTH_TOKEN=good-token\n"


def _init_env(tmp_path, monkeypatch, *, with_token: bool = False):
    """A `nh init` run wired to tmp dirs: no real HOME, no real credential."""
    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    config_path = nh_home / "config.yaml"
    config_path.write_text("llm:\n  auth_mode: subscription\n")
    env_path = nh_home / ".env"
    if with_token:
        env_path.write_text("CLAUDE_CODE_OAUTH_TOKEN=test_token\n")
        env_path.chmod(0o600)

    import no_human.cli.init_cmd as init_mod

    monkeypatch.setattr(init_mod, "NO_HUMAN_HOME", nh_home)
    monkeypatch.setattr(init_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)
    monkeypatch.setattr(init_mod, "load_config", lambda **kw: None)
    return env_path


def test_nh_init_refuses_an_api_key_pasted_into_the_oauth_field(tmp_path, monkeypatch):
    """`nh init` accepted ANY string and printed `Auth: ✓ ready`.

    Onboarding walkthrough 2026-08-09, finding B4/B4b: `sk-ant-api03-…` typed
    at the subscription-token prompt was written verbatim into
    CLAUDE_CODE_OAUTH_TOKEN — while `PUT /api/auth/token` and the desktop app
    both refused exactly that. The CLI now goes through the same validator, so
    the bad value is never written and the user is told which option they want.
    """
    env_path = _init_env(tmp_path, monkeypatch)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["init"],
            input="1\nsk-ant-api03-FAKEFAKEFAKE\nn\n",
            catch_exceptions=False,
        )
        # And it did not smuggle itself into the process env for this run.
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ

    # Nothing written: no .env at all, and certainly not the key.
    assert not env_path.exists()
    # Told what it looks like they pasted, and where that credential belongs.
    assert "not an OAuth token" in result.output
    assert "auth_mode: api_key" in result.output
    # The summary must not claim readiness.
    assert "not set" in result.output


def test_nh_init_accepts_a_valid_oauth_token(tmp_path, monkeypatch):
    """The valid path is unchanged: the token is written at 0600 and reported
    ready. Guards the fix against being over-strict — only the demonstrated bad
    shapes are refused, never a plausible OAuth token."""
    env_path = _init_env(tmp_path, monkeypatch)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["init"],
            input="1\nsk-ant-oat01-VALIDLOOKINGTOKEN\nn\n",
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert env_path.read_text() == (
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-VALIDLOOKINGTOKEN\n"
    )
    assert (env_path.stat().st_mode & 0o777) == 0o600
    assert "ready" in result.output


def test_save_subscription_token_refuses_what_cannot_be_a_credential(tmp_path):
    """Empty and whitespace-only never reach the .env — the validator's own
    rule, not a second opinion about token shapes."""
    from no_human.cli.init_cmd import _save_subscription_token

    env = tmp_path / ".env"
    nh_home = tmp_path / ".no_human"
    nh_home.mkdir(mode=0o700)
    with (
        patch("no_human.cli.init_cmd.ENV_PATH", env),
        patch("no_human.cli.init_cmd.NO_HUMAN_HOME", nh_home),
    ):
        for bad in ("", "   ", "\t\n "):
            assert _save_subscription_token(bad) is False
        assert not env.exists()
        assert _save_subscription_token("plausible-oauth-token") is True
    assert "CLAUDE_CODE_OAUTH_TOKEN=plausible-oauth-token" in env.read_text()


# --------------------------------------------------------------------------- #
# The prerequisite table actually compares the version it captured (B12)       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version,minimum,too_old", [
    ("Python 3.10.11", (3, 12), True),
    ("Python 3.12.0", (3, 12), False),
    ("Python 3.13.1", (3, 12), False),
    ("Python 3.12", (3, 12), False),
    ("Python 4.0.0", (3, 12), False),
    ("git version 2.49.0", None, False),      # no requirement stated
    ("some tool vNEXT", (3, 12), False),      # unparseable is not evidence
])
def test_version_too_old_compares_what_check_tool_captured(version, minimum, too_old):
    """`_check_tool` captured the version string and nothing ever read it, so
    `nh init` printed a green tick for a Python it knew was too old
    (walkthrough B12). An UNPARSEABLE version must NOT fail: a format we don't
    recognise is not evidence of an old interpreter."""
    problem = _version_too_old(version, minimum)
    assert (problem is not None) is too_old, (version, minimum, problem)
    if too_old:
        assert "3.12+" in problem, problem


def test_a_required_tool_below_its_minimum_is_an_error_naming_the_requirement(
        monkeypatch, capsys):
    """The general grading rule: a required tool that resolves but is older
    than the stated minimum fails the check, and the message names the real
    requirement rather than green-ticking it."""
    import no_human.cli.init_cmd as init_mod

    monkeypatch.setattr(init_mod, "_PREREQUISITES",
                        [("git", "--version", "install git", (99, 0))])
    monkeypatch.setattr(init_mod, "_OPTIONAL", [])
    errors, warnings = check_prerequisites()

    assert errors, "a too-old required tool must fail the check"
    assert "99.0+" in errors[0], errors
    assert not warnings


def test_an_old_python3_on_PATH_is_reported_but_does_not_block_a_venv_install(
        monkeypatch, capsys):
    """The fix must not refuse a machine the product demonstrably runs on.

    `python3` on PATH being 3.10 while no_human runs under a 3.12 venv or
    `uv run` is the DOCUMENTED install, and it is exactly the machine the
    walkthrough was performed on. The old green tick was still a lie, so the
    line is downgraded and names the requirement — but it is a warning, not an
    error that would exit 1 on a working install.
    """
    import no_human.cli.init_cmd as init_mod

    monkeypatch.setattr(init_mod, "_OPTIONAL", [])
    monkeypatch.setattr(init_mod, "_check_tool",
                        lambda binary, flag: "Python 3.10.11")
    errors, warnings = check_prerequisites()
    out = capsys.readouterr().out

    assert not errors, errors
    assert any("3.12+" in w for w in warnings), warnings
    assert "3.12+ required" in out, out
    assert "✓ python3" not in out, out


# --------------------------------------------------------------------------- #
# A credential init FOUND is validated before it is called ready (B4b)         #
# --------------------------------------------------------------------------- #


def test_a_stored_token_that_cannot_work_is_not_reported_ready(tmp_path, monkeypatch):
    """`nh init` short-circuits on PRESENCE, so a pre-hardening install with an
    API key in CLAUDE_CODE_OAUTH_TOKEN got `✓ found` and `Auth: ✓ ready` from
    an install that cannot make a single call (init-fix review advisory 5).

    Validate-only: the stored value is reported, never rewritten or deleted —
    a re-run of init destroys nothing.
    """
    env_path = _init_env(tmp_path, monkeypatch)
    env_path.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-api03-STOREDBYAPRIORINSTALL\n")
    env_path.chmod(0o600)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        result = CliRunner().invoke(cli, ["init"], input="n\n",
                                    catch_exceptions=False)

    assert "invalid" in result.output, result.output
    assert "not an OAuth token" in result.output, result.output
    assert "not set" in result.output, "the summary card still claimed readiness"
    assert "✓ ready" not in result.output, result.output
    # Untouched: init reports, it does not repair someone else's credential.
    assert env_path.read_text() == (
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-api03-STOREDBYAPRIORINSTALL\n")


def test_a_bad_token_inherited_from_the_shell_is_not_reported_ready(
        tmp_path, monkeypatch):
    """The other found-route: nothing in .env, but the variable is exported in
    the caller's shell. Same validator, same refusal — and still no write."""
    env_path = _init_env(tmp_path, monkeypatch)

    with patch.dict(os.environ,
                    {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-api03-FROMTHESHELL"},
                    clear=False):
        result = CliRunner().invoke(cli, ["init"], input="n\n",
                                    catch_exceptions=False)

    assert "invalid" in result.output, result.output
    assert "not set" in result.output, result.output
    assert not env_path.exists(), "a validate-only check must write nothing"


def test_a_usable_stored_token_is_still_reported_ready(tmp_path, monkeypatch):
    """The control: validation must not turn a working install into a broken
    one. A plausible OAuth token found in .env is still `✓ ready`."""
    env_path = _init_env(tmp_path, monkeypatch, with_token=True)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        result = CliRunner().invoke(cli, ["init"], input="n\n",
                                    catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "ready" in result.output, result.output
    assert "invalid" not in result.output, result.output
    assert env_path.read_text() == "CLAUDE_CODE_OAUTH_TOKEN=test_token\n"


def test_the_token_prompt_does_not_echo_the_credential(tmp_path, monkeypatch):
    """The OAuth prompt used to echo the token to the terminal and into
    scrollback (walkthrough B13) while the API-key prompt two functions up
    already hid it. Same class of secret, same treatment."""
    _init_env(tmp_path, monkeypatch)
    secret = "sk-ant-oat01-SECRETVALUETHATMUSTNOTAPPEAR"

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        result = CliRunner().invoke(cli, ["init"], input=f"1\n{secret}\nn\n",
                                    catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "saved" in result.output, result.output
    assert secret not in result.output, (
        "the token was echoed back to the terminal:\n" + result.output)


def test_no_module_defines_the_same_top_level_name_twice():
    """The general form of the bug above, which no test could see.

    A duplicated definition is silent: the file imports, the suite passes, and
    the LAST definition wins — so a security fix can be present in the source,
    reviewed, and still dead. Cheap to check across the whole package.
    """
    import ast
    import pathlib
    from collections import Counter

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "no_human"
    offenders = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text())
        names = Counter(
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        offenders += [f"{py.relative_to(src)}:{n} defined {c}x"
                      for n, c in names.items() if c > 1]
    assert not offenders, offenders
