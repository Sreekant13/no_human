"""Deterministic lint evidence for the reviewer (SCRUM-64).

ruff is not assumed to be installed in the test environment — every test that
exercises the subprocess call mocks ``subprocess.run`` rather than depending
on a real ruff binary.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from no_human.review.lint_evidence import (
    LintFinding,
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
    lint_block = "Evidence: ruff (deterministic static analysis of the changed files)\n  calc.py:3 F401 unused import"
    prompt = _build_review_prompt(t, "diff", "tests", "", lint_evidence=lint_block)
    assert "Evidence: ruff" in prompt
    assert "calc.py:3 F401 unused import" in prompt
