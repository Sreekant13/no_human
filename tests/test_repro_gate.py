"""The repro gate proves fails-before / passes-after — both directions, really run."""

from __future__ import annotations

import json
import subprocess

import pytest

from no_human.testing.repro_gate import MANIFEST, read_manifest, run_repro_gate


@pytest.fixture
def repo(tmp_path):
    """base commit: buggy add(); attempt tree: fixed add() + a repro test."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_repro.py").write_text(
        "from calc import add\n\ndef test_add_fixed():\n    assert add(1, 2) == 3\n"
    )
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(
        json.dumps({"tests": ["test_repro.py::test_add_fixed"]})
    )
    return tmp_path


def test_no_manifest_waives_loudly(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    r = run_repro_gate(tmp_path, "HEAD")
    assert r.verdict == "waived"
    assert "no" in r.reasons[0] and MANIFEST in r.reasons[0]


def test_a_real_bugfix_passes_both_directions(repo):
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "pass", r.reasons
    assert r.tests == ["test_repro.py::test_add_fixed"]


def test_a_test_that_passes_on_the_base_fails_the_gate(repo):
    """A 'repro' that reproduces on healthy code demonstrates nothing."""
    (repo / "test_repro.py").write_text(
        "def test_add_fixed():\n    assert True\n"
    )
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "fail"
    assert "fails-before" in r.reasons[0]


def test_a_test_failing_on_the_attempt_tree_fails_the_gate(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # still buggy\n")
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "fail"
    assert "passes-after" in r.reasons[0]


def test_a_deleted_declared_test_fails_the_gate(repo):
    """Delete-the-test defense: a listed repro test may not vanish."""
    (repo / "test_repro.py").unlink()
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "fail"
    assert "may not be deleted" in r.reasons[0]


def test_a_bad_base_ref_is_an_error_never_a_fail(repo):
    r = run_repro_gate(repo, "no-such-ref")
    assert r.verdict == "error"


def test_manifest_reader_tolerates_garbage(tmp_path):
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text("{not json")
    assert read_manifest(tmp_path) == []
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": "not-a-list"}))
    assert read_manifest(tmp_path) == []
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": [" a.py::t ", ""]}))
    assert read_manifest(tmp_path) == ["a.py::t"]
