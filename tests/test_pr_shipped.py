"""SCRUM-68 follow-up: every successfully shipped task escalated, because the
CLOSED-PR rung trusted GitHub's merged flag. The operator's hard rule is a
LOCAL, identity-normalized squash merge (never `gh pr merge`), so a squash
commit lands on the base branch with a fresh SHA that has no commit-graph
lineage back to the source branch — `git merge-base --is-ancestor` is FALSE
for every one of our merges even when the change is fully landed. These tests
pin the content-based fix: `default_branch_shipped` (git-backed) and its
wiring into WakeWatcher._check_open_pr's CLOSED rung."""

from __future__ import annotations

import subprocess
import time

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import pr_watcher
from no_human.vcs.pr_watcher import default_branch_shipped


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_rc(repo_path, *args):
    """git's exit code, failure allowed. Sync on purpose: an inline
    ``subprocess.run`` inside an async test blocks the event loop."""
    return subprocess.run(["git", "-C", str(repo_path), *args],
                          capture_output=True, check=False).returncode


def _git_out(repo_path, *args):
    """git's stdout, failure allowed. Sync for the same reason as ``_git_rc``."""
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=False).stdout


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _squash_merge_repo(tmp_path):
    """Branch commits a real change; main gets the SAME content via a fresh
    (squash-shaped) commit — branch head is NOT an ancestor of main, but the
    touched file's content matches."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "squash: change a.txt (fresh sha, no lineage)")
    return repo


def _unshipped_repo(tmp_path):
    """Branch commits a real change that never made it to main at all."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    return repo


def _with_upstream(repo):
    """Give ``main`` a real upstream, as any working checkout has."""
    bare = repo.parent / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _stale_local_base_repo(tmp_path, *, land=True):
    """The live shape that escalated two shipped tasks on 2026-08-10.

    The identity-normalized squash is committed and PUSHED from a throwaway
    worktree, so ``refs/remotes/origin/main`` carries the landing while the
    long-lived checkout the watcher inspects keeps its own ``main`` exactly
    where it was — here, and in the real repo, 20+ commits behind.
    """
    repo = _with_upstream(_make_repo(tmp_path))
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    if land:
        _git(repo, "checkout", "-b", "landing", "main")
        (repo / "a.txt").write_text("changed\n")
        _git(repo, "commit", "-am", "squash: land the feature content")
        _git(repo, "push", "origin", "landing:main")
        _git(repo, "checkout", "main")
        _git(repo, "branch", "-D", "landing")
    _git(repo, "checkout", "main")
    _git(repo, "fetch", "origin")
    return repo


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def _approval_task(store, repo_path, url="https://github.com/o/r/pull/86"):
    t = Task.new("shipped-check", repo_path=str(repo_path))
    t.context = {"pr_watch": url, "pr_branch": "feature", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


async def test_closed_pr_with_squash_merged_content_ships_not_escalates(tmp_path, store):
    repo = _squash_merge_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "shipped_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_closed_pr_with_content_genuinely_absent_still_escalates(tmp_path, store):
    repo = _unshipped_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "closed without merging" in fresh.blocker["question"]


async def test_closed_pr_with_no_pr_shipped_hook_falls_back_to_escalation(tmp_path, store):
    """Backward compatibility: hosts that don't wire pr_shipped keep the old
    (safe, if noisy) behavior rather than crashing."""
    repo = _unshipped_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"


async def test_a_half_landed_rename_is_not_shipped(tmp_path):
    """A branch that MOVES a file must not report shipped when only the
    destination landed on base.

    `git diff --name-only` has rename detection ON by default, so a `git mv`
    is reported as the destination path alone — the source path never enters
    the touched set and its deletion is never compared. Without
    `--no-renames` this returns True while `old.py` is still sitting on base,
    and the task is marked DONE with half its deliverable missing.
    """
    repo = _make_repo(tmp_path)
    (repo / "old.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add old")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "move old -> new")
    # base gets the destination but NOT the removal — a half-landed move.
    _git(repo, "checkout", "main")
    (repo / "new.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add new, forget to delete old")

    assert (repo / "old.py").exists(), "fixture: base must still carry old.py"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_fully_landed_rename_is_shipped(tmp_path):
    """The companion: once BOTH halves of the move are on base, it ships.

    Without this, `--no-renames` could be 'fixed' by always returning False
    for any branch that renames anything.
    """
    repo = _make_repo(tmp_path)
    (repo / "old.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add old")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "move old -> new")
    _git(repo, "checkout", "main")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "same move, squash-landed")

    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_branch_name_that_is_also_a_path_does_not_crash(tmp_path):
    """Without a trailing `--`, git bails with 'ambiguous argument' when a
    branch name is also a path in the tree. That failed safe (escalate), but
    it meant a genuinely shipped task kept escalating forever."""
    repo = _make_repo(tmp_path)
    (repo / "dup").write_text("i am a file named like a branch\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add dup file")
    _git(repo, "checkout", "-b", "dup")
    (repo / "feat.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature work")
    _git(repo, "checkout", "main")
    (repo / "feat.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash-land the same content")

    assert await default_branch_shipped(str(repo), "dup", "main") is True


