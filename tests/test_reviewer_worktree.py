"""Tests for the reviewer-worktree integrity guard (task d115e22f).

`reviewer_worktree.snapshot`/`compare` originally drove entirely off `git
status --porcelain`, which is structurally blind to the `.git` subtree — a
Bash-enabled reviewer session could plant an executable hook there invisibly
to that instrument, and `revert()`'s own git calls would then EXECUTE it.
These tests cover, against REAL git (no mocking: the whole claim is about
what git itself does with hooks in a linked worktree, which a mock would
assume away):

  - detection: a planted/chmod-flipped `.git`-subtree file is reported
    through the same added/modified/deleted delta as a worktree change
  - non-vacuity: the `.git` inventory is not silently empty for the kind of
    linked worktree the product actually hands a reviewer
  - hook-safety: `revert()`'s own `git checkout`/`git reset` calls cannot be
    made to execute a hook planted in the (real, already-installed)
    per-worktree hooks path
  - no false positives: read-only git plumbing and the documented volatile
    paths do not themselves trip the guard
  - the pre-existing worktree-file snapshot/compare/revert cycle and
    fail-closed semantics, unrelated to the `.git`-subtree work above
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from no_human.core import reviewer_worktree as rw
from no_human.vcs.git import GitRepo

PROTECTED = ["main", "master", "release/*"]
_TIMEOUT = 30.0


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


@pytest.fixture
def worktree_env(tmp_path):
    """A reviewer worktree built through the product's own `add_worktree`,
    so `no_human-hooks` (the hooks path already installed and effective —
    see `push_hook.py`) is present exactly as it is for a real task."""
    remote = tmp_path / "remote.git"
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "src").mkdir()
    (up / "src" / "main.py").write_text("v1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "init")
    _git(up, "remote", "add", "origin", str(remote))
    _git(up, "push", "-q", "origin", "main")

    wt_path = tmp_path / "reviewer-wt"
    main_repo = GitRepo(up, never_push_to=list(PROTECTED))
    main_repo.add_worktree(wt_path, base="main", detach=True)
    _git(wt_path, "checkout", "-q", "-b", "no-human/task-1")

    admin_dir = rw._resolve_git_root(wt_path, "--absolute-git-dir", timeout=_TIMEOUT)
    common_dir = rw._resolve_git_root(wt_path, "--git-common-dir", timeout=_TIMEOUT)
    return {
        "remote": remote, "up": up, "wt": wt_path,
        "admin_dir": admin_dir, "common_dir": common_dir,
        "hooks_dir": admin_dir / "no_human-hooks",
    }


def _plant_exec_hook(path: Path, canary: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {canary}\n")
    path.chmod(0o755)


def _plant_symlinked_hook(path: Path, canary: Path, target_dir: Path) -> Path:
    """Plant an executable OUTSIDE the repo and symlink `path` at it — the
    exact evasion a prior review round refuted this guard on: the old
    `_walk_git_root` unconditionally skipped `path.is_symlink()` entries, so
    `ln -s` past the inventory instead of writing a regular file defeated
    detection outright."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "payload.sh"
    target.write_text(f"#!/bin/sh\ntouch {canary}\n")
    target.chmod(0o755)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return target


# --------------------------------------------------------------------------- #
# Part A: `.git`-subtree detection
# --------------------------------------------------------------------------- #

def test_git_inventory_is_nonempty_for_a_linked_worktree(worktree_env):
    inv = rw._git_dir_inventory(worktree_env["wt"], timeout=_TIMEOUT)
    assert inv, "the .git inventory is empty for a real linked worktree"
    assert "admin/HEAD" in inv, sorted(inv)[:20]


def test_planted_git_hook_between_snapshot_and_compare_is_detected(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    hook = worktree_env["hooks_dir"] / "post-checkout"
    _plant_exec_hook(hook, worktree_env["wt"] / "canary")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), "planted .git hook was not detected"
    offending = [*delta.added, *delta.modified, *delta.deleted]
    assert any(p.startswith(".git/") and "post-checkout" in p for p in offending), offending


def test_planted_symlinked_git_hook_between_snapshot_and_compare_is_detected(worktree_env):
    """A prior revision of `_walk_git_root` unconditionally skipped any path
    where `path.is_symlink()` was true, so `ln -s /tmp/payload post-checkout`
    was invisible to the `.git` inventory even though git executes a
    symlinked hook exactly like a regular one. This is the exact evasion an
    independent review used to refute the previous attempt."""
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    hook = worktree_env["hooks_dir"] / "post-checkout"
    canary = worktree_env["wt"] / "canary"
    outside = worktree_env["wt"].parent / "outside-payload"
    _plant_symlinked_hook(hook, canary, outside)

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), "planted symlinked .git hook was not detected"
    offending = [*delta.added, *delta.modified, *delta.deleted]
    assert any(p.startswith(".git/") and "post-checkout" in p for p in offending), offending


