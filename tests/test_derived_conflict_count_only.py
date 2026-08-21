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

from no_human.vcs import derived_conflict as dc
from no_human.vcs.derived_conflict import classification_count_only
from no_human.vcs.pr_watcher import _git_rc

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


# --------------------------------------------------------------------------- #
# The branch's own edit is judged against the MERGE-BASE, not against main's   #
# tip: a rule line main gained AFTER the fork is main's decision, merges       #
# cleanly, and must not defeat the count-only shape (live: task 63928824,      #
# PR #592 -- a coder round was opened for `ship 321 -> 322` vs `321 -> 323`    #
# only because main had also gained two unrelated rule lines).                #
# --------------------------------------------------------------------------- #

# Synthetic rule lines, deliberately NOT real paths from this repo's
# EXPORT_CLASSIFICATION.txt. One of these used to name a document the export
# actually drops, and naming such a document from a SHIPPED test file is
# exactly what test_no_exported_source_file_names_a_manifest_dropped_document
# exists to catch — it turned main red on 2026-08-22. (Do not name the file
# here either, even to explain the fix: the guard counts mentions, and a
# comment is a mention. That is how the first attempt at this fix stayed red.)
# The test only needs two rule lines main gained after the fork; what they name
# is irrelevant to it.
MAIN_GAINED_LINES = (
    "ship   1  plugins/example-catalog.json\n"
    "drop   1  docs/EXAMPLE_NOTES.md\n"
)


async def test_a_rule_line_main_gained_since_the_fork_is_still_count_only(tmp_path):
    """The bug: main bumped the same rule 1 -> 3 AND (from other landings)
    gained two rule lines the branch never saw. The branch's own edit is
    still nothing but the digit, so the conflict is arithmetic."""
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py")
        + MAIN_GAINED_LINES,
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    assert await classification_count_only(str(work), base_sha, branch_sha) is True


async def test_an_unresolvable_merge_base_is_not_count_only(tmp_path):
    """Fail-closed: two histories with NO common ancestor whose files happen
    to differ only in a digit are not a count-only merge -- there is no
    merge-base to judge the branch's own edit against, so the shape is
    unknown and must be refused."""
    work = _repo(tmp_path)
    _git(work, "checkout", "-q", "--orphan", "orphan")
    branch_sha = _write_commit(
        work, BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
        "unrelated history")
    _git(work, "checkout", "-q", "main")
    base_sha = _write_commit(
        work, BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        "base edit")

    rc, _out = await _git_rc(str(work), "merge-base", base_sha, branch_sha)
    assert rc != 0, "fixture must have NO merge base"
    assert await classification_count_only(str(work), base_sha, branch_sha) is False


# --------------------------------------------------------------------------- #
# `conflict_hunks_count_only`: the pure half of the hunk check                 #
# --------------------------------------------------------------------------- #

_TWO_WAY = (
    "ship src/**\n"
    "<<<<<<< HEAD\n"
    "ship   2  src/base*.py\n"
    "=======\n"
    "ship   3  src/base*.py\n"
    ">>>>>>> main\n"
    "drop   1  tests/**\n"
)
_DIFF3 = (
    "ship src/**\n"
    "<<<<<<< HEAD\n"
    "ship   2  src/base*.py\n"
    "||||||| 4a076a2\n"
    "ship   1  src/base*.py\n"
    "=======\n"
    "ship   3  src/base*.py\n"
    ">>>>>>> main\n"
    "drop   1  tests/**\n"
)


def test_a_two_way_count_only_hunk_is_count_only():
    assert dc.conflict_hunks_count_only(_TWO_WAY) is True


def test_a_diff3_count_only_hunk_is_count_only():
    assert dc.conflict_hunks_count_only(_DIFF3) is True


def test_a_hunk_whose_sides_differ_by_more_than_a_digit_is_not_count_only():
    """Main's side of the hunk carries an ADDED rule line as well as the
    count -- a decision landed inside the conflict, not arithmetic."""
    mixed = _TWO_WAY.replace(
        "ship   3  src/base*.py\n", "ship   3  src/base*.py\ndrop   1  dist/**\n")
    assert dc.conflict_hunks_count_only(mixed) is False


def test_a_verb_flip_inside_a_hunk_is_not_count_only():
    flipped = _TWO_WAY.replace("ship   3  src/base*.py", "drop   3  src/base*.py")
    assert dc.conflict_hunks_count_only(flipped) is False


def test_a_diff3_hunk_whose_base_side_differs_by_more_than_a_digit_is_not_count_only():
    other_base = _DIFF3.replace("ship   1  src/base*.py", "ship   1  src/basic*.py")
    assert dc.conflict_hunks_count_only(other_base) is False


