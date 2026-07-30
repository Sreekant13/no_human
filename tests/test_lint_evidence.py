"""Deterministic lint evidence for the reviewer (SCRUM-64).

ruff is not assumed to be installed in the test environment — every test that
exercises the subprocess call mocks ``subprocess.run`` rather than depending
on a real ruff binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from no_human.review.lint_evidence import (
    LintFinding,
    changed_line_numbers,
    collect_lint_evidence,
    format_lint_evidence,
    has_ruff_config,
)
from no_human.review.reviewer import _build_review_prompt
from no_human.core.task import Task


# --------------------------------------------------------------------------- #
# Config detection                                                             #
# --------------------------------------------------------------------------- #

def test_has_ruff_config_ruff_toml(tmp_path):
    (tmp_path / "ruff.toml").write_text("line-length = 100\n")
    assert has_ruff_config(tmp_path) is True


def test_has_ruff_config_dot_ruff_toml(tmp_path):
    (tmp_path / ".ruff.toml").write_text("line-length = 100\n")
    assert has_ruff_config(tmp_path) is True


def test_has_ruff_config_pyproject_tool_ruff(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[tool.ruff]\nline-length = 100\n"
    )
    assert has_ruff_config(tmp_path) is True


def test_has_ruff_config_pyproject_tool_ruff_subsection(tmp_path):
    """A repo that only configures a sub-table (e.g. [tool.ruff.lint]) without
    a bare [tool.ruff] header still counts as configured."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[tool.ruff.lint]\nselect = ['E']\n"
    )
    assert has_ruff_config(tmp_path) is True


def test_has_ruff_config_not_found_no_files(tmp_path):
    assert has_ruff_config(tmp_path) is False


def test_has_ruff_config_not_found_pyproject_without_ruff(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert has_ruff_config(tmp_path) is False


def test_has_ruff_config_survives_read_error(tmp_path):
    """A pyproject.toml that exists but can't be read (e.g. permission error
    surfaced as OSError) must never raise — just treated as unconfigured."""
    bad = tmp_path / "pyproject.toml"
    bad.mkdir()  # a directory named pyproject.toml -> read_text raises OSError
    assert has_ruff_config(tmp_path) is False


# --------------------------------------------------------------------------- #
# Changed-files-only scoping                                                   #
# --------------------------------------------------------------------------- #

def _configured_repo(tmp_path):
    (tmp_path / "ruff.toml").write_text("line-length = 100\n")
    (tmp_path / "changed.py").write_text("import os\n")
    (tmp_path / "untouched.py").write_text("import sys\n")
    (tmp_path / "notes.txt").write_text("hello\n")
    return tmp_path


def test_collect_lint_evidence_no_config_never_calls_subprocess(tmp_path):
    (tmp_path / "changed.py").write_text("import os\n")
    with patch("no_human.review.lint_evidence.subprocess.run") as run:
        result = collect_lint_evidence(tmp_path, ["changed.py"])
    assert result == []
    run.assert_not_called()


def test_collect_lint_evidence_only_lints_changed_python_files(tmp_path):
    repo = _configured_repo(tmp_path)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    with patch("no_human.review.lint_evidence.subprocess.run", side_effect=fake_run):
        collect_lint_evidence(
            repo, ["changed.py", "notes.txt", "../outside.py", "missing.py"]
        )
    # Only the changed, existing, in-repo .py file is passed to ruff — never
    # the non-python file, the path-traversal attempt, or a non-existent file,
    # and NEVER the whole repo (untouched.py is absent from the command).
    assert captured["cmd"][:4] == ["ruff", "check", "--output-format=json", "--no-fix"]
    assert captured["cmd"][4:] == ["changed.py"]
    assert "untouched.py" not in captured["cmd"]
    assert "notes.txt" not in captured["cmd"]


def test_collect_lint_evidence_no_changed_python_files_skips_ruff(tmp_path):
    repo = _configured_repo(tmp_path)
    with patch("no_human.review.lint_evidence.subprocess.run") as run:
        result = collect_lint_evidence(repo, ["notes.txt"])
    assert result == []
    run.assert_not_called()


def test_collect_lint_evidence_parses_findings(tmp_path):
    repo = _configured_repo(tmp_path)
    payload = json.dumps([
        {
            "filename": "changed.py",
            "code": "F401",
            "message": "`os` imported but unused",
            "location": {"row": 1, "column": 8},
        }
    ])
    with patch(
        "no_human.review.lint_evidence.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, stdout=payload, stderr=""),
    ):
        findings = collect_lint_evidence(repo, ["changed.py"])
    assert findings == [
        LintFinding(path="changed.py", line_number=1, error_code="F401",
                    message="`os` imported but unused")
    ]


# --------------------------------------------------------------------------- #
# Timeout / failure modes -> empty, never raises                              #
# --------------------------------------------------------------------------- #

def test_collect_lint_evidence_timeout_returns_empty(tmp_path):
    repo = _configured_repo(tmp_path)
    with patch(
        "no_human.review.lint_evidence.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ruff", timeout=30),
    ):
        assert collect_lint_evidence(repo, ["changed.py"]) == []


