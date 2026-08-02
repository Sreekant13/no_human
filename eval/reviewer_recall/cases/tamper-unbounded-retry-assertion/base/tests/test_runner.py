"""Tests for local test-command detection (testing/runner.py)."""

from pathlib import Path

from no_human.testing.runner import (
    detect_command,
    _parse_vitest,
    _parse_jest,
    _parse_test_output,
    _looks_like_pytest,
    run_lint_on_changed,
    _lint_supports_file_args,
)


def test_uv_pyproject_uses_uv_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "uv.lock").write_text("")
    assert detect_command(tmp_path) == "uv run pytest -q"


def test_plain_pytest_project(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert detect_command(tmp_path) == "pytest -q"


def test_polyglot_pytest_over_maven(tmp_path):
    # The polyglot shape that motivated this: a root pom.xml (Java build) but the
    # real test suite is pytest under src/tests with a root pytest.ini. Must NOT
    # be misrouted to `mvn` (the shadow-validation finding).
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = src/tests\n")
    (tmp_path / "src" / "tests").mkdir(parents=True)
    assert detect_command(tmp_path) == "pytest -q"


def test_requirements_with_pytest(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "requirements-dev.txt").write_text("pytest>=8\nrich\n")
    assert detect_command(tmp_path) == "pytest -q"


def test_pure_maven_still_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_command(tmp_path) == "mvn -q test"


def test_node_project(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_command(tmp_path) == "npm test --silent"


def test_unknown_returns_none(tmp_path):
    assert detect_command(tmp_path) is None


# --------------------------------------------------------------------------- #
# Regression: Bug 1 — Node repo with tests/ dir must NOT detect as pytest     #
# --------------------------------------------------------------------------- #


def test_node_with_tests_dir_not_pytest(tmp_path):
    """Node-repo shape: package.json + tests/fixtures/ (no .py files).
    Must detect as npm, NOT pytest."""
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}')
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "data.json").write_text("{}")
    assert not _looks_like_pytest(tmp_path)
    assert detect_command(tmp_path) == "npm test --silent"


def test_node_with_pyproject_not_pytest(tmp_path):
    """Node repo that also has pyproject.toml (e.g. for a Python linter tool).
    package.json should win."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length=88\n")
    assert not _looks_like_pytest(tmp_path)
    assert detect_command(tmp_path) == "npm test --silent"


def test_tests_dir_with_python_files_is_pytest(tmp_path):
    """A tests/ dir WITH .py files should still detect as pytest."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_x(): pass")
    assert _looks_like_pytest(tmp_path)
    assert detect_command(tmp_path) == "pytest -q"


# --------------------------------------------------------------------------- #
# Bug 3: vitest / jest parsers                                                #
# --------------------------------------------------------------------------- #


def test_parse_vitest_all_pass():
    output = """\
 ✓ src/copilot.test.js  (14 tests) 4ms
 ✓ src/parser.test.js  (20 tests) 5ms

 Test Files  2 passed (2)
      Tests  34 passed (34)
   Start at  13:24:04
   Duration  1.23s
"""
    assert _parse_vitest(output) == (34, 0, 0)


def test_parse_vitest_with_failures():
    output = """\
 Test Files  1 failed | 2 passed (3)
      Tests  3 failed | 31 passed (34)
"""
    assert _parse_vitest(output) == (31, 3, 0)


def test_parse_vitest_no_match():
    assert _parse_vitest("no output") == (0, 0, 0)


def test_parse_jest_all_pass():
    output = """\
Test Suites: 5 passed, 5 total
Tests:       42 passed, 42 total
"""
    assert _parse_jest(output) == (42, 0, 0)


def test_parse_jest_with_failures():
    output = """\
Test Suites: 1 failed, 4 passed, 5 total
Tests:       3 failed, 39 passed, 42 total
"""
    assert _parse_jest(output) == (39, 3, 0)


def test_parse_jest_no_match():
    assert _parse_jest("no output") == (0, 0, 0)


# --------------------------------------------------------------------------- #
# _parse_test_output dispatcher                                               #
# --------------------------------------------------------------------------- #


def test_dispatch_vitest_by_output():
    output = " Test Files  2 passed (2)\n      Tests  34 passed (34)"
    assert _parse_test_output("npm test", output) == (34, 0, 0)


def test_dispatch_jest_by_output():
    output = "Tests:       42 passed, 42 total"
    assert _parse_test_output("npm test", output) == (42, 0, 0)


def test_dispatch_vitest_by_command():
    output = "✓ src/foo.test.js (5 tests)\n Tests  5 passed (5)"
    assert _parse_test_output("npx vitest run", output) == (5, 0, 0)


def test_dispatch_pytest_by_command():
    output = "42 passed, 3 failed in 12s"
    assert _parse_test_output("pytest -q", output) == (42, 3, 0)