async def test_a_deleted_branch_fails_safe(tmp_path):
    """The branch ref may be gone by the time the watcher looks. That must
    escalate (the old behaviour), never silently report shipped."""
    repo = _make_repo(tmp_path)
    assert await default_branch_shipped(str(repo), "no-such-branch", "main") is False


async def test_a_non_ascii_path_that_never_landed_is_not_shipped(tmp_path):
    """A branch whose touched paths need C-quoting must not report shipped.

    `core.quotePath` is on by default, so `git diff --name-only` returns
    `café.py` as the literal 7-character string `"caf\303\251.py"`. Feeding
    that back as a pathspec matches NOTHING, `git diff --quiet` then reports
    "no differences", and a branch whose entire deliverable is unmerged is
    marked DONE. `-z` emits raw NUL-separated names instead.

    No other test in this file uses a path outside [a-z0-9_.], which is why
    this survived two rounds of review.
    """
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a non-ascii path")
    _git(repo, "checkout", "main")

    assert not (repo / "café.py").exists(), "fixture: base must NOT have the file"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_non_ascii_path_that_did_land_is_shipped(tmp_path):
    """Companion, so the fix cannot be 'return False for anything unusual'."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a non-ascii path")
    _git(repo, "checkout", "main")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash-land the same content")

    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_stale_local_base_does_not_hide_a_landed_pr(tmp_path):
    """The defect: the probe compared against the checkout's own ``main``.

    That ref is routinely days behind the branch the PR actually merged into,
    so a fully landed PR read as "content absent" and the watcher escalated
    "closed without merging" minutes after the content was on main.
    """
    repo = _stale_local_base_repo(tmp_path)
    rc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "feature", "main", "--", "a.txt"]
    ).returncode
    assert rc != 0, "fixture: the LOCAL base must still be missing the landing"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_stale_local_base_does_not_invent_a_landing(tmp_path):
    """Control: an upstream that never received the content still says no."""
    repo = _stale_local_base_repo(tmp_path, land=False)
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_closed_pr_ships_when_only_the_upstream_base_has_the_landing(tmp_path, store):
    """End to end through the CLOSED rung, on the shape that misfired live."""
    repo = _stale_local_base_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    assert await w._check_open_pr(t) == "shipped_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_a_shared_generated_file_base_extended_further_is_still_shipped(tmp_path):
    """RELEASE_MANIFEST.txt's shape, and the second half of the live defect.

    Every PR here edits the same generated checksum manifest, so by the time
    the watcher looks, base carries this PR's line PLUS every other landing's.
    A per-file BYTE-EQUALITY test can therefore never recognise such a PR
    again — task 2f29209f's content was on main and its manifest still
    differed. A three-way merge that changes nothing is the honest question:
    does the branch still have anything to contribute?
    """
    repo = _make_repo(tmp_path)
    (repo / "manifest.txt").write_text("aaa  one\nbbb  two\nccc  three\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the generated manifest")
    _git(repo, "checkout", "-b", "feature")
    (repo / "manifest.txt").write_text("aaa  one\nBBB  two\nccc  three\n")
    _git(repo, "commit", "-am", "feature: re-hash two")
    _git(repo, "checkout", "main")
    # base got this branch's line AND another landing's new entry.
    (repo / "manifest.txt").write_text("aaa  one\nBBB  two\nccc  three\nddd  four\n")
    _git(repo, "commit", "-am", "squash-land, plus another task's entry")

    rc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "feature", "main", "--",
         "manifest.txt"]).returncode
    assert rc != 0, "fixture: the file must NOT be byte-identical on base"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_deletion_that_landed_is_shipped(tmp_path):
    """A PR whose deliverable is a REMOVAL ships once base has removed it too."""
    repo = _make_repo(tmp_path)
    (repo / "gone.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the file the PR will delete")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-m", "feature: delete gone.py")
    _git(repo, "checkout", "main")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-m", "squash-land the deletion")

    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_deletion_still_absent_from_base_is_not_shipped(tmp_path):
    """Companion, so "shipped" cannot be reached by ignoring removals."""
    repo = _make_repo(tmp_path)
    (repo / "gone.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the file the PR will delete")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-m", "feature: delete gone.py")
    _git(repo, "checkout", "main")

    assert (repo / "gone.py").exists(), "fixture: base must still carry the file"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


def _f_py(first, last="epsilon"):
    """The driver-owned file. Multi-line so a later task's edit to the LAST
    line and the branch's edit to the FIRST are separate hunks — without a
    driver they merge cleanly, which is what makes the no-driver control a
    control rather than a conflict."""
    return f"{first}\nbeta\ngamma\ndelta\n{last}\n"


def _driver_ran(tmp_path):
    """Whether the fixture's merge driver actually fired.

    git invokes a merge driver ONLY when BOTH sides changed the path AND the
    two blobs differ; anything else resolves trivially and the driver is never
    consulted. A driver test without this sentinel therefore proves nothing —
    which is exactly how the round-3 defect hid.
    """
    return (tmp_path / "drvran").exists()


def _merge_driver_repo(tmp_path, *, land, extend=False, attrs="both"):
    """A repo whose `.gitattributes` routes a file to a merge driver that
    DISCARDS the incoming side (it only touches the sentinel, leaving %A, i.e.
    ours, untouched — `true` with a receipt).

    Nothing here is exotic: `merge=<name>` in `.gitattributes` plus
    `merge.<name>.driver` in config is the documented way to own a generated
    or lockfile-shaped path, and `pr_watcher` runs against the USER'S repo, so
    any customer repo may carry one.

    ``attrs``: ``"both"`` commits the attributes before branching (both sides
    carry them), ``"base"`` commits them on ``main`` only, ``None`` builds the
    same history with NO driver at all — the control.
    ``extend``: a later task edits the same path after the landing, which is
    what makes both sides differ and so is what makes the driver fire.
    """
    repo = _make_repo(tmp_path)
    rule = "f.py merge=keepours\n"
    if attrs:
        _git(repo, "config", "merge.keepours.driver",
             f'touch "{tmp_path / "drvran"}"')
    if attrs == "both":
        (repo / ".gitattributes").write_text(rule)
    (repo / "f.py").write_text(_f_py("alpha"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add f.py behind a custom merge driver")
    _git(repo, "checkout", "-b", "feature")
    (repo / "f.py").write_text(_f_py("feature content"))
    _git(repo, "commit", "-am", "feature: rewrite f.py")
    _git(repo, "checkout", "main")
    if attrs == "base":
        (repo / ".gitattributes").write_text(rule)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "base only: adopt the driver")
    (repo / "f.py").write_text(_f_py("feature content" if land else "something else"))
    _git(repo, "commit", "-am", "land it" if land else "unrelated change to f.py")
    if extend:
        (repo / "f.py").write_text(_f_py("feature content", "a later task's line"))
        _git(repo, "commit", "-am", "a later task extends the same path")
    return repo


async def test_a_custom_merge_driver_cannot_manufacture_a_landing(tmp_path):
    """A merge driver that discards "theirs" must not be able to report
    shipped for content that never landed.

    `git merge-tree` honours `.gitattributes`, so a driver like `true` (keep
    ours) resolves every contested path to the BASE TIP's own side: exit 0,
    and the tree written is byte-for-byte the tip's tree. Trusting that pair
    alone reports shipped for a branch whose whole deliverable is still
    missing, and the CLOSED rung turns shipped into `TaskStatus.DONE` with no
    human in the loop -- a silent completion on undelivered work. The old
    per-file blob comparison was immune (attributes never enter a `git diff`),
    so this is a regression the merge-tree switch would introduce.
    """
    repo = _merge_driver_repo(tmp_path, land=False)
    assert "feature content" not in (repo / "f.py").read_text(), \
        "fixture: base must NOT carry the feature content"
    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_genuine_landing_under_a_position_driver_reads_absent_once_base_moves(tmp_path):
    """🔴 THIS TEST PINS A COST, NOT A GUARANTEE. It asserts False for content
    that DID land -- i.e. it documents a live spurious escalation.

    It is the price of the both-directions rule, and the direct counter-example
    to the claim this docstring's predecessor made ("a genuine landing is
    unaffected"). A position-resolving driver on a generated or lockfile-shaped
    path is exactly where such drivers live, so the shape is ordinary: the PR
    edits the path, genuinely lands, and a LATER task edits the same path. Both
    sides now carry an edit, so the driver fires; the forward pass keeps the
    tip's blob (True) and the reverse pass keeps the BRANCH's blob (False).

    The failure is fail-CLOSED -- a human reads the escalation -- which is why
    the trade stands. It is also not curable without a merge engine that
    ignores merge drivers, which `merge-tree` does not offer.

    Its predecessor asserted True here and was VACUOUS: both sides made the
    IDENTICAL edit, so git resolved trivially and never ran the driver, which
    is how this went unnoticed. Hence the sentinel.
    """
    repo = _merge_driver_repo(tmp_path, land=True, extend=True)
    assert "feature content" in (repo / "f.py").read_text(), \
        "fixture: base really DID receive the branch's content"
    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_the_same_landing_without_a_driver_is_still_shipped(tmp_path):
    """The control for the test above: identical history, no merge driver. It
    ships. So the driver is the sole cause of that False, not the fact that
    base moved on -- and the cure was not "return False whenever the path was
    touched twice"."""
    repo = _merge_driver_repo(tmp_path, land=True, extend=True, attrs=None)
    assert await default_branch_shipped(str(repo), "feature", "main") is True
    assert not _driver_ran(tmp_path), "fixture: the control must have no driver"