def test_collect_lint_evidence_missing_binary_returns_empty(tmp_path):
    repo = _configured_repo(tmp_path)
    with patch(
        "no_human.review.lint_evidence.subprocess.run",
        side_effect=FileNotFoundError("ruff not found"),
    ):
        assert collect_lint_evidence(repo, ["changed.py"]) == []


def test_collect_lint_evidence_bad_json_returns_empty(tmp_path):
    repo = _configured_repo(tmp_path)
    with patch(
        "no_human.review.lint_evidence.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
    ):
        assert collect_lint_evidence(repo, ["changed.py"]) == []


def test_collect_lint_evidence_nonzero_nonone_exit_returns_empty(tmp_path):
    """A ruff usage/config error (exit 2) is untrustworthy output — advisory
    means we drop it rather than surface a possibly-garbage result."""
    repo = _configured_repo(tmp_path)
    with patch(
        "no_human.review.lint_evidence.subprocess.run",
        return_value=subprocess.CompletedProcess([], 2, stdout="[]", stderr="bad config"),
    ):
        assert collect_lint_evidence(repo, ["changed.py"]) == []


def test_collect_lint_evidence_file_access_error_returns_empty(tmp_path):
    """A repo path that raises on config detection (unreadable file access)
    is handled gracefully."""
    (tmp_path / "pyproject.toml").mkdir()  # forces an OSError inside has_ruff_config
    assert collect_lint_evidence(tmp_path, ["changed.py"]) == []


# --------------------------------------------------------------------------- #
# Formatting / provenance                                                      #
# --------------------------------------------------------------------------- #

def test_format_lint_evidence_empty():
    assert format_lint_evidence([]) == ""


def test_format_lint_evidence_labels_provenance():
    block = format_lint_evidence([
        LintFinding(path="calc.py", line_number=3, error_code="F401",
                    message="`os` imported but unused"),
    ])
    assert block.startswith("Evidence: ruff")
    assert "calc.py:3" in block
    assert "F401" in block
    assert "imported but unused" in block


def test_format_lint_evidence_caps_finding_count():
    """A file with thousands of violations (vendored/generated code,
    select=['ALL']) must never blow the reviewer's context — cap at
    MAX_LINT_FINDINGS with a truncation tail line."""
    from no_human.review.lint_evidence import MAX_LINT_FINDINGS

    huge = [
        LintFinding(path="big.py", line_number=i, error_code="E501", message="line too long")
        for i in range(MAX_LINT_FINDINGS + 25)
    ]
    block = format_lint_evidence(huge)
    lines = block.splitlines()
    # header + MAX_LINT_FINDINGS finding lines + 1 truncation line
    assert len(lines) == 1 + MAX_LINT_FINDINGS + 1
    assert lines[-1] == "  ... truncated (25 more findings)"


def test_format_lint_evidence_caps_byte_size():
    """A modest number of findings with very long messages must still respect
    the byte cap, even though the count is under MAX_LINT_FINDINGS."""
    from no_human.review.lint_evidence import MAX_LINT_BYTES

    long_msg = "x" * 500
    many = [
        LintFinding(path="big.py", line_number=i, error_code="E501", message=long_msg)
        for i in range(40)
    ]
    block = format_lint_evidence(many)
    assert len(block.encode("utf-8")) <= MAX_LINT_BYTES + 200  # + slack for tail line
    assert "truncated" in block