def test_dispatch_npm_test_with_pytest_output():
    output = "10 passed, 2 failed, 1 error in 5s"
    assert _parse_test_output("npm test", output) == (10, 2, 1)


# --------------------------------------------------------------------------- #
# Lint helpers (I1/I4)                                                         #
# --------------------------------------------------------------------------- #


def test_lint_supports_file_args_common_tools():
    assert _lint_supports_file_args("ruff check")
    assert _lint_supports_file_args("flake8 --max-line-length=120")
    assert _lint_supports_file_args("eslint --fix")
    assert _lint_supports_file_args("black --check")
    assert _lint_supports_file_args("mypy --strict")
    assert not _lint_supports_file_args("npm run lint")
    assert not _lint_supports_file_args("make lint")


def test_run_lint_no_command(tmp_path):
    result = run_lint_on_changed(tmp_path, lint_cmd=None)
    assert not result.ran
    assert result.ok


def test_run_lint_passes(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    result = run_lint_on_changed(tmp_path, lint_cmd="true")
    assert result.ran
    assert result.ok


def test_run_lint_fails(tmp_path):
    result = run_lint_on_changed(tmp_path, lint_cmd="false")
    assert result.ran
    assert not result.ok


def test_run_lint_scoped_to_changed_files(tmp_path):
    """When changed_files is provided and linter supports args, only those files are passed."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    # Use "echo ruff check" so _lint_supports_file_args matches and the
    # shell just echoes the command + appended file args.
    result = run_lint_on_changed(
        tmp_path,
        lint_cmd="echo ruff check",
        changed_files=["a.py"],
    )
    assert result.ran
    assert result.ok
    assert "a.py" in result.output
    assert "b.py" not in result.output


# --------------------------------------------------------------------------- #
# The repo's virtualenv                                                        #
# --------------------------------------------------------------------------- #

def _fake_venv(repo: Path, name: str = ".venv") -> Path:
    """A venv whose `python` is a shell script that identifies itself."""
    bin_dir = repo / name / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python"
    py.write_text("#!/bin/sh\necho VENV_PYTHON_USED\n")
    py.chmod(0o755)
    return bin_dir


def test_run_tests_uses_the_repo_venv(tmp_path):
    """`run_tests` shells out with the SERVER's environment, which on a real
    machine has `python3` but no `python` at all. A confirmed profile in use
    says `python run_tests.py`, so every attempt handed the reviewer a
    failed invocation and TESTING never ran once across 8 tasks."""
    from no_human.testing.runner import run_tests

    _fake_venv(tmp_path)
    result = run_tests(tmp_path, "python run_tests.py")

    assert "VENV_PYTHON_USED" in result.output
    assert result.ok


def test_venv_bin_prefers_dot_venv_then_globs(tmp_path):
    from no_human.testing.runner import _venv_bin

    assert _venv_bin(tmp_path) is None          # no venv → unchanged behavior
    bin312 = _fake_venv(tmp_path, ".venv312")
    assert _venv_bin(tmp_path) == bin312        # found by glob
    canonical = _fake_venv(tmp_path, ".venv")
    assert _venv_bin(tmp_path) == canonical     # `.venv` wins when both exist


def test_env_for_sets_virtual_env_and_prepends_path(tmp_path):
    from no_human.testing.runner import _env_for

    bin_dir = _fake_venv(tmp_path)
    env = _env_for(tmp_path)

    assert env["PATH"].startswith(str(bin_dir))
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


# --------------------------------------------------------------------------- #
# Test-output parsing                                                          #
# --------------------------------------------------------------------------- #

def test_unittest_summary_block_is_parsed_not_grepped():
    """The pytest fallback grepped the whole LOG for "N failed" instead of
    reading the summary block, so a run whose own `Failed:` line said 0, and
    which exited 0, was reported as failing — the reviewer's own evidence,
    wrong. Counts here are illustrative: a real suite's size is a fingerprint
    and the bug does not depend on the number."""
    from no_human.testing.runner import _parse_test_output

    output = (
        "some test printed the word failed 4 times, whatever\n"
        "============================================================\n"
        "Total tests run: 100\n"
        "Passed: 98\n"
        "Failed: 0\n"
        "Errors: 0\n"
    )
    assert _parse_test_output("python run_tests.py", output) == (98, 0, 0)


def test_pytest_output_still_parses():
    from no_human.testing.runner import _parse_test_output
    assert _parse_test_output("pytest -q", "5 passed, 2 failed in 1.2s") == (5, 2, 0)


def test_node_test_tap_summary_is_parsed():
    """Routing web changes to `node --test` must not read '# pass 40' as 0
    passed (the pytest fallback would). Real format captured live 2026-07-11."""
    from no_human.testing.runner import _parse_test_output
    out = "ok 1 - x\n# tests 40\n# pass 40\n# fail 0\n# duration_ms 120\n"
    assert _parse_test_output("node --test src/", out) == (40, 0, 0)
    fail_out = "# tests 40\n# pass 38\n# fail 2\n"
    assert _parse_test_output("node --test src/", fail_out) == (38, 2, 0)
    # Command-agnostic: the TAP summary alone triggers it.
    assert _parse_test_output("make check", "# pass 5\n# fail 0\n") == (5, 0, 0)


# --- process-group kill on timeout (venv-teardown-race, timeout half) --------

def test_run_shell_normal_completion(tmp_path):
    import os
    from no_human.testing.runner import _run_shell
    rc, out, timed_out = _run_shell("echo hello", tmp_path, 10, dict(os.environ))
    assert rc == 0 and "hello" in out and timed_out is False


def test_timeout_kills_the_whole_process_group(tmp_path):
    """On timeout the shell's backgrounded grandchild (stand-in for an xdist
    worker) must die too — not survive to hold the worktree .venv open. The
    grandchild would create a marker at t=2s; if the group is killed at the 1s
    timeout it never does. Plain subprocess.run only kills the direct child, so
    this marker WOULD appear (the bug)."""
    import os, time
    from no_human.testing.runner import _run_shell
    survived = tmp_path / "survived.marker"
    cmd = f"(sleep 2; touch {survived}) & wait"
    rc, out, timed_out = _run_shell(cmd, tmp_path, 1, dict(os.environ))
    assert timed_out is True
    time.sleep(2.5)  # past when the grandchild WOULD have touched the marker
    assert not survived.exists(), "grandchild survived timeout — process group not killed"


def test_terminate_running_kills_a_test_orphaned_by_cancellation(tmp_path):
    """A test subprocess left running (its awaiting coroutine cancelled) must be
    killed by terminate_running BEFORE the worktree teardown rmtree's its .venv."""
    import os, threading, time
    from no_human.testing.runner import _run_shell, terminate_running
    survived = tmp_path / "survived.marker"
    cmd = f"(sleep 3; touch {survived}) & wait"
    # Background thread mimics asyncio.to_thread: it registers itself, then we
    # call terminate_running from "the teardown" while it is still running.
    t = threading.Thread(target=_run_shell, args=(cmd, tmp_path, 30, dict(os.environ)))
    t.start()
    time.sleep(0.6)                       # let it register + spawn the grandchild
    killed = terminate_running(tmp_path)
    assert killed >= 1
    t.join(timeout=5)
    time.sleep(3.2)                       # past when the grandchild WOULD touch it
    assert not survived.exists(), "orphan survived terminate_running — race not closed"


