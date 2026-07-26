"""The repro gate proves fails-before / passes-after — both directions, really run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.profile import ProjectProfile
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


# --------------------------------------------------------------------------- #
# SCRUM-65: non-Python repos route the repro run through profile.test_cmd —   #
# pytest-only silently skipped the fails-before/passes-after guarantee for    #
# JS/TS/Go bugfixes. A tiny Python "runner" stands in for a real jest/go test #
# binary so these tests need no non-Python toolchain in CI; it only proves   #
# the WIRING (which command runs, with which args), not a real JS runner.     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def js_repo(tmp_path):
    """base commit: buggy lib.js; attempt tree: fixed lib.js + a repro test.

    ``run_tests.py`` (committed, so it exists in both the attempt tree and the
    base worktree) stands in for the repo's real test_cmd: for each test-file
    argument it evaluates that file's content (a Python boolean expression,
    read relative to the test file's own directory) and exits 0 only if every
    expression is true — a minimal stand-in for a real jest/go test binary
    that keeps the pass/fail decision entirely inside the (copyable) test
    file, exactly like the pytest fixture above does with test_repro.py.
    """
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "run_tests.py").write_text(
        "import os, pathlib, sys\n"
        "def ok(test_file):\n"
        "    p = pathlib.Path(test_file).resolve()\n"
        "    expr = p.read_text().strip()\n"
        "    old = os.getcwd()\n"
        "    os.chdir(p.parent)\n"
        "    try:\n"
        "        return bool(eval(expr))\n"
        "    finally:\n"
        "        os.chdir(old)\n"
        "sys.exit(0 if all(ok(f) for f in sys.argv[1:]) else 1)\n"
    )
    (tmp_path / "lib.js").write_text("module.exports = 'buggy';\n")
    (tmp_path / "lib.test.js").write_text("'fixed' in open('lib.js').read()\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "lib.js").write_text("module.exports = 'fixed';\n")
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(
        json.dumps({"tests": ["lib.test.js"]})
    )
    return tmp_path


def _node_profile(repo: Path, test_cmd: str) -> ProjectProfile:
    return ProjectProfile(repo_path=str(repo), ecosystem="node", test_cmd=test_cmd)


def test_non_python_bugfix_routes_through_profile_test_cmd(js_repo):
    """Language routing: a non-Python profile drives the repro run via its
    test_cmd, and the fails-before/passes-after proof still holds end to end."""
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "pass", r.reasons
    assert r.tests == ["lib.test.js"]


def test_non_python_test_cmd_only_runs_the_touched_test_files(js_repo, monkeypatch):
    """Test file targeting: the invoked command receives exactly the manifest's
    test files as args — not a bare/full-suite invocation."""
    recorder = js_repo / "argv.json"
    (js_repo / "run_tests.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(__file__).parent.joinpath('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "sys.exit(1)\n"  # fail so we never reach the git-worktree step
    )
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "fail"
    assert json.loads(recorder.read_text()) == ["lib.test.js"]


def test_non_python_bugfix_fails_before_check_still_holds(js_repo):
    """A 'repro' that already passes on the buggy base demonstrates nothing,
    same as the pytest path — the non-Python route must not weaken this."""
    (js_repo / "lib.test.js").write_text("True\n")  # always green, source-independent
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "fail"
    assert "fails-before" in r.reasons[0]


def test_missing_test_cmd_is_an_advisory_error_not_a_verdict(js_repo):
    """Advisory fallback: no test_cmd on the profile must never be read as a
    pass or a fail."""
    profile = _node_profile(js_repo, "")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error"
    assert "test_cmd" in r.reasons[0]
    assert r.tests == ["lib.test.js"]  # honest, not silent


def test_unparseable_test_cmd_is_an_advisory_error(js_repo):
    """Advisory fallback: a test_cmd shlex can't parse (mismatched quote)
    fails closed to 'error', never guesses a verdict."""
    profile = _node_profile(js_repo, "npm test 'unterminated")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error"
    assert "test_cmd" in r.reasons[0]


def test_missing_profile_defaults_to_python_pytest(repo):
    """Regression pin: no profile at all keeps the historical pytest-only
    behaviour byte-for-byte."""
    r = run_repro_gate(repo, "HEAD", None)
    assert r.verdict == "pass", r.reasons


def test_python_profile_still_uses_pytest_and_ignores_test_cmd(repo):
    """Regression pin: a profile that declares a python ecosystem stays on the
    pytest path even when test_cmd is garbage — proves the routing decision is
    keyed off the ecosystem, not "profile present or not"."""
    profile = ProjectProfile(
        repo_path=str(repo), ecosystem="python-pytest",
        test_cmd="this is not a real command !!",
    )
    r = run_repro_gate(repo, "HEAD", profile)
    assert r.verdict == "pass", r.reasons


def test_parse_test_cmd_rejects_missing_blank_and_unparseable():
    assert repro_gate._parse_test_cmd(None) is None
    assert repro_gate._parse_test_cmd("") is None
    assert repro_gate._parse_test_cmd("   ") is None
    assert repro_gate._parse_test_cmd("npm test 'unterminated") is None
    assert repro_gate._parse_test_cmd("npm test --silent") == ["npm", "test", "--silent"]


def test_nonexistent_runner_binary_is_advisory_error_not_pass(js_repo):
    """Mutation-test pin (SCRUM-65 review item 2): an OSError from a missing
    runner binary must classify as 'error' end to end, never a silent pass —
    the ``_run_test_cmd`` except-OSError branch is easy to accidentally
    mutate into a 'ran, failed' result without any test noticing."""
    profile = _node_profile(js_repo, "/no/such/binary-xyz --run")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error", r.reasons
    assert "could not run test_cmd" in r.reasons[0]


def test_runner_exiting_command_not_found_is_advisory_error(js_repo):
    """A runner that exits 126/127 (shell 'command not found' / 'not
    executable') is an environment failure, never a genuine fails-before
    verdict — SCRUM-65 review item 1."""
    (js_repo / "run_tests.py").write_text("import sys\nsys.exit(127)\n")
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error", r.reasons
    assert "127" in r.reasons[0]


def test_non_python_missing_uncommitted_dep_is_advisory_error_not_pass(tmp_path):
    """Realistic fixture (SCRUM-65 review item 3): the committed test_cmd
    runner imports an UNCOMMITTED helper module — standing in for
    node_modules. It is present when the 'after' run executes in repo_path,
    but absent from the fresh git-worktree checkout used for the 'before'
    run, so that run fails for an ENVIRONMENT reason unrelated to the
    bugfix. Before the sanity-pre-run fix this produced a false 'pass'
    ('fails-before proven' from a runner that never really ran); it must
    instead be an advisory error."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "run_tests.py").write_text(
        "import helper\n"  # uncommitted dependency — stands in for node_modules
        "import sys\n"
        "sys.exit(0 if helper.check(sys.argv[1:]) else 1)\n"
    )
    (tmp_path / "lib.js").write_text("module.exports = 'buggy';\n")
    (tmp_path / "lib.test.js").write_text("'fixed' in open('lib.js').read()\n")
    git("add", "-A")
    git("commit", "-m", "base (buggy)")
    (tmp_path / "lib.js").write_text("module.exports = 'fixed';\n")
    (tmp_path / "helper.py").write_text(  # NEVER committed
        "import pathlib\n"
        "def check(files):\n"
        "    return all(eval(pathlib.Path(f).read_text().strip()) for f in files)\n"
    )
    (tmp_path / ".no_human").mkdir()
    (tmp_path / MANIFEST).write_text(json.dumps({"tests": ["lib.test.js"]}))
    profile = _node_profile(tmp_path, f"{sys.executable} run_tests.py")
    r = run_repro_gate(tmp_path, "HEAD", profile)
    assert r.verdict == "error", r.reasons
    assert "runner" in r.reasons[0].lower()