async def test_a_merge_driver_in_info_attributes_cannot_manufacture_a_landing(tmp_path):
    """The same attack through `$GIT_DIR/info/attributes`, which outranks the
    in-tree file and which no `--attr-source` / `core.attributesFile` override
    can switch off. Pinned so the cure cannot be narrowed to `.gitattributes`.
    """
    repo = _merge_driver_repo(tmp_path, land=False)
    (repo / ".git" / "info" / "attributes").write_text("f.py merge=keepours\n")
    _git(repo, "rm", "-q", "--cached", ".gitattributes")
    (repo / ".gitattributes").unlink()
    _git(repo, "commit", "-m", "drop the in-tree attributes file")

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_merge_driver_from_core_attributesfile_cannot_manufacture_a_landing(tmp_path):
    """The same attack through `core.attributesFile`, i.e. attributes that live
    entirely outside the repo."""
    repo = _merge_driver_repo(tmp_path, land=False)
    external = tmp_path / "attributes"
    external.write_text("f.py merge=keepours\n")
    _git(repo, "config", "core.attributesFile", str(external))
    _git(repo, "rm", "-q", "--cached", ".gitattributes")
    (repo / ".gitattributes").unlink()
    _git(repo, "commit", "-m", "drop the in-tree attributes file")

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_wildcard_merge_driver_cannot_manufacture_a_landing(tmp_path):
    """The same attack routed by `*` rather than by an exact path, so the rule
    is not one the probe could dodge by naming the file."""
    repo = _merge_driver_repo(tmp_path, land=False)
    (repo / ".gitattributes").write_text("* merge=keepours\n")
    _git(repo, "commit", "-am", "route EVERY path to the driver")

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_merge_driver_attached_on_the_base_side_only_cannot_manufacture_a_landing(tmp_path):
    """The same attack with the attributes committed on ONE side only -- base,
    which is the side the checkout is usually on: `merge-tree` reads the
    CHECKOUT's attributes, not either commit's, so a rule living only on base
    fires exactly as if both sides carried it, which is what this pins. The
    mirror case is conditional rather than absolute: a rule living only on the
    branch goes unconsulted while the checkout sits on base (a weaker no, not a
    defeated driver), and IS consulted once the branch itself is checked out --
    verified both ways, False either way."""
    repo = _merge_driver_repo(tmp_path, land=False, attrs="base")
    assert _git_rc(repo, "cat-file", "-e", "feature:.gitattributes") != 0, \
        "fixture: the branch side must NOT carry the attributes"

    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert _driver_ran(tmp_path), "vacuous: the driver was never consulted"