def test_terminate_running_is_a_noop_when_nothing_runs(tmp_path):
    from no_human.testing.runner import terminate_running
    assert terminate_running(tmp_path) == 0


# ── Phase 0.1: pin the interpreter/venv resolution that fixes the historical
# "0 passed, 0 failed, 1 errors" env failure (python missing on the server PATH).
# The server shell has python3 but no `python`; a repo venv or the python3
# fallback must resolve it, and it must be classified as an invocation error
# (retryable) — never reported as a genuine test failure. ────────────────────

def test_python_not_found_classified_as_invocation_error_not_test_failure():
    # returncode!=0 with 0/0/0 counts + "command not found" is an invocation
    # error (retry with a fixed command), NOT "1 errors" reported as a failure.
    from no_human.testing.runner import _is_invocation_error
    out = "/bin/sh: python: command not found"
    assert _is_invocation_error(
        returncode=127, output=out, passed=0, failed=0, errors=0) is True


def test_fix_invocation_rewrites_python_to_python3():
    from no_human.testing.runner import _fix_invocation
    out = "/bin/sh: python: command not found"
    # the exact command shape from a confirmed real profile
    assert _fix_invocation("python run_tests.py", out, Path("/tmp")) == "python3 run_tests.py"
    assert _fix_invocation("python -m pytest -q", out, Path("/tmp")) == "python3 -m pytest -q"


def test_fix_invocation_leaves_non_python_and_found_python_alone():
    from no_human.testing.runner import _fix_invocation
    # python present (no "command not found") → no rewrite
    assert _fix_invocation("python x.py", "3 passed", Path("/tmp")) is None
    # a node command is never rewritten to python3
    assert _fix_invocation("node --test", "node: command not found", Path("/tmp")) is None


def test_venv_bin_finds_repo_python(tmp_path):
    from no_human.testing.runner import _venv_bin
    assert _venv_bin(tmp_path) is None  # no venv yet
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    assert _venv_bin(tmp_path) == bin_dir


def test_env_for_activates_repo_venv(tmp_path):
    from no_human.testing.runner import _env_for
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    env = _env_for(tmp_path)
    # venv bin prepended to PATH (so `python` resolves) + VIRTUAL_ENV set
    assert env["PATH"].split(":")[0] == str(bin_dir)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


