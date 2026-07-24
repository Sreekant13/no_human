"""The coding-backend readiness probe: a missing `claude` CLI must be loud.

The silent cliff this guards: nh start only requires an OAuth *token*, and nh
doctor is a HISTORICAL liveness check — neither noticed that the `claude` CLI
the Claude Agent SDK shells out to is absent. The board rendered green while
every task failed at launch. These pin the probe that closes the gap.
"""

from __future__ import annotations

from pathlib import Path

from no_human.agent import backend_check
from no_human.agent.backend_check import BackendStatus, check_backend, find_claude_cli


def test_find_cli_returns_a_path_when_on_PATH(monkeypatch):
    monkeypatch.setattr(backend_check.shutil, "which",
                        lambda name: "/usr/local/bin/claude" if name == "claude" else None)
    # No bundled CLI in the test SDK, no fallback files → PATH wins.
    monkeypatch.setattr(backend_check.Path, "is_file", lambda self: False)
    assert find_claude_cli() == "/usr/local/bin/claude"


def test_find_cli_returns_none_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(backend_check.shutil, "which", lambda name: None)
    monkeypatch.setattr(backend_check.Path, "is_file", lambda self: False)
    assert find_claude_cli() is None


def test_find_cli_uses_a_known_fallback_location(monkeypatch, tmp_path):
    fake = str(tmp_path / ".local" / "bin" / "claude")
    monkeypatch.setattr(backend_check.shutil, "which", lambda name: None)
    monkeypatch.setattr(backend_check.Path, "home", classmethod(lambda cls: tmp_path))
    # is_file True ONLY for the fake fallback path, so a real /usr/local/bin
    # /claude on the test machine can't shadow the assertion.
    monkeypatch.setattr(backend_check.Path, "is_file", lambda self: str(self) == fake)
    assert find_claude_cli() == fake


def test_check_backend_ready_when_cli_and_token_present(monkeypatch):
    monkeypatch.setattr(backend_check, "find_claude_cli", lambda: "/bin/claude")
    st = check_backend(token_present=True)
    assert isinstance(st, BackendStatus)
    assert st.ready
    assert st.reasons == []


def test_check_backend_flags_a_missing_cli(monkeypatch):
    monkeypatch.setattr(backend_check, "find_claude_cli", lambda: None)
    st = check_backend(token_present=True)
    assert not st.ready
    assert st.cli_path is None
    assert any("claude` CLI" in r for r in st.reasons)


def test_check_backend_flags_a_missing_token(monkeypatch):
    monkeypatch.setattr(backend_check, "find_claude_cli", lambda: "/bin/claude")
    st = check_backend(token_present=False)
    assert not st.ready
    assert any("OAuth token" in r for r in st.reasons)


def test_check_backend_reports_both_when_both_missing(monkeypatch):
    monkeypatch.setattr(backend_check, "find_claude_cli", lambda: None)
    st = check_backend(token_present=False)
    assert not st.ready
    assert len(st.reasons) == 2


def test_token_probe_never_raises_on_a_bad_profile(monkeypatch):
    # A malformed profile must degrade to "no token", not crash a readiness probe.
    st = check_backend(profile="!!!not a valid profile!!!")
    assert isinstance(st.token_present, bool)