async def test_a_deleted_local_base_fails_closed_instead_of_guessing_a_remote(tmp_path):
    """A worktree or fresh checkout need not carry a LOCAL branch named
    ``main``, and ``<base>@{upstream}`` is only defined for a local branch. So
    with no such branch NEITHER candidate tip resolves and a fully landed PR
    reads as absent.

    That is a deliberate choice, not an oversight. A ``refs/remotes/*/<base>``
    fallback would answer this case, and was tried -- but a glob over every
    remote cannot tell the remote the PR TARGETS from any other remote that
    happens to carry a branch of the same name, and the two tests below show it
    manufacturing a landing out of a fork's ``main``. The two outcomes are not
    symmetric: a False costs one spurious escalation with a human on the other
    end of it, while a True writes ``TaskStatus.DONE`` on undelivered work with
    no human at all. Fail closed.
    """
    repo = _stale_local_base_repo(tmp_path)
    _git(repo, "checkout", "feature")
    _git(repo, "branch", "-D", "main")
    assert _git_rc(repo, "rev-parse", "--verify", "--quiet", "main") != 0, \
        "fixture: the local base branch must be gone"
    assert _git_out(repo, "show", "origin/main:a.txt") == "changed\n", \
        "fixture: the remote-tracking ref really does carry the landing"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_deleted_local_base_does_not_invent_a_landing(tmp_path):
    """Companion: nothing on any ref carries the content either, so the same
    False here is the honest answer rather than an artefact of failing closed.
    """
    repo = _stale_local_base_repo(tmp_path, land=False)
    _git(repo, "checkout", "feature")
    _git(repo, "branch", "-D", "main")
    assert await default_branch_shipped(str(repo), "feature", "main") is False