def test_a_non_rule_line_inside_a_hunk_is_not_count_only():
    """A conflicting comment/blank is a hand edit even when both sides'
    rule lines match modulo the digit."""
    commented = _TWO_WAY.replace(
        "ship   2  src/base*.py\n", "# why\nship   2  src/base*.py\n").replace(
        "ship   3  src/base*.py\n", "# why not\nship   3  src/base*.py\n")
    assert dc.conflict_hunks_count_only(commented) is False


def test_text_with_no_conflict_markers_is_not_count_only():
    assert dc.conflict_hunks_count_only(BASE_TEXT) is False


def test_an_unterminated_conflict_hunk_is_not_count_only():
    assert dc.conflict_hunks_count_only(
        "ship src/**\n<<<<<<< HEAD\nship   2  src/base*.py\n") is False


def test_a_stray_conflict_marker_outside_a_hunk_is_not_count_only():
    assert dc.conflict_hunks_count_only(
        "ship src/**\n=======\nship   2  src/base*.py\n") is False


def test_an_empty_side_of_a_hunk_is_not_count_only():
    """One side deleted the rule outright -- a decision, not a tally."""
    assert dc.conflict_hunks_count_only(
        "<<<<<<< HEAD\nship   2  src/base*.py\n=======\n>>>>>>> main\n") is False


# --------------------------------------------------------------------------- #
# `mechanically_resolvable` on a REAL three-way conflict                       #
# --------------------------------------------------------------------------- #

CLASSIFICATION_ONLY = {"EXPORT_CLASSIFICATION.txt"}


async def test_mechanically_resolvable_accepts_a_count_conflict_when_main_gained_a_rule(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py")
        + MAIN_GAINED_LINES,
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    eligible = await dc.mechanically_resolvable(
        str(work), set(CLASSIFICATION_ONLY), base_sha, branch_sha)
    assert eligible == dc.DERIVED_ARTEFACTS | {dc.CLASSIFICATION_NAME}


async def test_mechanically_resolvable_refuses_a_main_side_addition_inside_the_hunk(tmp_path):
    """Main's new rule line lands ADJACENT to the count line, so git folds
    both into ONE conflicting hunk: the branch's own edit is count-only, but
    the hunk itself carries a decision, so this must still refuse."""
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace(
            "ship   1  src/base*.py", "ship   3  src/base*.py\ndrop   1  dist/**"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    assert await dc.mechanically_resolvable(
        str(work), set(CLASSIFICATION_ONLY), base_sha, branch_sha) is None


async def test_mechanically_resolvable_refuses_a_branch_side_added_rule(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace(
            "ship   1  src/base*.py", "ship   2  src/base*.py\nship   1  src/extra*.py"),
    )
    assert await dc.mechanically_resolvable(
        str(work), set(CLASSIFICATION_ONLY), base_sha, branch_sha) is None


async def test_mechanically_resolvable_refuses_a_branch_side_pattern_change(tmp_path):
    work, base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/basic*.py"),
    )
    assert await dc.mechanically_resolvable(
        str(work), set(CLASSIFICATION_ONLY), base_sha, branch_sha) is None


async def test_mechanically_resolvable_refuses_an_unresolvable_merge_base(tmp_path):
    work, _base_sha, branch_sha = _two_revs(
        tmp_path,
        base_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   3  src/base*.py"),
        branch_text=BASE_TEXT.replace("ship   1  src/base*.py", "ship   2  src/base*.py"),
    )
    assert await dc.mechanically_resolvable(
        str(work), set(CLASSIFICATION_ONLY), "0" * 40, branch_sha) is None


# --------------------------------------------------------------------------- #
# `take_ours_in_conflict_hunks`: only the HUNKS are taken from the branch      #
# --------------------------------------------------------------------------- #


def test_taking_ours_keeps_the_lines_main_merged_in_cleanly():
    """The other half of the same bug: `git checkout --ours` restores the
    branch's whole blob, dropping every rule line main gained since the fork
    and leaving main's files unclassified. Only the hunk may be taken."""
    merged = _TWO_WAY + "drop   1  docs/**\n"
    assert dc.take_ours_in_conflict_hunks(merged) == (
        "ship src/**\n"
        "ship   2  src/base*.py\n"
        "drop   1  tests/**\n"
        "drop   1  docs/**\n"
    )


def test_taking_ours_drops_the_diff3_base_section_too():
    assert dc.take_ours_in_conflict_hunks(_DIFF3) == (
        "ship src/**\n"
        "ship   2  src/base*.py\n"
        "drop   1  tests/**\n"
    )


def test_taking_ours_refuses_text_with_no_conflict_hunk():
    assert dc.take_ours_in_conflict_hunks(BASE_TEXT) is None


def test_taking_ours_refuses_an_unterminated_hunk():
    assert dc.take_ours_in_conflict_hunks(
        "ship src/**\n<<<<<<< HEAD\nship   2  src/base*.py\n") is None


def test_taking_ours_refuses_a_stray_marker():
    assert dc.take_ours_in_conflict_hunks(
        "ship src/**\n>>>>>>> main\n") is None