def test_chmod_of_existing_git_file_is_detected(worktree_env):
    wt = worktree_env["wt"]
    sample = worktree_env["common_dir"] / "hooks" / "pre-commit.sample"
    assert sample.is_file(), "fixture assumption: git ships hooks/*.sample files"
    before_mode = stat.S_IMODE(sample.stat().st_mode)

    before = rw.snapshot(wt, timeout=_TIMEOUT)
    # XOR, not OR: git's shipped *.sample hooks are already mode 0o755 on some
    # git versions, so OR-ing in the exec bits would be a no-op. XOR-ing a
    # single bit is guaranteed to change the mode regardless of the starting
    # value.
    sample.chmod(before_mode ^ stat.S_IXOTH)

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert not delta.is_empty(), (
        "an exec-bit-only change on byte-identical content was missed"
    )
    offending = [*delta.added, *delta.modified, *delta.deleted]
    assert any("pre-commit.sample" in p for p in offending), offending


def test_benign_git_status_does_not_trigger_reviewer_wrote(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    # Real, read-only git plumbing rewrites `.git/index`'s stat cache — this
    # is the exact false positive the exclusion list exists to prevent.
    _git(wt, "status", "--porcelain")
    _git(wt, "log", "--oneline", "-1")
    _git(wt, "diff", "--stat")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        f"read-only git activity was reported as a write: added={delta.added} "
        f"modified={delta.modified} deleted={delta.deleted}"
    )


