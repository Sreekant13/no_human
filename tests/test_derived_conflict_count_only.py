"""Predicate-only unit tests for `classification_count_only`
(`src/no_human/vcs/derived_conflict.py`) -- the conflict-SHAPE test that
decides whether a real `EXPORT_CLASSIFICATION.txt` merge conflict is "both
sides bumped the same rule's count for files each independently added"
(arithmetic, safe to repair with the existing `reconcile_merge_count_drift`)
versus any other edit (a hand decision, must still open a coder round).

These tests call the predicate directly against a tiny two-commit repo -- no
worktree, no export_guard stub, no PR-conflict machinery -- so each shape is
isolated from the end-to-end mechanical-resolution tests in
`test_orchestrator_pr_conflict.py`. The predicate only ever reads two git
revisions (`git show <rev>:EXPORT_CLASSIFICATION.txt`); it does not care
whether those revisions are actually in conflict, so a bare two-branch repo
is enough.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from no_human.vcs.derived_conflict import classification_count_only

BASE_TEXT = (
    "ship src/**\n"
    "ship   1  src/base*.py\n"
    "drop   1  tests/**\n"
    "drop   1  EXPORT_CLASSIFICATION.txt\n"
)


def _run(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{args} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def _git(cwd, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd)


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    _run(["git", "init", "-q", "-b", "main", str(work)], cwd=tmp_path)
    _git(work, "config", "user.email", "a@example.com")
    _git(work, "config", "user.name", "a")
    (work / "EXPORT_CLASSIFICATION.txt").write_text(BASE_TEXT, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    return work


def _write_commit(work: Path, text: str, message: str) -> str:
    """Write `text` to EXPORT_CLASSIFICATION.txt and commit iff it actually
    changed (some scenarios deliberately leave one side identical to the
    common ancestor, which would otherwise make `git commit -a` fail with
    'nothing to commit'). Returns the resulting HEAD sha either way."""
    (work / "EXPORT_CLASSIFICATION.txt").write_text(text, encoding="utf-8")
    _git(work, "add", "-A")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(work), capture_output=True, text=True)
    if staged.returncode != 0:
        _git(work, "commit", "-qm", message)
    return _git(work, "rev-parse", "HEAD").stdout.strip()


def _two_revs(tmp_path: Path, base_text: str, branch_text: str) -> tuple[Path, str, str]:
    """A repo with a common ancestor (`BASE_TEXT`), then `main` rewritten to
    `base_text` and a `feature` branch (forked before `main`'s edit)
    rewritten to `branch_text`. Returns (repo, base_sha, branch_sha)."""
    work = _repo(tmp_path)
    _git(work, "checkout", "-qb", "feature")
    branch_sha = _write_commit(work, branch_text, "branch edit")

    _git(work, "checkout", "-q", "main")
    base_sha = _write_commit(work, base_text, "base edit")

    return work, base_sha, branch_sha


async def test_a_digit_only_change_on_both_sides_is_count_only(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is True


async def test_a_pattern_change_is_not_count_only(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/basic*.py"),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_a_verb_flip_is_not_count_only(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "drop   2  src/base*.py"),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_an_added_rule_is_not_count_only(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace(
            "ship   1  src/base*.py", "ship   2  src/base*.py\nship   1  src/extra*.py"),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_a_removed_rule_is_not_count_only(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py").replace(
            "drop   1  tests/**\n", ""),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_a_reordered_pair_of_rules_is_not_count_only(tmp_path):
    reordered = (
        "ship src/**\n"
        "drop   1  tests/**\n"
        "ship   2  src/base*.py\n"
        "drop   1  EXPORT_CLASSIFICATION.txt\n"
    )
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=reordered,
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_a_comment_block_edit_is_not_count_only(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text="# updated rationale\n" + BASE_TEXT.replace(
            "ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_a_whitespace_only_reflow_is_not_count_only(tmp_path):
    """Same digit on both sides (no count change at all), but the branch
    reflows the spacing around the pattern -- the strict textual half of the
    predicate must still catch this; the decisions-only half (which elides
    the count and therefore the surrounding spacing detail) would miss it."""
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT,
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   1   src/base*.py"),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_the_file_missing_on_one_side_is_not_count_only(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    _git(work, "checkout", "-q", "feature")
    _git(work, "rm", "-q", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "remove classification file")
    branch_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "checkout", "-q", "main")

    assert await classification_count_only(str(work), base_sha, branch_sha) is False


async def test_a_git_failure_resolving_the_base_is_not_count_only(tmp_path):
    work, _base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    bogus_sha = "0" * 40
    assert await classification_count_only(str(work), bogus_sha, branch_sha) is False
