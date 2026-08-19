"""Regression coverage for the Bash-created-non-code-file blind spot.

`commit_paths` used to stage untracked files only when their extension was
in `_CODE_EXTS` — so a file a Bash command wrote (a generated report, a
Dockerfile) with no code extension was silently never staged, and the
`uncommitted_source_files` guard that exists to catch exactly this shared
the same two predicates (`_CODE_EXTS` + edit-hook `coder_touched`), so it
never flagged it either. See `src/no_human/vcs/git.py`'s `commit_paths` and
`uncommitted_source_files` docstrings for the fix and its discriminator.
"""

import subprocess

import pytest

from no_human.vcs import GitRepo


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_bare_remote(tmp_path):
    """A work repo with one commit on `main`, wired to a local bare remote."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _committed_files(repo_path):
    return subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    ).stdout


def test_bash_created_md_beside_an_edit_created_py_is_committed(repo_with_bare_remote):
    """Verbatim repro from the escalated task (aaf752ad, 18.3M tokens over 2
    attempts): an edit-tool-created harness.py explicitly passed to
    commit_paths, plus a Bash-shaped REPORT.md written straight to disk
    (never in the paths list) beside it in a brand-new directory. Both must
    land in the commit."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/impossible-bench", base="main")
    bench = repo.path / "eval" / "newtool"
    bench.mkdir(parents=True)
    (bench / "harness.py").write_text("print('ran')\n")           # edit-tool-tracked
    (bench / "REPORT.md").write_text("| class | caught |\n")     # Bash-created, non-code
    repo.commit_paths([str(bench / "harness.py")], "add the eval harness")
    files = _committed_files(repo.path)
    assert "eval/newtool/harness.py" in files
    assert "eval/newtool/REPORT.md" in files


def test_side_effect_json_in_existing_dir_is_not_committed_and_not_flagged(repo_with_bare_remote):
    """NEGATIVE CONTROL — must not regress. A genuine test side-effect
    (alert-state.json written into a directory that already existed before
    this commit) must stay uncommitted AND unflagged by the guard, even
    though its directory now also holds a file the commit IS staging."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/side-effect", base="main")
    teams = repo.path / "teams" / "test"
    teams.mkdir(parents=True)
    (teams / "existing.py").write_text("x = 1\n")
    repo.commit_all("prior: add teams/test dir")
    # Now, in a later commit, the coder edits existing.py and the test suite
    # (run as a side-effect of verification) drops alert-state.json next to it.
    (teams / "existing.py").write_text("x = 2\n")
    (teams / "alert-state.json").write_text('{"state": "updated"}')
    repo.commit_paths([str(teams / "existing.py")], "PROJ-1: tweak existing.py")
    files = _committed_files(repo.path)
    assert "teams/test/existing.py" in files
    assert "alert-state.json" not in files
    leftover = repo.uncommitted_source_files(coder_touched={"teams/test/existing.py"})
    assert "teams/test/alert-state.json" not in leftover


def test_side_effect_json_in_a_new_dir_with_no_staged_sibling_is_not_committed(repo_with_bare_remote):
    """Second half of the discriminator: a new directory alone is not
    enough — the commit must also be staging something else into it, or the
    lone file is presumed a side-effect (mirrors the pinned
    test_commit_paths_excludes_untracked_json_side_effects in test_vcs.py)."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/lone-side-effect", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    data = repo.path / "data"
    data.mkdir()
    (data / "state.json").write_text('{"updated": true}')
    repo.commit_paths([str(repo.path / "feature.py")], "PROJ-1: add feature")
    files = _committed_files(repo.path)
    assert "feature.py" in files
    assert "state.json" not in files


def test_extensionless_deliverable_in_a_new_dir_is_committed(repo_with_bare_remote):
    """Suffix-agnostic coverage (AC-6): extensionless deliverables
    (Dockerfile, Makefile) in a brand-new directory land alongside a staged
    sibling, even though neither carries an extension in _CODE_EXTS."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/deploy", base="main")
    deploy = repo.path / "deploy"
    deploy.mkdir()
    (deploy / "entrypoint.sh").write_text("#!/bin/sh\necho hi\n")
    (deploy / "Dockerfile").write_text("FROM scratch\n")
    (deploy / "Makefile").write_text("build:\n\techo building\n")
    repo.commit_paths([str(deploy / "entrypoint.sh")], "add deploy scaffolding")
    files = _committed_files(repo.path)
    assert "deploy/entrypoint.sh" in files
    assert "deploy/Dockerfile" in files
    assert "deploy/Makefile" in files


def test_guard_flags_a_leftover_in_a_directory_the_commit_created(repo_with_bare_remote):
    """The guard's third predicate, exercised directly, WITHOUT coder_touched
    and without a _CODE_EXTS-matching suffix — proving it no longer depends
    on either of the two predicates commit_paths itself uses."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/guard-new-dir", base="main")
    x = repo.path / "eval" / "x"
    x.mkdir(parents=True)
    (x / "harness.py").write_text("print('ran')\n")
    (x / "REPORT.md").write_text("| class | caught |\n")
    # Hand-stage only harness.py, bypassing commit_paths entirely — simulates
    # a partial commit made through some other path.
    subprocess.run(["git", "add", "eval/x/harness.py"], cwd=repo.path,
                    check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "partial"], cwd=repo.path,
                    check=True, capture_output=True)
    leftover = repo.uncommitted_source_files(coder_touched=None)
    assert "eval/x/REPORT.md" in leftover


def test_unborn_repo_degrades_to_old_behaviour_without_crashing(tmp_path):
    """No HEAD at all yet (truly unborn repo — `current_branch()` itself
    requires a commit to exist, a pre-existing constraint, so the only way
    to reach this path is to call the new helpers directly, as
    `commit_paths`/`uncommitted_source_files` do internally before any
    commit exists on the branch). Both helpers must degrade to the old
    conservative (exclude / not-new) behaviour rather than raise."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    repo = GitRepo(work)
    assert repo._dir_absent_from_tree("eval/newtool", "HEAD") is False
    assert repo._dirs_newly_added_by_head() == set()


def test_root_commit_with_no_head_caret_still_flags_a_leftover(repo_with_bare_remote):
    """The guard's root-commit branch (no HEAD^ to diff against): when HEAD
    IS the repo's first-ever commit, every directory it touched counts as
    newly added, so a leftover left behind in that same directory (by a
    hand-crafted commit bypassing commit_paths, mirroring
    test_guard_flags_a_leftover_in_a_directory_the_commit_created) is still
    caught rather than silently passed through."""
    bare = repo_with_bare_remote.parent / "root.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                    capture_output=True)
    work = repo_with_bare_remote.parent / "root_work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    x = work / "eval" / "x"
    x.mkdir(parents=True)
    (x / "harness.py").write_text("print('ran')\n")
    (x / "REPORT.md").write_text("| class | caught |\n")
    _git(work, "add", "eval/x/harness.py")
    _git(work, "commit", "-m", "first commit ever, only harness.py")
    repo = GitRepo(work)
    leftover = repo.uncommitted_source_files()
    assert "eval/x/REPORT.md" in leftover