def _fork_layout_repo(tmp_path, *, local_base):
    """The standard OSS fork layout: ``origin`` is the developer's OWN fork and
    ``upstream`` is the canonical repo the PR actually targets.

    The developer merged the branch into their fork's ``main``. The PR against
    canonical is still open, or was closed unmerged -- so the content is on
    ``origin/main`` and is NOT on ``upstream/main``, which is the tip the
    question is about.

    ``local_base`` picks which of the two routes into a glob fallback this
    exercises: with a local ``main`` created ``--no-track`` (or from a
    customised ``remote.origin.fetch``, or a locally-created base branch),
    ``main@{upstream}`` is unset even though the branch exists; without one,
    there is no local branch for ``@{upstream}`` to be defined on at all.
    """
    canonical = tmp_path / "canonical.git"
    fork = tmp_path / "fork.git"
    for bare in (canonical, fork):
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                       check=True, capture_output=True)
    seed = _make_repo(tmp_path)
    _git(seed, "remote", "add", "canonical", str(canonical))
    _git(seed, "remote", "add", "fork", str(fork))
    _git(seed, "push", "canonical", "main")
    _git(seed, "push", "fork", "main")
    _git(seed, "checkout", "-b", "feature")
    (seed / "a.txt").write_text("changed\n")
    _git(seed, "commit", "-am", "feature: change a.txt")
    _git(seed, "checkout", "main")
    (seed / "a.txt").write_text("changed\n")
    _git(seed, "commit", "-am", "squash-land onto MY OWN fork, not canonical")
    _git(seed, "push", "fork", "main")

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True,
                   capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(fork))
    _git(work, "remote", "add", "upstream", str(canonical))
    _git(work, "fetch", "origin")
    _git(work, "fetch", "upstream")
    _git(work, "fetch", str(seed), "feature:feature")
    _git(work, "checkout", "feature")
    if local_base:
        _git(work, "branch", "--no-track", "main", "upstream/main")
    return work


def _assert_fork_layout(work):
    """Pin the shape the two tests below depend on, so neither can pass by
    accident: the PR's target tip lacks the content and the fork's has it."""
    assert _git_out(work, "show", "upstream/main:a.txt") == "orig\n", \
        "fixture: canonical (what the PR targets) must NOT have the content"
    assert _git_out(work, "show", "origin/main:a.txt") == "changed\n", \
        "fixture: the developer's fork MUST have the content"