def test_excluded_volatile_paths_do_not_trigger_reviewer_wrote(worktree_env):
    """FETCH_HEAD/ORIG_HEAD/reflog churn constantly under ordinary git use
    (fetch, reset, merge) and carries no execution risk. Writing them
    directly isolates the exclusion list itself from git's own, less
    predictable, internals (e.g. whether a given git version elides a
    same-value ref rewrite)."""
    wt = worktree_env["wt"]
    common = worktree_env["common_dir"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (common / "FETCH_HEAD").write_text("deadbeef\t\tbranch 'main' of origin\n")
    (common / "ORIG_HEAD").write_text(_git(wt, "rev-parse", "HEAD").stdout)
    log_head = common / "logs" / "HEAD"
    log_head.parent.mkdir(parents=True, exist_ok=True)
    with log_head.open("a") as fh:
        fh.write("0" * 40 + " " + "1" * 40 + " t <t@t.t> 0 +0000\tcommit: x\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        f"a volatile-list path was reported as a write: added={delta.added} "
        f"modified={delta.modified} deleted={delta.deleted}"
    )


# --------------------------------------------------------------------------- #
# Part B: hook-safe revert
# --------------------------------------------------------------------------- #

def test_revert_does_not_execute_planted_post_checkout_hook(worktree_env):
    wt = worktree_env["wt"]
    canary = worktree_env["wt"].parent / "canary"
    assert not canary.exists()

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    # Plant in every location the resolved hooks-path chain can reach: the
    # already-installed, ALREADY-EFFECTIVE per-worktree hooks dir (this is
    # what `core.hooksPath` resolves to today, absent the fix's override —
    # see push_hook.py's per-worktree `core.hooksPath`), and the shared
    # common-dir hooks as a second surface.
    _plant_exec_hook(worktree_env["hooks_dir"] / "post-checkout", canary)
    _plant_exec_hook(worktree_env["common_dir"] / "hooks" / "post-checkout", canary)

    # Dirty a TRACKED file so revert() must run `git checkout <head> --
    # path` — the call whose hook-safety is under test. (`git reset` for
    # added paths never fires a hook at all, so that path would prove
    # nothing here.)
    tracked = wt / "src" / "main.py"
    tracked.write_text("v2 -- reviewer edit\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert "src/main.py" in delta.modified

    rw.revert(wt, before, delta, timeout=_TIMEOUT)

    assert not canary.exists(), "revert() executed a planted post-checkout hook"
    assert tracked.read_text() == "v1\n", "revert() did not restore the reviewed baseline"


def test_revert_does_not_execute_planted_symlinked_post_checkout_hook(worktree_env):
    """Same evasion as the detection-side symlink test, but exercised
    against `revert()`'s own `git checkout`/`git reset` calls: even if a
    symlinked hook slipped past detection, `-c core.hooksPath=<empty dir>`
    must still stop git from executing it."""
    wt = worktree_env["wt"]
    canary = worktree_env["wt"].parent / "symlink-canary"
    assert not canary.exists()

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    outside = worktree_env["wt"].parent / "outside-payload-2"
    _plant_symlinked_hook(worktree_env["hooks_dir"] / "post-checkout", canary, outside)
    _plant_symlinked_hook(
        worktree_env["common_dir"] / "hooks" / "post-checkout", canary, outside)

    tracked = wt / "src" / "main.py"
    tracked.write_text("v2 -- reviewer edit via symlinked hook\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert "src/main.py" in delta.modified

    rw.revert(wt, before, delta, timeout=_TIMEOUT)

    assert not canary.exists(), "revert() executed a planted symlinked post-checkout hook"
    assert tracked.read_text() == "v1\n", "revert() did not restore the reviewed baseline"


# --------------------------------------------------------------------------- #
# Baseline: pre-existing worktree-file snapshot/compare/revert + fail-closed
# --------------------------------------------------------------------------- #

def test_snapshot_and_compare_detect_worktree_add_modify_delete(worktree_env):
    wt = worktree_env["wt"]
    (wt / "src" / "extra.py").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "second file")

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (wt / "src" / "new_file.py").write_text("new\n")   # added
    (wt / "src" / "main.py").write_text("changed\n")   # modified
    (wt / "src" / "extra.py").unlink()                  # deleted

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.added == ["src/new_file.py"]
    assert delta.modified == ["src/main.py"]
    assert delta.deleted == ["src/extra.py"]


def test_revert_restores_worktree_to_snapshot(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (wt / "src" / "new_file.py").write_text("new\n")
    (wt / "src" / "main.py").write_text("changed\n")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    rw.revert(wt, before, delta, timeout=_TIMEOUT)

    assert not (wt / "src" / "new_file.py").exists()
    assert (wt / "src" / "main.py").read_text() == "v1\n"
    assert rw.compare(wt, before, timeout=_TIMEOUT).is_empty()


def test_compare_reports_moved_head_and_revert_refuses(worktree_env):
    wt = worktree_env["wt"]
    before = rw.snapshot(wt, timeout=_TIMEOUT)

    (wt / "src" / "main.py").write_text("committed change\n")
    _git(wt, "commit", "-qam", "reviewer committed")

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert any(m.startswith("HEAD:") for m in delta.modified), delta.modified

    with pytest.raises(rw.WorktreeCheckFailed, match="moved HEAD"):
        rw.revert(wt, before, delta, timeout=_TIMEOUT)


def test_snapshot_fails_closed_for_a_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(rw.WorktreeCheckFailed):
        rw.snapshot(not_a_repo, timeout=_TIMEOUT)


def test_guard_config_defaults_and_fallback():
    assert rw.guard_config(None) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config({}) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config({"pipeline": None}) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config(
        {"pipeline": {"reviewer_worktree_guard": {"timeout_seconds": "not-a-number"}}}
    ) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config(
        {"pipeline": {"reviewer_worktree_guard": {"timeout_seconds": -5}}}
    ) == rw._DEFAULT_TIMEOUT_SECONDS
    assert rw.guard_config(
        {"pipeline": {"reviewer_worktree_guard": {"timeout_seconds": 12}}}
    ) == 12.0


# --------------------------------------------------------------------------- #
# Part C: pruned shared subtrees (`objects/`, `refs/`) — perf + concurrency
# --------------------------------------------------------------------------- #

def test_new_loose_object_and_unrelated_ref_do_not_trigger_reviewer_wrote(worktree_env):
    """A second review refutation of this guard: `objects/` and `refs/` live
    in `--git-common-dir`, SHARED by every linked worktree of the repo, so
    unrelated concurrent git activity in another worktree/task lands there
    too. This reproduces exactly that shape without needing a second real
    worktree: `hash-object -w` adds a brand-new loose object under
    `common_dir/objects/`, and `update-ref` creates a brand-new ref under
    `common_dir/refs/heads/` for a branch this review never touched — neither
    touches this worktree's `index`, `HEAD`, or any tracked file. Both must
    be invisible to `compare()`: they are additions to shared,
    content-addressed/bookkeeping storage, not writes to an execution
    surface, and the checked-out ref this worktree actually cares about is
    independently covered by the `HEAD` comparison (see
    `test_compare_reports_moved_head_and_revert_refuses`)."""
    wt = worktree_env["wt"]
    common = worktree_env["common_dir"]

    before = rw.snapshot(wt, timeout=_TIMEOUT)

    # New loose object: content-addressed, never previously present, written
    # straight into the shared object store — the exact "an object can only
    # be added" case the `objects/` prune is justified on.
    new_blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(wt), input="unrelated concurrent worktree content\n",
        capture_output=True, text=True, check=True,
    )
    assert new_blob.stdout.strip(), "fixture assumption: hash-object printed a sha"
    oid = new_blob.stdout.strip()
    assert (common / "objects" / oid[:2] / oid[2:]).is_file(), (
        "fixture assumption: hash-object -w wrote a loose object file"
    )

    # New ref on a branch this review never touched, as if a sibling
    # worktree sharing this common dir pushed/branched concurrently.
    _git(wt, "update-ref", "refs/heads/unrelated-concurrent-branch", "HEAD")
    assert (common / "refs" / "heads" / "unrelated-concurrent-branch").is_file(), (
        "fixture assumption: update-ref wrote a loose ref file"
    )

    delta = rw.compare(wt, before, timeout=_TIMEOUT)
    assert delta.is_empty(), (
        "a new object/ref from shared, concurrent-safe storage was reported "
        f"as a write: added={delta.added} modified={delta.modified} "
        f"deleted={delta.deleted}"
    )