def test_format_lint_evidence_under_cap_no_truncation_line():
    block = format_lint_evidence([
        LintFinding(path="calc.py", line_number=3, error_code="F401",
                    message="`os` imported but unused"),
    ])
    assert "truncated" not in block


def test_collect_lint_evidence_sorts_findings_deterministically(tmp_path):
    """Ordering must be enforced by us, not inherited from ruff's own
    (unordered-across-files) output order."""
    repo = _configured_repo(tmp_path)
    payload = json.dumps([
        {"filename": "changed.py", "code": "F401", "message": "z",
         "location": {"row": 5}},
        {"filename": "changed.py", "code": "E501", "message": "a",
         "location": {"row": 1}},
        {"filename": "aaa.py", "code": "F401", "message": "m",
         "location": {"row": 2}},
    ])
    with patch(
        "no_human.review.lint_evidence.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, stdout=payload, stderr=""),
    ):
        findings = collect_lint_evidence(repo, ["changed.py"])
    assert [(f.path, f.line_number, f.error_code) for f in findings] == [
        ("aaa.py", 2, "F401"),
        ("changed.py", 1, "E501"),
        ("changed.py", 5, "F401"),
    ]


# --------------------------------------------------------------------------- #
# Context inclusion / exclusion in the reviewer prompt                        #
# --------------------------------------------------------------------------- #

def test_review_prompt_excludes_lint_block_when_empty():
    t = Task.new("Fix bug")
    prompt = _build_review_prompt(t, "diff", "tests", "", lint_evidence="")
    assert "Evidence: ruff" not in prompt


def test_review_prompt_byte_identical_with_empty_lint_evidence():
    """A prompt built with lint_evidence='' must be BYTE-IDENTICAL to one
    built without the kwarg at all — a stray whitespace injection in the
    empty path would slip past a substring-only check."""
    t = Task.new("Fix bug")
    prompt_default = _build_review_prompt(t, "diff", "tests", "")
    prompt_explicit_empty = _build_review_prompt(t, "diff", "tests", "", lint_evidence="")
    assert prompt_default == prompt_explicit_empty


def test_review_prompt_includes_lint_block_when_present():
    t = Task.new("Fix bug")
    lint_block = "Evidence: ruff (deterministic static analysis, scoped to the lines this diff changed)\n  calc.py:3 F401 unused import"
    prompt = _build_review_prompt(t, "diff", "tests", "", lint_evidence=lint_block)
    assert "Evidence: ruff" in prompt
    assert "calc.py:3 F401 unused import" in prompt


# --------------------------------------------------------------------------- #
# Changed-LINES scoping (real git repos, real diffs)                           #
# --------------------------------------------------------------------------- #
#
# These tests build a real git repo and run real `git diff` — the hunk parsing
# is exercised against git's actual output, not a hand-written fixture of what
# we imagine git prints.

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV,
        capture_output=True, text=True, check=True,
    )


# A file whose line 1 violation (unused `import os`) is PRE-EXISTING and whose
# appended last line carries the violation the diff actually introduced.
_CALC_V1 = "import os\n\n\ndef total(values):\n    return sum(values)\n"
_CALC_V2 = _CALC_V1 + "import sys\n"
_TOUCHED_LINE = 6  # the appended `import sys`


def _git_repo_with_diff(tmp_path):
    """A committed repo whose HEAD~1..HEAD diff touches ONLY line 6 of calc.py."""
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "ruff.toml").write_text("line-length = 100\n")
    (tmp_path / "calc.py").write_text(_CALC_V1)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "calc.py").write_text(_CALC_V2)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "append an unused import")
    return tmp_path


def _fake_ruff_only(payload, returncode=1):
    """Intercept the `ruff` subprocess ONLY — git calls still run for real."""
    real_run = subprocess.run

    def _run(cmd, **kwargs):
        if cmd and cmd[0] == "ruff":
            return subprocess.CompletedProcess(cmd, returncode, stdout=payload, stderr="")
        return real_run(cmd, **kwargs)

    return _run


