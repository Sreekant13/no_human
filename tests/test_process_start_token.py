"""Pool-lease pid-reuse fix: `config.process_start_token` and its three
per-platform backends. See `core.scheduler._lease_sibling_is_dead` for the
consumer and `tests/test_status_clobber.py` for the lease-level behaviour
tests — this file is scoped to the token functions themselves.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

from no_human.config import (
    _linux_start_token,
    _macos_start_token,
    _parse_linux_stat_starttime,
    _win_start_token_from_kernel32,
    _windows_start_token,
    process_start_token,
)


# --------------------------------------------------------------------------- #
# Linux: pure-parser test, runs on any host (fixture text, no real /proc)      #
# --------------------------------------------------------------------------- #

def test_parse_linux_stat_starttime_handles_a_comm_with_spaces_and_parens():
    """The `comm` field (2nd, parenthesized) is not attacker-controlled here,
    but it IS user-controlled (`prctl(PR_SET_NAME)` / argv[0] tricks) and can
    contain spaces and `)` characters — e.g. a process named
    `my (weird) comm name`. The only safe split is from the LAST `)` in the
    line; splitting from the first would misparse `state` and every field
    after it, including `starttime` (field 22 overall, index 19 after the
    split)."""
    # pid=4242, comm="my (weird) comm name", then state=R, ppid=1, pgrp=4242,
    # session=4242, tty_nr=0, tpgid=-1, flags=0 (7 fields, indices 0-6 after
    # the split), then minflt..itrealvalue (12 filler fields, indices 7-18),
    # then starttime at index 19 (field 22 overall, per `/proc/<pid>/stat`).
    fixed_fields = ["R", "1", "4242", "4242", "0", "-1", "0"]  # indices 0-6
    filler = ["0"] * 12  # indices 7-18
    starttime = "123456789"  # index 19
    trailing = ["0"] * 5  # a few more fields after starttime, as real /proc has
    content = (
        "4242 (my (weird) comm name) "
        + " ".join(fixed_fields + filler + [starttime] + trailing)
    )

    assert _parse_linux_stat_starttime(content) == starttime


def test_parse_linux_stat_starttime_returns_none_when_too_short():
    assert _parse_linux_stat_starttime("4242 (sh) R 1") is None


def test_parse_linux_stat_starttime_returns_none_with_no_closing_paren():
    assert _parse_linux_stat_starttime("garbage with no paren") is None


def test_linux_start_token_returns_none_for_a_dead_or_unreadable_pid():
    # PID 1 (init/launchd) is not readable as /proc on macOS at all (no
    # /proc filesystem), and an absurd pid is never a real /proc entry on
    # Linux either — either way this must return None, never raise.
    assert _linux_start_token(999999999) is None


# --------------------------------------------------------------------------- #
# macOS: proven LIVE with a real pid, on this actual development machine      #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only backend")
def test_macos_start_token_is_live_and_stable_for_this_real_process():
    """Proof-of-life on the real backend, not a mock: two reads of THIS
    process's own live pid must agree (the token is stable across reads of
    the same still-running process), and it must be non-None."""
    my_pid = os.getpid()
    first = _macos_start_token(my_pid)
    second = _macos_start_token(my_pid)
    assert first is not None
    assert first == second


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only backend")
def test_macos_start_token_none_for_an_implausible_pid():
    assert _macos_start_token(999999999) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only backend")
def test_process_start_token_dispatches_to_macos_on_this_host():
    assert process_start_token(os.getpid()) == _macos_start_token(os.getpid())


# --------------------------------------------------------------------------- #
# Windows: unit-tested with a mocked handle (this dev machine is not win32)   #
# --------------------------------------------------------------------------- #

def _real_wintypes_mod():
    """The REAL `ctypes.wintypes` module — it is importable on any platform
    (only `ctypes.windll`, the DLL loader, is Windows-only; verified via
    `hasattr(ctypes, 'windll')` is False on this macOS host). Only `kernel32`
    is mocked below: `wintypes.FILETIME` must stay a genuine ctypes Structure
    so `ctypes.byref(...)` inside `_win_start_token_from_kernel32` accepts it
    — a plain Python stand-in raises `TypeError: byref() argument must be a
    ctypes instance` before the mock ever gets a chance to run."""
    from ctypes import wintypes
    return wintypes


def test_win_start_token_from_kernel32_reads_the_creation_filetime():
    wintypes_mod = _real_wintypes_mod()
    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = 0xDEAD

    def _fake_get_process_times(handle, creation_ref, exit_ref, kernel_ref, user_ref):
        creation_ref._obj.dwLowDateTime = 0x1234
        creation_ref._obj.dwHighDateTime = 0x5678
        return True

    kernel32.GetProcessTimes.side_effect = _fake_get_process_times

    token = _win_start_token_from_kernel32(kernel32, wintypes_mod, 4242)

    assert token == "0000567800001234"
    kernel32.OpenProcess.assert_called_once()
    kernel32.CloseHandle.assert_called_once_with(0xDEAD)


def test_win_start_token_from_kernel32_returns_none_when_open_process_fails():
    wintypes_mod = _real_wintypes_mod()
    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = 0  # NULL handle — no such pid

    assert _win_start_token_from_kernel32(kernel32, wintypes_mod, 999999999) is None
    kernel32.GetProcessTimes.assert_not_called()
    kernel32.CloseHandle.assert_not_called()


def test_win_start_token_from_kernel32_returns_none_when_get_process_times_fails():
    wintypes_mod = _real_wintypes_mod()
    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = 0xDEAD
    kernel32.GetProcessTimes.return_value = False

    assert _win_start_token_from_kernel32(kernel32, wintypes_mod, 4242) is None
    kernel32.CloseHandle.assert_called_once_with(0xDEAD)


def test_windows_start_token_fails_soft_off_win32(monkeypatch):
    """On any non-`win32` host (this dev machine included) `_windows_start_token`
    must return None without touching `ctypes.windll` at all — never raise."""
    assert sys.platform != "win32"
    assert _windows_start_token(os.getpid()) is None


def test_windows_start_token_fails_soft_when_kernel32_raises(monkeypatch):
    """Even if somehow reached on win32, any exception from the real
    `ctypes.windll.kernel32` path must be swallowed — fail soft to legacy
    behaviour, never crash the lease claim."""
    import no_human.config as config_mod

    monkeypatch.setattr(config_mod.sys, "platform", "win32")

    class _BoomWindll:
        @property
        def kernel32(self):
            raise RuntimeError("no kernel32 on this host")

    monkeypatch.setattr(config_mod.ctypes, "windll", _BoomWindll(), raising=False)

    assert _windows_start_token(4242) is None


def test_process_start_token_returns_none_on_an_unrecognised_platform(monkeypatch):
    import no_human.config as config_mod

    monkeypatch.setattr(config_mod.sys, "platform", "plan9")

    assert process_start_token(os.getpid()) is None
