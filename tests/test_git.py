"""GitRepo unit tests."""

import subprocess

from no_human.vcs import GitRepo

def test_default_branch_local_only_never_touches_the_network(tmp_path, monkeypatch):
    """PR-001's GUI half needs the default branch while a person types in the
    composer. `default_branch()` falls back to `git remote show origin`, which is
    NETWORK I/O — in a request handler that is a timeout, not an answer, and it
    would hang the board offline. `local_only=True` must stop at the local ref.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    repo = GitRepo(tmp_path)
    calls: list[tuple] = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return ""  # no origin/HEAD locally -> the fallback would fire

    monkeypatch.setattr(repo, "_run", fake_run)

    assert repo.default_branch(local_only=True) == ""
    assert not any("remote" in a for a in calls), (
        f"local_only must not run a network git command; ran: {calls}"
    )
    # Positive control: WITHOUT local_only the network fallback IS attempted, so
    # the assertion above is testing the flag and not a selector that never matches.
    calls.clear()
    repo.default_branch()
    assert any("remote" in a for a in calls), (
        "the network fallback should still exist for non-request callers"
    )