def test_pytest_style_node_id_stripped_before_reaching_foreign_runner(js_repo, monkeypatch):
    """SCRUM-65 review item 4: a manifest entry with a pytest-style node id
    (``path::case``) must reach a non-Python runner as a bare file path, not
    verbatim — a raw '::' means nothing to jest/mocha/go test."""
    (js_repo / MANIFEST).write_text(
        json.dumps({"tests": ["lib.test.js::whatever"]})
    )
    recorder = js_repo / "argv.json"
    (js_repo / "run_tests.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(__file__).parent.joinpath('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "sys.exit(1)\n"
    )
    profile = _node_profile(js_repo, f"{sys.executable} run_tests.py")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "fail"
    assert json.loads(recorder.read_text()) == ["lib.test.js"]


def test_unsubstituted_placeholder_in_test_cmd_is_advisory_error():
    """SCRUM-65 review item 4: an un-interpolated template token in test_cmd
    (e.g. '{test_file}') must fail closed at parse time rather than reach
    the runner literally."""
    assert repro_gate._parse_test_cmd("run-tests {test_file}") is None


def test_unsubstituted_placeholder_in_test_cmd_is_advisory_error_e2e(js_repo):
    profile = _node_profile(js_repo, "run-tests {test_file}")
    r = run_repro_gate(js_repo, "HEAD", profile)
    assert r.verdict == "error"
    assert "test_cmd" in r.reasons[0]


def test_is_python_profile_routing():
    assert repro_gate._is_python_profile(None) is True
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="")) is True
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="python-pytest")) is True
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="node")) is False
    assert repro_gate._is_python_profile(
        ProjectProfile(repo_path=".", ecosystem="maven")) is False
