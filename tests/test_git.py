"""GitRepo unit tests."""

import subprocess

import pytest

from no_human.vcs import GitError, GitRepo


def _rev_count(path) -> str:
    return subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=path,
                          check=True, capture_output=True, text=True).stdout.strip()


def _git_repo(path) -> GitRepo:
    """A minimal real repo with one commit, checked out on a non-protected
    branch (`commit_all` refuses to commit on `main`/`master`/`release/*`) —
    the shared fixture for the bypass_gate contract tests below.

    The bootstrap commit is made via raw `git commit`, not `GitRepo.commit_all`:
    on a brand-new repo HEAD is unborn (no commit yet), and `commit_all` calls
    `current_branch()` (`git rev-parse --abbrev-ref HEAD`), which git itself
    refuses to resolve before the first commit exists. Making that first
    commit directly sidesteps the chicken-and-egg case; every commit after it
    goes through the real `GitRepo` API being tested.
    """
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "a.txt").write_text("1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=no_human",
         "-c", "user.email=no-human@acme.com", "commit", "-m", "init"],
        check=True,
    )
    repo = GitRepo(path)
    repo.create_branch("wip/git-test")
    return repo


def test_bypass_gate_is_refused_for_a_non_wip_commit_message(tmp_path):
    """AC2: `bypass_gate` is the ONE contract that lets a commit skip the
    pre-commit gate — it exists solely for a `[WIP-*]` checkpoint, which
    ships nothing. Any other message must be refused, and refused BEFORE any
    commit is created."""
    repo = _git_repo(tmp_path)
    before = _rev_count(tmp_path)

    (tmp_path / "a.txt").write_text("2\n")
    with pytest.raises(GitError):
        repo.commit_all("feat: ship it", bypass_gate=True)

    assert _rev_count(tmp_path) == before, (
        "a refused bypass_gate call must not create a commit")


def test_bypass_gate_skips_the_pre_commit_hook_only_for_wip(tmp_path):
    """AC2: a real pre-commit hook that always refuses must still block the
    normal (non-bypass) path — proving the hook is actually wired up — while
    a `[WIP-*]` commit with `bypass_gate=True` skips it via `--no-verify`."""
    repo = _git_repo(tmp_path)
    hooks_dir = tmp_path / ".git" / "hooks"
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    (tmp_path / "a.txt").write_text("3\n")
    with pytest.raises(GitError):
        repo.commit_all("feat: normal change")

    (tmp_path / "a.txt").write_text("4\n")
    commit = repo.commit_all("[WIP-BLOCKED] x", bypass_gate=True)
    assert commit.sha, "the bypassed WIP commit must succeed despite the hook"
    assert repo.head_sha() == commit.sha


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


def _branch(tmp_path) -> GitRepo:
    """A real two-branch repo (`main` + `feat/x`) for `numstat`/`commit_
    subjects` pins — reuses `_git_repo`'s bootstrap-commit trick."""
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-qb", "feat/x"],
                    check=True)
    return repo


def test_numstat_reports_a_binary_file_as_0_0_but_still_lists_it(tmp_path):
    repo = _branch(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    repo.commit_all("add a binary file")

    stats = repo.numstat("main")

    assert ("blob.bin", 0, 0) in stats


def test_numstat_strips_only_the_surrounding_quotes_of_a_quoted_path(tmp_path):
    """Git's default `core.quotepath` wraps a non-ASCII path in quotes AND
    octal-escapes each non-ASCII byte (`"caf\\303\\251.txt"` for `café.txt`).
    `numstat` strips only the wrapping quotes — it must NOT try to decode the
    octal escapes itself, or it risks mangling a path git didn't actually
    have."""
    repo = _branch(tmp_path)
    (tmp_path / "café.txt").write_text("bonjour\n")
    repo.commit_all("add a non-ascii filename")

    stats = repo.numstat("main")
    paths = [p for p, _, _ in stats]

    assert paths == [r"caf\303\251.txt"]
    assert not any(p.startswith('"') or p.endswith('"') for p in paths)


def test_numstat_and_commit_subjects_are_empty_for_an_unresolvable_base(tmp_path):
    repo = _branch(tmp_path)
    (tmp_path / "a.txt").write_text("2\n")
    repo.commit_all("touch a.txt")

    assert repo.numstat("no-such-branch") == []
    assert repo.commit_subjects("no-such-branch") == []
