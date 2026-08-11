"""`nh serve` must take the pid lock so CLI runners can see it.

`nh serve` binds no socket, so the HTTP probe `_server_owns_worker` uses to
detect `nh start` cannot see it — `nh task new --run`, `nh reply --run` and
`nh watch` would all drive a second orchestrator beside serve's scheduler
over the same checkout. The fix has two parts: `_server_owns_worker` falls
back to the pid lock file when the HTTP probe fails, and `serve()` takes that
same lock (mirroring `nh start`) so the fallback has something to read.

Every test here isolates `NO_HUMAN_HOME` to an empty `tmp_path` dir — the
pidfile fallback means an unpatched `NO_HUMAN_HOME` couples the test's result
to whether the developer's own `nh start`/`nh serve` happens to be running.
"""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

import no_human.cli.commands as cmd_mod
import no_human.config as config_mod
from no_human.cli.commands import cli


class _Cfg:
    """Minimal `nh serve` config: pool switched on, one worker."""

    primary_model = "claude-sonnet-4-6"
    review_model = "claude-sonnet-4-6"
    data = {
        "server": {"host": "127.0.0.1", "port": 1},  # port 1 never listens
        "concurrency": {"enabled": True, "max_workers": 1},
        "integrations": {"jira": {"enabled": False},
                          "linear": {"enabled": False}},
    }
    db_path = Path(":memory:")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


def _isolate_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setattr(config_mod, "NO_HUMAN_HOME", home)
    return home


# --------------------------------------------------------------------------- #
# _server_owns_worker pidfile fallback                                        #
# --------------------------------------------------------------------------- #


def test_no_server_and_no_pidfile_is_false(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)

    assert not (home / "nh.pid").exists()
    assert cmd_mod._server_owns_worker(_Cfg()) is False


def test_live_pidfile_beats_a_failing_http_probe(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    home.mkdir(parents=True)
    pidfile = home / "nh.pid"
    pidfile.write_text(str(os.getpid()))  # our own pid: guaranteed alive

    assert cmd_mod._server_owns_worker(_Cfg()) is True

    # Control: remove the pidfile and re-assert False in the same test, so the
    # pidfile — not some other difference — is what flipped the result.
    pidfile.unlink()
    assert cmd_mod._server_owns_worker(_Cfg()) is False


def test_dead_pid_in_pidfile_is_false(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    home.mkdir(parents=True)
    pidfile = home / "nh.pid"

    monkeypatch.setattr(cmd_mod, "_probe_pid", lambda pid: False)
    pidfile.write_text("424242")
    assert cmd_mod._server_owns_worker(_Cfg()) is False

    pidfile.write_text("not-a-pid")
    assert cmd_mod._server_owns_worker(_Cfg()) is False

    pidfile.write_text("")
    assert cmd_mod._server_owns_worker(_Cfg()) is False


def test_another_users_pid_in_pidfile_is_false(tmp_path, monkeypatch):
    """`_probe_pid` is tri-state (True/False/None-for-another-user's-pid); the
    fallback treats None as False, unlike `_acquire_pid_lock` where None means
    "not ours, take the lock" — the safe answer for a *probe* is "assume no
    server", not "assume it's ours"."""
    home = _isolate_home(monkeypatch, tmp_path)
    home.mkdir(parents=True)
    pidfile = home / "nh.pid"
    pidfile.write_text("1")

    monkeypatch.setattr(cmd_mod, "_probe_pid", lambda pid: None)
    assert cmd_mod._server_owns_worker(_Cfg()) is False


# --------------------------------------------------------------------------- #
# serve() takes the same lock nh start does                                   #
# --------------------------------------------------------------------------- #


def _patch_serve_prelude(monkeypatch, cfg):
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(cmd_mod, "_assert_backend_usable", lambda: None)


def test_serve_refuses_when_the_lock_is_held(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    _patch_serve_prelude(monkeypatch, _Cfg())
    monkeypatch.setattr(cmd_mod, "_acquire_pid_lock", lambda: False)

    reached = []

    def _fail_if_reached(coro):
        coro.close()
        reached.append(True)

    monkeypatch.setattr(cmd_mod.asyncio, "run", _fail_if_reached)

    result = CliRunner().invoke(cli, ["serve", "--max-workers", "1"])

    assert result.exit_code == 1, result.output
    assert "already running" in result.output
    assert reached == [], "serve ran the scheduler despite a held lock"


def test_serve_releases_the_lock_on_exit(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    _patch_serve_prelude(monkeypatch, _Cfg())
    # Real lock helpers: the whole point is proving `serve()` drives them.
    seen_during_run = []

    def _fake_run(coro):
        coro.close()
        seen_during_run.append((home / "nh.pid").exists())
        return 0

    monkeypatch.setattr(cmd_mod.asyncio, "run", _fake_run)

    result = CliRunner().invoke(cli, ["serve", "--max-workers", "1"])

    assert result.exit_code == 0, result.output
    assert seen_during_run == [True], "the pidfile must exist while the run is in flight"
    assert not (home / "nh.pid").exists(), "the pidfile must be gone after serve exits"


def test_serve_releases_the_lock_when_the_drain_raises(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    _patch_serve_prelude(monkeypatch, _Cfg())

    def _raising_run(coro):
        coro.close()
        raise RuntimeError("boom")

    monkeypatch.setattr(cmd_mod.asyncio, "run", _raising_run)

    result = CliRunner().invoke(cli, ["serve", "--max-workers", "1"])

    assert result.exit_code != 0
    assert not (home / "nh.pid").exists(), "the pidfile must be released even on a raise"


def test_serve_and_start_share_one_lock(tmp_path, monkeypatch):
    """The actual product claim: `nh serve` and `nh start` are mutually
    exclusive because they take the SAME lock file, not just "a function
    returns False" in isolation."""
    _isolate_home(monkeypatch, tmp_path)
    _patch_serve_prelude(monkeypatch, _Cfg())
    monkeypatch.setattr(cmd_mod.asyncio, "run", lambda coro: coro.close())

    assert cmd_mod._acquire_pid_lock() is True  # simulates `nh start` running
    try:
        result = CliRunner().invoke(cli, ["serve", "--max-workers", "1"])
        assert result.exit_code == 1, result.output
        assert "already running" in result.output
    finally:
        cmd_mod._release_pid_lock()