async def test_a_fork_remote_cannot_vouch_for_a_landing_on_canonical(tmp_path):
    """A local base branch with no upstream must not let ANY remote answer.

    ``--no-track`` (and a customised ``remote.origin.fetch``, and a
    locally-created base branch) leaves ``main@{upstream}`` unset while ``main``
    itself exists. A ``refs/remotes/*/main`` fallback then offers
    ``origin/main`` -- the developer's own fork -- as evidence about a PR that
    targeted ``upstream``, and the CLOSED rung turns that True into
    ``TaskStatus.DONE`` with no human in the loop.
    """
    work = _fork_layout_repo(tmp_path, local_base=True)
    _assert_fork_layout(work)
    assert _git_rc(work, "rev-parse", "--abbrev-ref", "main@{upstream}") != 0, \
        "fixture: the local base must have no configured upstream"
    assert _git_rc(work, "rev-parse", "--verify", "--quiet", "main") == 0, \
        "fixture: but the local base branch itself must exist"
    assert await default_branch_shipped(str(work), "feature", "main") is False


async def test_a_fork_remote_cannot_vouch_when_there_is_no_local_base(tmp_path):
    """The same attack through the other route into the fallback: no local
    ``main`` at all, so ``@{upstream}`` cannot be defined."""
    work = _fork_layout_repo(tmp_path, local_base=False)
    _assert_fork_layout(work)
    assert _git_rc(work, "rev-parse", "--verify", "--quiet", "main") != 0, \
        "fixture: there must be no local base branch"
    assert await default_branch_shipped(str(work), "feature", "main") is False


async def test_a_content_resolving_merge_driver_is_a_known_residual(tmp_path):
    """🔴 THIS TEST PINS A HOLE, NOT A GUARANTEE. It asserts True for content
    that never landed -- i.e. it documents a live false "shipped".

    The both-directions rule defeats drivers that resolve by POSITION (which
    side git handed them as %A). It does NOT defeat drivers that resolve by
    CONTENT: a constant emitter, a regenerator, or the rule-picker below all
    write the same bytes whichever side is %A, so they satisfy BOTH passes and
    the probe returns True. That is a silent ``TaskStatus.DONE`` on undelivered
    work.

    It is PRE-EXISTING, not a regression: the single-direction predecessor
    returns True here too. Closing it needs a merge that ignores merge drivers
    entirely, which `merge-tree` offers no flag for.

    Kept as a test rather than a comment so the residual cannot rot silently:
    if a later change makes this False, that is a WIN -- delete the test and
    the "NOT COVERED" paragraph in ``default_branch_shipped``'s docstring
    together, so the claim and the mechanism move as one.
    """
    driver = tmp_path / "pick.sh"
    driver.write_text(
        "#!/bin/bash\n"
        # %A=ours %B=theirs -- keep whichever side lacks the branch marker,
        # by CONTENT, so the answer does not depend on the argument order.
        'if grep -q "feature content" "$1"; then cp "$2" "$1"; fi\nexit 0\n'
    )
    driver.chmod(0o755)
    repo = _merge_driver_repo(tmp_path, land=False)
    _git(repo, "config", "merge.keepours.driver", f"{driver} %A %B")

    assert "feature content" not in (repo / "f.py").read_text(), \
        "fixture: base must NOT carry the feature content"
    assert await default_branch_shipped(str(repo), "feature", "main") is True, \
        "if this is now False the hole is closed -- update the docstring too"


async def test_a_hung_merge_driver_cannot_wedge_the_watcher(tmp_path, monkeypatch):
    """``merge-tree`` EXECUTES the repo's custom merge drivers, so a driver
    that blocks blocks the watcher's event loop for as long as it likes. The
    probe runs up to two of them per candidate tip, against the USER'S repo.

    A bounded wait turns that into the same fail-closed False every other git
    failure produces.
    """
    repo = _merge_driver_repo(tmp_path, land=False)
    _git(repo, "config", "merge.keepours.driver", "sleep 20")
    monkeypatch.setattr(pr_watcher, "_GIT_TIMEOUT", 1.0)

    started = time.monotonic()
    assert await default_branch_shipped(str(repo), "feature", "main") is False
    assert time.monotonic() - started < 15, "the hung driver was not bounded"


async def test_squash_merge_shape_is_shipped_despite_no_ancestry(tmp_path):
    repo = _squash_merge_repo(tmp_path)
    rc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "feature", "main"]
    ).returncode
    assert rc != 0, "fixture must NOT have branch-head ancestry into main"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_genuinely_absent_branch_is_not_shipped(tmp_path):
    repo = _unshipped_repo(tmp_path)
    assert await default_branch_shipped(str(repo), "feature", "main") is False
