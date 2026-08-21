"""The update-available notice must never touch stdout.

`nh status --json | jq` broke 8 tests across two tasks when 0.1.3 published
while branches still carried 0.1.2: `_schedule_update_notice` printed the
notice via `console.print(...)` unconditionally, appending it after the JSON
body on stdout. The fix (`src/no_human/cli/commands.py`) writes the notice to
**stderr** via `click.echo(..., err=True)` and suppresses it entirely when
stdout is not a TTY, or when the invoked command marked itself as emitting
machine output via the new `mark_machine_output()` helper.

Simulating an interactive TTY through `CliRunner` needs one piece of prior
art: `CliRunner`'s captured stdout is a `click.testing._NamedTextIOWrapper`
(a *Python* subclass of the C-implemented, attribute-immutable
`io.TextIOWrapper`), so `isatty` can be patched on the subclass but not on
`io.TextIOWrapper` itself. Left unpatched, `.isatty()` returns `False` by
default — the "piped" scenario for free.
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner, _NamedTextIOWrapper

import no_human.updates as updates
from no_human.cli.commands import cli
from no_human.core.task import TaskStatus

from tests.test_cli_commands import _make_runner, _seed_task

_NOTICE = "A new version of no_human is available: 9.9.9 (you have 0.1.4)"


@pytest.fixture
def _interactive_tty(monkeypatch):
    """Make every stream CliRunner hands the process under test report
    `isatty() -> True`, simulating a real terminal."""
    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)


def _stub_notice(monkeypatch, text: str | None = _NOTICE) -> None:
    """`_schedule_update_notice` does `from ..updates import check_for_update`
    at call time, so patching the source module's attribute is enough — no
    need to patch `no_human.cli.commands`."""
    monkeypatch.setattr(updates, "check_for_update", lambda *a, **kw: text)


# --------------------------------------------------------------------------- #
# AC1 — `--json` stdout stays pure JSON; the notice (if any) goes to stderr   #
# --------------------------------------------------------------------------- #

def test_status_json_stdout_is_pure_json_and_the_notice_goes_to_stderr(
        tmp_path, monkeypatch, _interactive_tty):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)
    _stub_notice(monkeypatch)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)  # must not raise — notice text can't be in here
    assert parsed["queued"] == 1
    assert "9.9.9" not in result.stdout
    assert _NOTICE not in result.stdout
    assert _NOTICE not in result.stderr, (
        "the command marked itself machine-output, so the notice is fully "
        "suppressed for this invocation — even on stderr, since a caller "
        "may still be piping interactively (`nh status --json | jq` on a "
        "real terminal)")


def test_status_human_readable_on_a_tty_still_shows_the_notice_on_stderr(
        tmp_path, monkeypatch, _interactive_tty):
    """Regression guard: `--json` absent, on a TTY, the notice must still
    appear — and only on stderr, never mixed into stdout."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)
    _stub_notice(monkeypatch)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert _NOTICE not in result.stdout
    assert _NOTICE in result.stderr


# --------------------------------------------------------------------------- #
# AC1 — piped stdout: no notice anywhere, not even on stderr                  #
# --------------------------------------------------------------------------- #

def test_no_notice_when_stdout_is_not_a_tty(tmp_path, monkeypatch):
    """No `_interactive_tty` fixture here — CliRunner's default captured
    stdout already reports `isatty() -> False`, matching `nh status | cat`."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)
    _stub_notice(monkeypatch)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert _NOTICE not in result.stdout
    assert _NOTICE not in result.stderr, (
        "piped/redirected stdout is never the right audience for an "
        "advisory line, even on stderr")


# --------------------------------------------------------------------------- #
# Marker unit test — wired for a SECOND command, proving it's generic         #
# --------------------------------------------------------------------------- #

def test_mark_machine_output_sets_the_root_context_flag(
        tmp_path, monkeypatch, _interactive_tty):
    db = tmp_path / "test.db"
    _stub_notice(monkeypatch)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["memories", "scan", "--json"])

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # must not raise
    assert _NOTICE not in result.stdout
    assert _NOTICE not in result.stderr, (
        "the marker must suppress the notice for a SECOND command too, not "
        "just `status` — proving the mechanism is generic")


# --------------------------------------------------------------------------- #
# AC2 — the suite itself must be hermetic w.r.t. PyPI                         #
# --------------------------------------------------------------------------- #

def test_the_suite_disables_the_update_check():
    """If `tests/conftest.py` stops setting `NH_NO_UPDATE_CHECK` at import
    time, this must fail — that is the whole point of the guard."""
    assert os.environ.get(updates.DISABLE_ENV_VAR), (
        "tests/conftest.py must set no_human.updates.DISABLE_ENV_VAR in "
        "os.environ at module scope so the suite never depends on what "
        "PyPI holds")
    assert updates.is_disabled() is True