def _abs_ruff_payload(repo, rows):
    """What ruff really emits: an ABSOLUTE, symlink-resolved `filename`.
    Verified against ruff 0.14.0 — a relative path argument still produces an
    absolute filename in the JSON."""
    return json.dumps([
        {
            "filename": os.path.realpath(str(Path(repo) / "calc.py")),
            "code": code,
            "message": msg,
            "location": {"row": row, "column": 1},
        }
        for row, code, msg in rows
    ])


def test_changed_line_numbers_maps_added_lines(tmp_path):
    repo = _git_repo_with_diff(tmp_path)
    assert changed_line_numbers(repo, "HEAD~1", "HEAD") == {"calc.py": {_TOUCHED_LINE}}


def test_changed_line_numbers_new_file_is_all_lines(tmp_path):
    repo = _git_repo_with_diff(tmp_path)
    (repo / "brand_new.py").write_text("a = 1\nb = 2\nc = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add file")
    assert changed_line_numbers(repo, "HEAD~1", "HEAD") == {"brand_new.py": {1, 2, 3}}


def test_changed_line_numbers_pure_deletion_contributes_no_after_lines(tmp_path):
    """`@@ -4,2 +3,0 @@` — a deletion-only hunk adds no after-state lines. The
    path is still reported (with an empty set) so callers can tell 'this file
    changed but no line was added' apart from 'git told us nothing'."""
    repo = _git_repo_with_diff(tmp_path)
    (repo / "calc.py").write_text("import os\n\n\ndef total(values):\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete lines")
    assert changed_line_numbers(repo, "HEAD~1", "HEAD") == {"calc.py": set()}