# --------------------------------------------------------------------------- #
# SCRUM-35: hermetic web tests in worktrees. `node_modules` is gitignored, so
# a freshly created task worktree (`git worktree add --detach`) never has it —
# unlike a Python venv, which is physically present and auto-reused by
# `_venv_bin`/`_env_for` above. Live 2026-07-25 (SCRUM-33): a worktree-only
# missing-deps failure read as "2335 passed, 1 failed" and burned the whole
# bounded loop chasing a test that was never broken (359/359 green against the
# real `web/node_modules`). Fix: symlink node deps from the source checkout
# before a node/npm test command runs, and — when that isn't possible —
# classify a module-resolution failure as `invocation_error` (INFRA) instead
# of a genuine test failure.
# --------------------------------------------------------------------------- #

def test_ensure_node_deps_symlinks_from_source(tmp_path):
    """A bare worktree with no `node_modules` gets one symlinked in from the
    source repo's populated install, at the same relative path as the cwd."""
    from no_human.testing.runner import run_tests

    source_repo = tmp_path / "source_repo"
    (source_repo / "web" / "node_modules" / "left-pad").mkdir(parents=True)
    work_dir = tmp_path / "work_dir"
    (work_dir / "web").mkdir(parents=True)

    run_tests(
        work_dir, "node --test src/",
        cwd=work_dir / "web", source_repo=source_repo,
    )

    linked = work_dir / "web" / "node_modules"
    assert linked.is_symlink()
    assert linked.resolve() == (source_repo / "web" / "node_modules").resolve()


def test_ensure_node_deps_noop_when_present(tmp_path):
    """Deps already present in the worktree are left alone — never replaced
    by a symlink, even when a source repo is given."""
    from no_human.testing.runner import run_tests

    source_repo = tmp_path / "source_repo"
    (source_repo / "web" / "node_modules").mkdir(parents=True)
    work_dir = tmp_path / "work_dir"
    existing = work_dir / "web" / "node_modules"
    existing.mkdir(parents=True)
    marker = existing / "REAL_INSTALL"
    marker.write_text("present")

    run_tests(
        work_dir, "node --test src/",
        cwd=work_dir / "web", source_repo=source_repo,
    )

    assert not existing.is_symlink(), "a real install must not be replaced"
    assert marker.exists()


def test_ensure_node_deps_skips_python_command(tmp_path):
    """A pytest command never triggers node provisioning — existing
    python/venv behaviour is unaffected by the new source_repo param."""
    from no_human.testing.runner import run_tests

    source_repo = tmp_path / "source_repo"
    (source_repo / "node_modules").mkdir(parents=True)
    work_dir = tmp_path / "work_dir"
    work_dir.mkdir()

    run_tests(work_dir, "true", source_repo=source_repo)

    assert not (work_dir / "node_modules").exists()


def _fake_node_script(bin_dir: Path, body: str) -> dict[str, str]:
    """A fake `node` on PATH that prints canned output instead of running a
    real suite — real subprocess execution, no mocking of the runner."""
    import os as _os
    bin_dir.mkdir(parents=True, exist_ok=True)
    node = bin_dir / "node"
    node.write_text(f"#!/bin/sh\n{body}\n")
    node.chmod(0o755)
    return {"PATH": f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}"}


def test_node_missing_deps_classified_as_invocation_error(tmp_path):
    """A node module-resolution failure — the SCRUM-33 shape, most tests
    collected fine but one file can't resolve its import — must classify as
    `invocation_error`, not a plain test failure the coder gets blamed for."""
    from no_human.testing.runner import run_tests

    env = _fake_node_script(
        tmp_path / "bin",
        "printf '# tests 3\\n# pass 2\\n# fail 0\\n'\n"
        "printf \"Error [ERR_MODULE_NOT_FOUND]: Cannot find package "
        "'left-pad'\\n\" 1>&2\n"
        "exit 1\n",
    )

    result = run_tests(tmp_path, "node --test src/", env=env)

    assert result.invocation_error is True
    assert result.failed == 0, "a dependency-resolution crash is not a test failure count"


def test_node_real_test_failure_is_not_invocation_error(tmp_path):
    """Guard against over-broad classification: a genuine assertion failure,
    with no module-resolution error in the output, must still read as a real
    test failure."""
    from no_human.testing.runner import run_tests

    env = _fake_node_script(
        tmp_path / "bin",
        "printf '# tests 3\\n# pass 2\\n# fail 1\\n'\n"
        "printf 'not ok 3 - assertion mismatch\\n'\n"
        "printf '  AssertionError: expected 2 to equal 3\\n'\n"
        "exit 1\n",
    )

    result = run_tests(tmp_path, "node --test src/", env=env)

    assert result.invocation_error is False
    assert result.failed == 1
