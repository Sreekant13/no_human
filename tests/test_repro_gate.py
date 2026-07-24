"""The repro gate proves fails-before / passes-after — both directions, really run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from no_human.testing import repro_gate
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


# --------------------------------------------------------------------------- #
# Frozen (PyInstaller) build: sys.executable is the nh binary, not python.     #
# The gate must resolve a real interpreter — never shell the frozen binary and #
# read its non-zero exit as a test verdict (a confident, false "fail").        #
# --------------------------------------------------------------------------- #

def test_pytest_python_uses_sys_executable_when_not_frozen():
    assert not getattr(repro_gate.sys, "frozen", False)
    assert repro_gate._pytest_python(Path(".")) == repro_gate.sys.executable


def test_pytest_python_avoids_the_frozen_binary(tmp_path, monkeypatch):
    """Frozen build: resolve the target repo's venv python, NOT sys.executable
    (which is the nh binary)."""
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("")  # _venv_bin only checks existence
    got = repro_gate._pytest_python(tmp_path)
    assert got == str(venv_bin / "python")
    assert got != repro_gate.sys.executable


def test_pytest_python_is_none_when_frozen_and_nothing_resolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    monkeypatch.setattr(repro_gate.shutil, "which", lambda name: None)
    assert repro_gate._pytest_python(tmp_path) is None


def test_frozen_build_with_no_interpreter_fails_closed_to_error(repo, monkeypatch):
    """The crux of the bug: a real bugfix must NEVER be verdicted 'fail' just
    because the frozen build can't find an interpreter. It fails closed to
    'error' (advisory) and names what it could not verify."""
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    monkeypatch.setattr(repro_gate.shutil, "which", lambda name: None)
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "error", r.reasons
    assert "no Python interpreter" in r.reasons[0]
    assert r.tests == ["test_repro.py::test_add_fixed"]  # honest, not silent


def test_frozen_build_still_verdicts_correctly_with_a_real_interpreter(repo, monkeypatch):
    """With a real interpreter resolved (PATH fallback), a frozen build produces
    the SAME correct verdict a normal install would — proving the fix restored
    the gate rather than merely muting it."""
    real = repro_gate.sys.executable
    monkeypatch.setattr(repro_gate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(repro_gate.sys, "executable", "/frozen/nh-binary")
    monkeypatch.setattr(
        repro_gate.shutil, "which",
        lambda name: real if name in ("python3", "python") else None)
    r = run_repro_gate(repo, "HEAD")
    assert r.verdict == "pass", r.reasons


def test_pytest_not_importable_is_an_error_never_a_fail():
    """A fallback interpreter without pytest must read as an environment error,
    not a test failure — otherwise every gate false-fails."""
    ran, ok, out = repro_gate._run_pytest(
        ["x.py::t"], Path("."), {}, "/does/not/exist/python")
    assert ran is False  # OSError → could not launch
    # And the explicit 'No module named pytest' signature is caught too.
    import types
    fake = types.SimpleNamespace(
        stdout="", stderr="/usr/bin/python: No module named pytest\n",
        returncode=1)
    def fake_run(*a, **k):
        return fake
    orig = repro_gate.subprocess.run
    try:
        repro_gate.subprocess.run = fake_run
        ran2, ok2, out2 = repro_gate._run_pytest(["x.py::t"], Path("."), {}, "python")
    finally:
        repro_gate.subprocess.run = orig
    assert ran2 is False and ok2 is False


def test_manifest_reader_tolerates_garbage(tmp_path):
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text("{not json")
    assert read_manifest(tmp_path) == []
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": "not-a-list"}))
    assert read_manifest(tmp_path) == []
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": [" a.py::t ", ""]}))
    assert read_manifest(tmp_path) == ["a.py::t"]