def test_changed_line_numbers_multiline_and_multifile(tmp_path):
    repo = _git_repo_with_diff(tmp_path)
    (repo / "calc.py").write_text("import os\n\n\ndef total(values):\n    x = 1\n    y = 2\n    return x + y\n")
    (repo / "other.py").write_text("z = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "edit both")
    got = changed_line_numbers(repo, "HEAD~1", "HEAD")
    assert got["other.py"] == {1}
    # lines 5-7 replaced the old lines 5-6
    assert got["calc.py"] == {5, 6, 7}


def test_changed_line_numbers_handles_quoted_paths(tmp_path):
    """git quotes paths with backslashes/non-ASCII in `+++ b/...` headers."""
    repo = _git_repo_with_diff(tmp_path)
    (repo / "café naïve.py").write_text("q = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unicode path")
    assert changed_line_numbers(repo, "HEAD~1", "HEAD") == {"café naïve.py": {1}}


def test_changed_line_numbers_bad_ref_fails_open_to_empty(tmp_path):
    """Never raises into the review gate; an unusable ref yields {}."""
    repo = _git_repo_with_diff(tmp_path)
    assert changed_line_numbers(repo, "no-such-ref", "HEAD") == {}


def test_changed_line_numbers_not_a_repo_fails_open_to_empty(tmp_path):
    assert changed_line_numbers(tmp_path, "HEAD~1", "HEAD") == {}


def test_collect_lint_evidence_drops_findings_on_untouched_lines(tmp_path):
    """THE point of this module's scoping: a pre-existing violation on a line
    the diff never touched must not be presented to the reviewer as evidence
    about the agent's diff."""
    repo = _git_repo_with_diff(tmp_path)
    payload = _abs_ruff_payload(repo, [
        (1, "F401", "`os` imported but unused"),          # pre-existing, untouched
        (_TOUCHED_LINE, "F401", "`sys` imported but unused"),  # introduced here
    ])
    with patch("no_human.review.lint_evidence.subprocess.run",
               side_effect=_fake_ruff_only(payload)):
        findings = collect_lint_evidence(
            repo, ["calc.py"], before_ref="HEAD~1", after_ref="HEAD")
    assert [(f.path, f.line_number) for f in findings] == [("calc.py", _TOUCHED_LINE)]


def test_collect_lint_evidence_normalizes_absolute_ruff_paths(tmp_path):
    """ruff reports an ABSOLUTE filename. Rendering it verbatim leaks the
    operator's home directory into the reviewer prompt — and makes the
    changed-line filter silently drop everything."""
    repo = _git_repo_with_diff(tmp_path)
    payload = _abs_ruff_payload(repo, [(_TOUCHED_LINE, "F401", "`sys` imported but unused")])
    with patch("no_human.review.lint_evidence.subprocess.run",
               side_effect=_fake_ruff_only(payload)):
        findings = collect_lint_evidence(
            repo, ["calc.py"], before_ref="HEAD~1", after_ref="HEAD")
    assert [f.path for f in findings] == ["calc.py"]
    block = format_lint_evidence(findings)
    assert str(repo) not in block
    assert os.path.realpath(str(repo)) not in block
    assert "calc.py:6" in block


def test_collect_lint_evidence_without_refs_keeps_all_findings(tmp_path):
    """Back-compatible: no refs supplied -> whole-file behaviour, unchanged."""
    repo = _git_repo_with_diff(tmp_path)
    payload = _abs_ruff_payload(repo, [
        (1, "F401", "`os` imported but unused"),
        (_TOUCHED_LINE, "F401", "`sys` imported but unused"),
    ])
    with patch("no_human.review.lint_evidence.subprocess.run",
               side_effect=_fake_ruff_only(payload)):
        findings = collect_lint_evidence(repo, ["calc.py"])
    assert [f.line_number for f in findings] == [1, _TOUCHED_LINE]


def test_collect_lint_evidence_git_failure_fails_open_to_whole_file(tmp_path):
    """Fail OPEN: if the changed-line map can't be computed we degrade to the
    previous behaviour (all findings for changed files), never to silence."""
    repo = _git_repo_with_diff(tmp_path)
    payload = _abs_ruff_payload(repo, [
        (1, "F401", "`os` imported but unused"),
        (_TOUCHED_LINE, "F401", "`sys` imported but unused"),
    ])
    with patch("no_human.review.lint_evidence.subprocess.run",
               side_effect=_fake_ruff_only(payload)):
        findings = collect_lint_evidence(
            repo, ["calc.py"], before_ref="bogus-ref", after_ref="HEAD")
    assert [f.line_number for f in findings] == [1, _TOUCHED_LINE]


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_collect_lint_evidence_end_to_end_with_real_ruff(tmp_path):
    """No mocks at all: real git, real ruff. Only runs where ruff is installed
    (it is deliberately not a dependency of this project)."""
    repo = _git_repo_with_diff(tmp_path)
    findings = collect_lint_evidence(
        repo, ["calc.py"], before_ref="HEAD~1", after_ref="HEAD")
    assert findings, "real ruff should flag the appended unused import"
    assert {f.path for f in findings} == {"calc.py"}
    assert {f.line_number for f in findings} == {_TOUCHED_LINE}


def test_changed_line_numbers_ignores_content_masquerading_as_a_header(tmp_path):
    """An ADDED line whose content is literally `++ b/victim.py` is rendered by
    git as `+++ b/victim.py` at column 0 — indistinguishable from a file header
    unless the parser tracks the header region.

    The payload must be the bare text: a commented `# ++ b/victim.py` renders as
    `+# ++ ...`, never matches, and makes this test vacuous.

    Read as a header it retargets `current`, so the NEXT hunk's lines are
    credited to victim.py: calc.py silently loses coverage (its real violations
    stop being reported) and victim.py gains lines the diff never touched.
    """
    repo = _git_repo_with_diff(tmp_path)
    (repo / "victim.py").write_text("safe = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "victim")
    # Payload on line 1, a genuine change at the end -> two hunks, so a
    # retargeted `current` visibly steals the second one.
    (repo / "calc.py").write_text("++ b/victim.py\n" + _CALC_V2 + "z = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "sneaky content")

    raw = subprocess.run(
        ["git", "--no-pager", "diff", "--no-color", "--unified=0",
         "HEAD~1..HEAD", "--", "calc.py"],
        cwd=repo, env=_GIT_ENV, capture_output=True, text=True, check=True,
    ).stdout
    # Guard the guard: if git ever stops rendering this at column 0 the attack
    # is gone and this test would silently stop testing anything.
    assert "\n+++ b/victim.py\n" in raw, raw

    got = changed_line_numbers(repo, "HEAD~1", "HEAD")
    assert got == {"calc.py": {1, 8}}, got
    assert "victim.py" not in got


def test_parse_changed_lines_handles_hand_written_hunk_forms():
    """The three `@@` shapes, straight from the format: omitted count (1 line),
    explicit count, and a zero count (deletion) that adds nothing."""
    from no_human.review.lint_evidence import parse_changed_lines

    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -3 +3 @@\n-old\n+new\n"
        "@@ -10,0 +11,3 @@\n+x\n+y\n+z\n"
        "@@ -20,2 +22,0 @@\n-gone\n-gone\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    assert parse_changed_lines(diff) == {"a.py": {3, 11, 12, 13}}


def test_changed_line_numbers_never_runs_repo_configured_diff_programs(tmp_path):
    """`.gitattributes` is a TRACKED file that can arrive in the diff under
    review, and `.git/config` is writable by the coder agent — so both diff
    driver hooks are attacker-controlled here.

    Observes the side effect (did the program run?), not the command line.
    `--no-ext-diff` alone is NOT enough: it leaves `textconv` live.
    """
    repo = _git_repo_with_diff(tmp_path)
    ext_sentinel = tmp_path / "RAN_EXT_DIFF"
    conv_sentinel = tmp_path / "RAN_TEXTCONV"
    (repo / ".gitattributes").write_text("*.py diff=conv\n*.txt diff=ext\n")
    (repo / "notes.txt").write_text("before\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "arm the drivers")
    (repo / "calc.py").write_text(_CALC_V2 + "tail = 1\n")
    (repo / "notes.txt").write_text("after\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "payload")
    _git(repo, "config", "diff.conv.textconv", f"sh -c 'touch {conv_sentinel}; cat'")
    _git(repo, "config", "diff.ext.command", f"sh -c 'touch {ext_sentinel}'")

    got = changed_line_numbers(repo, "HEAD~1", "HEAD")

    assert not conv_sentinel.exists(), "diff.<driver>.textconv executed"
    assert not ext_sentinel.exists(), "diff.<driver>.command executed"
    assert got["calc.py"] == {7}, got


def test_changed_line_numbers_pins_header_prefixes(tmp_path):
    """`diff.dstPrefix` is repo-controlled. Unpinned it renames every key in
    the map, nothing matches a ruff finding, and the evidence disappears with
    no error anywhere — the silent-no-op failure mode this module must not have.
    """
    repo = _git_repo_with_diff(tmp_path)
    _git(repo, "config", "diff.dstPrefix", "dst/")
    _git(repo, "config", "diff.srcPrefix", "src/")
    assert changed_line_numbers(repo, "HEAD~1", "HEAD") == {"calc.py": {_TOUCHED_LINE}}


def test_collect_lint_evidence_survives_repo_controlled_diff_prefix(tmp_path):
    """The end the user feels: with a hostile `diff.dstPrefix`, real evidence
    on a touched line must still reach the reviewer."""
    repo = _git_repo_with_diff(tmp_path)
    _git(repo, "config", "diff.dstPrefix", "dst/")
    payload = _abs_ruff_payload(repo, [
        (1, "F401", "`os` imported but unused"),
        (_TOUCHED_LINE, "F401", "`sys` imported but unused"),
    ])
    with patch("no_human.review.lint_evidence.subprocess.run",
               side_effect=_fake_ruff_only(payload)):
        findings = collect_lint_evidence(
            repo, ["calc.py"], before_ref="HEAD~1", after_ref="HEAD")
    assert [(f.path, f.line_number) for f in findings] == [("calc.py", _TOUCHED_LINE)]


def test_filter_drops_findings_for_a_pure_rename_in_a_mixed_diff(tmp_path):
    """Pins the measured mechanism the docstring describes: a pure rename emits
    NO `+++` header, so it gets no map entry — and with another file supplying
    entries the map is non-empty, so those findings are DROPPED, not kept."""
    from no_human.review.lint_evidence import _filter_to_changed_lines

    repo = _git_repo_with_diff(tmp_path)
    _git(repo, "mv", "calc.py", "renamed.py")
    (repo / "other.py").write_text("k = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename plus an unrelated edit")

    got = changed_line_numbers(repo, "HEAD~1", "HEAD")
    assert got == {"other.py": {1}}, got  # no entry at all for renamed.py

    stale = [LintFinding(path="renamed.py", line_number=1,
                         error_code="F401", message="`os` imported but unused")]
    assert _filter_to_changed_lines(stale, got) == []
    # ... but the same finding survives when the map is empty (fail open).
    assert _filter_to_changed_lines(stale, {}) == stale
