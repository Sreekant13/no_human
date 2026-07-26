"""Tests for scripts/bootstrap.sh: dry-run shape, config preservation,
missing-tool messaging, and idempotency. Shells out to the real script with a
hermetic fake PATH + temp HOME so nothing touches the real ~/.no_human/."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bootstrap.sh"
TEMPLATE = REPO_ROOT / "scripts" / "config.yaml.template"
# NOTE: the script always `cd`s to the repo root before doing anything, so
# any .venv it touches lands at REPO_ROOT/.venv — NOT under the fake HOME
# used by fake_env. This repo checkout already has a real .venv (created by
# `uv sync`/`uv run` outside of these tests), so its mere *existence* can't
# be asserted on; step-banner absence ("[2/5]" not in stdout) is what proves
# the venv-touching step never ran.


def _write_stub(bin_dir: Path, name: str, output: str = "") -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_env(tmp_path):
    """A temp bin/ with stub python3/uv/claude/nh, plus isolated HOME."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "python3", "Python 3.12.4")
    _write_stub(bin_dir, "uv", "uv 0.4.0")
    _write_stub(bin_dir, "claude", "1.0.0")
    _write_stub(bin_dir, "nh", "nh doctor: ok")

    home = tmp_path / "home"
    home.mkdir()
    nh_home = home / ".no_human"

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "NO_HUMAN_HOME": str(nh_home),
    }
    return {"env": env, "bin_dir": bin_dir, "home": home, "nh_home": nh_home}


def _run(fake_env, extra_args=None):
    args = [str(SCRIPT)] + (extra_args or [])
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, env=fake_env["env"],
        cwd=str(REPO_ROOT),
    )


def test_dry_run_output_shape(fake_env):
    result = _run(fake_env, ["--dry-run"])
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for n in range(1, 6):
        assert f"[{n}/5]" in out, f"missing step banner [{n}/5]:\n{out}"
    order = [out.index(f"[{n}/5]") for n in range(1, 6)]
    assert order == sorted(order), "step banners out of order"
    assert "DRY-RUN" in out
    assert not fake_env["nh_home"].exists(), "--dry-run must not create ~/.no_human"


def test_dry_run_with_existing_config_does_not_touch_it(fake_env):
    fake_env["nh_home"].mkdir(parents=True)
    config_path = fake_env["nh_home"] / "config.yaml"
    sentinel = "# sentinel: do not touch\nfoo: bar\n"
    config_path.write_text(sentinel)

    result = _run(fake_env, ["--dry-run"])
    assert result.returncode == 0, result.stderr
    assert config_path.read_text() == sentinel
    assert "leaving untouched" in result.stdout
    assert "DRY-RUN: would run: cp" not in result.stdout


def test_existing_config_untouched(fake_env):
    fake_env["nh_home"].mkdir(parents=True)
    config_path = fake_env["nh_home"] / "config.yaml"
    sentinel = "# sentinel: do not touch\nfoo: bar\n"
    config_path.write_text(sentinel)

    result = _run(fake_env)
    assert result.returncode == 0, result.stderr
    assert config_path.read_text() == sentinel
    assert "leaving untouched" in result.stdout


def test_template_created_when_absent(fake_env):
    result = _run(fake_env)
    assert result.returncode == 0, result.stderr
    config_path = fake_env["nh_home"] / "config.yaml"
    assert config_path.exists()
    assert config_path.read_text() == TEMPLATE.read_text()


def test_missing_uv_prints_instructions_and_exits_nonzero(fake_env):
    (fake_env["bin_dir"] / "uv").unlink()
    result = _run(fake_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "astral.sh/uv/install.sh" in result.stdout
    assert "[2/5]" not in result.stdout


def test_missing_python_without_uv_prints_instructions_and_exits_nonzero(fake_env):
    (fake_env["bin_dir"] / "python3").unlink()
    (fake_env["bin_dir"] / "uv").unlink()
    result = _run(fake_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "python.org" in result.stdout or "brew install python@3.12" in result.stdout
    assert "[2/5]" not in result.stdout


def test_old_system_python_with_uv_available_proceeds(fake_env):
    # Stock-Mac case: system python3 is 3.10, but uv is present and can find
    # or provision 3.12 — the script must NOT refuse (send-back finding #2).
    _write_stub(fake_env["bin_dir"], "python3", "Python 3.10.9")
    result = _run(fake_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3.10" in result.stdout
    assert "[2/5]" in result.stdout
    assert "[5/5]" in result.stdout
    assert "Next steps:" in result.stdout


def test_missing_claude_cli_prints_pointer_and_exits_nonzero(fake_env):
    (fake_env["bin_dir"] / "claude").unlink()
    result = _run(fake_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "npm install -g @anthropic-ai/claude-code" in result.stdout
    assert "claude.com/claude-code" in result.stdout
    assert "[4/5]" not in result.stdout


def test_idempotent_rerun(fake_env):
    first = _run(fake_env)
    assert first.returncode == 0, first.stderr
    config_path = fake_env["nh_home"] / "config.yaml"
    first_bytes = config_path.read_bytes()

    second = _run(fake_env)
    assert second.returncode == 0, second.stderr
    assert "leaving untouched" in second.stdout
    assert config_path.read_bytes() == first_bytes
