"""Wiring evidence: deterministic, Python-only, evidence — never a verdict.

Feeds the reviewer's goal-reachability judgment with the raw material of the
"implemented but never called by the production path" class. It must fail
toward silence: any error yields an empty result, and the rendered block
states its own ceilings (static, Python-only) so the prose never exceeds the
mechanism.
"""

import subprocess

import pytest

from no_human.review.wiring_evidence import (
    collect_wiring_evidence,
    format_wiring_evidence,
)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "u@e.com")
    _git(tmp_path, "config", "user.name", "u")
    (tmp_path / "app.py").write_text("def handle(req):\n    return {}\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("import app\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    return tmp_path


def _commit(repo, msg="change"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def test_a_new_unreferenced_symbol_is_listed(repo):
    (repo / "rates.py").write_text("def volumetric(dims):\n    return 1.0\n")
    (repo / "tests" / "test_rates.py").write_text(
        "from rates import volumetric\n\ndef test_v():\n"
        "    assert volumetric(None) == 1.0\n")
    _commit(repo)
    out = collect_wiring_evidence(repo, "HEAD~1", "HEAD")
    assert out == [("rates.py", "volumetric")], (
        "a test-only reference is not production wiring")


def test_a_production_referenced_symbol_is_not_listed(repo):
    (repo / "rates.py").write_text("def volumetric(dims):\n    return 1.0\n")
    (repo / "app.py").write_text(
        "from rates import volumetric\n\ndef handle(req):\n"
        "    return {'w': volumetric(req)}\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_a_symbol_used_only_inside_its_own_file_is_still_listed(repo):
    """The search is for references OUTSIDE the defining file — the block's
    header states exactly that, so this behaviour is the documented one."""
    (repo / "rates.py").write_text(
        "def _mul(a, b):\n    return a * b\n\n"
        "def volumetric(dims):\n    return _mul(1, 1)\n")
    _commit(repo)
    out = collect_wiring_evidence(repo, "HEAD~1", "HEAD")
    assert ("rates.py", "_mul") in out and ("rates.py", "volumetric") in out


def test_non_python_diffs_contribute_nothing_and_do_not_crash(repo):
    (repo / "notes.md").write_text("# nothing\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_changed_test_files_are_ignored_as_definers(repo):
    (repo / "tests" / "test_new.py").write_text("def helper():\n    pass\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_bad_refs_fail_to_empty_never_raise(repo):
    assert collect_wiring_evidence(repo, "no-such-ref", "HEAD") == []


def test_unparseable_python_fails_to_empty(repo):
    (repo / "broken.py").write_text("def (\n")
    _commit(repo)
    assert collect_wiring_evidence(repo, "HEAD~1", "HEAD") == []


def test_format_block_is_labeled_hedged_and_empty_on_no_findings():
    assert format_wiring_evidence([]) == ""
    block = format_wiring_evidence([("rates.py", "volumetric")])
    assert block.startswith("WIRING EVIDENCE (deterministic, static, "
                            "Python-only")
    assert "rates.py: volumetric" in block
    assert "Evidence, not a verdict" in block
