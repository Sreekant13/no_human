"""Local test runner + git-backed snapshots for the tamper guard.

Phase 0 runs the existing suite locally and reports pass/fail with the raw
output as evidence (no assertions without the command + output). CI triggering
arrives in Phase 3.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import tamper_guard


log = logging.getLogger(__name__)


@dataclass
class TestRunResult:
    ran: bool
    ok: bool
    passed: int
    failed: int
    errors: int
    command: str
    output: str
    invocation_error: bool = False
    failing_tests: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.ran:
            return "no tests run"
        return f"{'PASS' if self.ok else 'FAIL'}: {self.passed} passed, {self.failed} failed, {self.errors} errors"


def _dir_has_python(d: Path) -> bool:
    """True if *d* contains at least one .py file (two levels deep)."""
    return bool(list(d.glob("*.py")) or list(d.glob("*/*.py")))


def _looks_like_pytest(repo_path: Path) -> bool:
    """Detect a pytest project, including polyglot repos that also carry a
    pom.xml/package.json but whose tests are really Python (e.g. tests live
    under src/tests with a root pytest.ini). Checks shallow, well-known
    locations only — never a deep recursive walk on a large repo."""
    # Strong markers — these files only exist in Python projects.
    for marker in ("pytest.ini", "tox.ini", "conftest.py"):
        if (repo_path / marker).exists():
            return True
    # pyproject.toml / setup.cfg / setup.py are Python-specific only when they
    # actually contain Python packaging metadata, but as a heuristic they are
    # strong enough when there is NO competing ecosystem marker.
    if not (repo_path / "package.json").exists():
        for marker in ("pyproject.toml", "setup.cfg", "setup.py"):
            if (repo_path / marker).exists():
                return True
    # Test directories — only count if they contain at least one .py file.
    for tdir in ("tests", "test", "src/tests", "src/test"):
        d = repo_path / tdir
        if d.is_dir() and _dir_has_python(d):
            return True
    if list(repo_path.glob("src/conftest.py")) \
            or list(repo_path.glob("src/*/conftest.py")):
        return True
    for req in repo_path.glob("requirements*.txt"):
        try:
            if "pytest" in req.read_text(errors="ignore").lower():
                return True
        except OSError:
            pass
    return False


def detect_command(repo_path: Path) -> str | None:
    """Best-effort test command detection.

    Pytest is checked first via explicit markers so a polyglot repo that also
    ships a pom.xml (a Java build alongside a Python test suite — an observed
    shape, not a hypothetical) isn't misrouted to ``mvn``.
    """
    repo_path = Path(repo_path)
    if _looks_like_pytest(repo_path):
        if (repo_path / "uv.lock").exists():
            return "uv run pytest -q"
        return "pytest -q"
    if (repo_path / "package.json").exists():
        return "npm test --silent"
    if (repo_path / "pom.xml").exists():
        return "mvn -q test"
    return None


_PYTEST_SUMMARY = re.compile(r"(\d+) passed|(\d+) failed|(\d+) error")

# The line pytest prints its final tally on (plain "5 passed, 2 failed in
# 1.2s" or "=" bar-padded "===== 2 failed, 40 passed in 3.45s ====="). Text
# printed AFTER this line — e.g. an atexit/session-finish traceback from a
# pytest-xdist teardown race — never contains a bare "N passed/failed/error"
# fragment today, so anchoring to the LAST matching line is a no-op for every
# known-good output and a hard backstop against a future stray digit+keyword
# match (a traceback mentioning a count) being misread as the real tally.
_PYTEST_SUMMARY_LINE = re.compile(r"^.*\b\d+\s+(?:passed|failed|error(?:s)?)\b.*$", re.M)


def _parse_pytest(output: str) -> tuple[int, int, int]:
    lines = _PYTEST_SUMMARY_LINE.findall(output)
    text = lines[-1] if lines else output
    passed = failed = errors = 0
    for m in re.finditer(r"(\d+)\s+(passed|failed|error[s]?)", text):
        n, kind = int(m.group(1)), m.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        else:
            errors = n
    return passed, failed, errors


_PYTEST_FAILED_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-.*)?$", re.M)


def _pytest_failing_tests(output: str) -> list[str]:
    """Extract failing/erroring test node ids from pytest's short test summary
    info section (``FAILED path::test - reason`` / ``ERROR path::test -
    reason``). Ordered, de-duplicated. Without this the gate's failure event
    carried only a bare count — the failing test(s) were never named."""
    seen: list[str] = []
    for m in _PYTEST_FAILED_ID.finditer(output):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


# pytest-xdist's tmp-dir cleanup (TempPathFactory's session-scoped finalizer)
# runs at pytest_unconfigure time, AFTER the real summary line already
# printed. A worker (`popen-gw*`) racing another worker's rmdir of the shared
# `garbage-*` staging dir raises `OSError: [Errno 66] Directory not empty`
# from that late finalizer — the tests already passed, but the process exits
# non-zero, so the gate previously read it as a bare test failure with no
# name to show for it (SCRUM-37).
# Reviewer finding (SCRUM-37 round 1): the race's OSError line often carries
# only the garbage-*/pytest-of-* staging path WITHOUT a popen-gw token (the
# per-worker dir appears on a separate "removing" line, or not at all when the
# session-scoped finalizer loses the race). Any of the three staging markers
# adjacent to the error is the signature; all are pytest-tmpdir-specific
# strings that never appear in real test-failure output.
_TEARDOWN_RACE = re.compile(
    r"Directory not empty.{0,400}?(?:popen-gw|garbage-|pytest-of-)"
    r"|(?:popen-gw|garbage-|pytest-of-).{0,400}?Directory not empty",
    re.DOTALL,
)


def _is_teardown_race(output: str) -> bool:
    return bool(_TEARDOWN_RACE.search(output))


def _parse_vitest(output: str) -> tuple[int, int, int]:
    """Parse vitest summary lines like:
      Tests  413 passed (413)
      Tests  3 failed | 5 passed (8)
    """
    passed = failed = errors = 0
    m = re.search(r"Tests\s+(.+)", output)
    if m:
        line = m.group(1)
        pm = re.search(r"(\d+)\s+passed", line)
        if pm:
            passed = int(pm.group(1))
        fm = re.search(r"(\d+)\s+failed", line)
        if fm:
            failed = int(fm.group(1))
    return passed, failed, errors


def _parse_jest(output: str) -> tuple[int, int, int]:
    """Parse Jest summary like:
      Tests:       3 failed, 5 passed, 8 total
    """
    passed = failed = errors = 0
    m = re.search(r"Tests:\s+(.+)", output)
    if m:
        line = m.group(1)
        pm = re.search(r"(\d+)\s+passed", line)
        if pm:
            passed = int(pm.group(1))
        fm = re.search(r"(\d+)\s+failed", line)
        if fm:
            failed = int(fm.group(1))
    return passed, failed, errors


def _parse_unittest_summary(output: str) -> tuple[int, int, int] | None:
    """Parse a ``Passed:/Failed:/Errors:`` summary block (unittest-style runners).

    A hand-rolled `run_tests.py` runner prints exactly this. Without it the
    pytest fallback scanned the whole LOG rather than the summary block, matched
    a "N failed" printed somewhere in the body, and reported failures for a run
    whose own ``Failed:`` line said zero and which exited 0. The counts are
    deliberately not quoted here: a real suite's size is a fingerprint, and the
    bug is in reading the wrong line, not in any particular number.
    """
    passed = re.search(r"^Passed:\s*(\d+)", output, re.M)
    failed = re.search(r"^Failed:\s*(\d+)", output, re.M)
    errors = re.search(r"^Errors:\s*(\d+)", output, re.M)
    if passed and failed and errors:
        return int(passed.group(1)), int(failed.group(1)), int(errors.group(1))
    return None


def _parse_node_test(output: str) -> tuple[int, int, int]:
    """node --test TAP summary: '# pass 40' / '# fail 0' / '# tests 40'."""
    p = re.search(r"^#\s*pass\s+(\d+)", output, re.M)
    f = re.search(r"^#\s*fail\s+(\d+)", output, re.M)
    return (int(p.group(1)) if p else 0, int(f.group(1)) if f else 0, 0)


def _parse_test_output(command: str, output: str) -> tuple[int, int, int]:
    """Dispatch to the right parser based on the command and output."""
    cmd_lower = command.lower()
    # node --test emits a distinctive TAP summary (`# pass N`); detect it
    # before the pytest fallback, which would read "# pass 40" as 0 passed.
    if "node --test" in cmd_lower or re.search(r"^#\s*(pass|fail|tests)\s+\d+", output, re.M):
        return _parse_node_test(output)
    # Try vitest/jest detection from output first (more reliable than command).
    if re.search(r"Test Files\s+\d+", output) or "vitest" in cmd_lower:
        return _parse_vitest(output)
    if re.search(r"Tests:\s+\d+", output) or "jest" in cmd_lower:
        return _parse_jest(output)
    # A line-anchored Passed:/Failed:/Errors: block is unambiguous — check it
    # before the pytest fallback, which greps the whole log for "N failed".
    unittest_counts = _parse_unittest_summary(output)
    if unittest_counts is not None:
        return unittest_counts
    if "pytest" in cmd_lower or re.search(r"\d+\s+passed", output):
        return _parse_pytest(output)
    # npm test / npx — inspect output to guess framework.
    if "npm" in cmd_lower or "npx" in cmd_lower:
        # vitest puts "Test Files" in output; jest puts "Tests:"
        if "Test Files" in output:
            return _parse_vitest(output)
        if "Tests:" in output:
            return _parse_jest(output)
    # Fallback to pytest parser (handles generic "N passed" lines).
    return _parse_pytest(output)


_INVOCATION_ERROR_PATTERNS = re.compile(
    r"error: unrecognized arguments"
    r"|no tests ran"
    r"|no tests collected"
    r"|ModuleNotFoundError"
    r"|ImportError"
    r"|command not found"
    # node module-resolution failures — a worktree without `node_modules`
    # (gitignored, so a fresh `git worktree add` never has it) reads as a
    # partial test failure ("2335 passed, 1 failed") instead of the
    # infrastructure gap it actually is (SCRUM-33/SCRUM-35).
    r"|Cannot find package"
    r"|Cannot find module"
    r"|ERR_MODULE_NOT_FOUND",
    re.IGNORECASE,
)


def _is_invocation_error(
    returncode: int, output: str,
    passed: int, failed: int, errors: int,
) -> bool:
    """True when the test runner itself failed to execute (bad flags, missing
    plugins, command not found) rather than tests actually failing."""
    if returncode == 0:
        return False
    if passed + failed + errors == 0:
        return True
    if _INVOCATION_ERROR_PATTERNS.search(output):
        return True
    return False


def _fix_invocation(cmd: str, output: str, repo_path: Path) -> str | None:
    """Try to produce a corrected command for a known invocation failure."""
    # python not found → try python3
    if cmd.startswith("python ") and "command not found" in output:
        return "python3" + cmd[6:]
    # pytest bad flags → strip addopts
    if "pytest" in cmd and "unrecognized arguments" in output:
        return f'pytest -q --override-ini="addopts=" --override-ini="testpaths=" {repo_path}'
    return None


def _venv_bin(repo_path: Path) -> Path | None:
    """The repo's virtualenv ``bin/`` directory, if it ships one.

    These commands are shelled out from the long-running server, whose PATH is
    not a developer's shell: on this machine it has ``python3`` but no ``python``
    at all. A repo that ships a venv expects ``source .venv/bin/activate`` first,
    and its test command is written on that assumption (a confirmed profile in
    use says ``python run_tests.py``, whose imports resolve only inside the
    venv). This is that activation, without the shell.
    """
    names = [".venv", "venv"]
    names += sorted(
        p.name for p in repo_path.glob(".venv*")
        if p.is_dir() and p.name not in names
    )
    for name in names:
        bin_dir = repo_path / name / "bin"
        if (bin_dir / "python").exists():
            return bin_dir
    return None


def _is_node_cmd(cmd: str) -> bool:
    """True for a node/npm-family test command — the ones that need
    `node_modules` present to even start."""
    c = cmd.lower()
    return (
        "node --test" in c or "npm" in c or "npx" in c
        or "vitest" in c or "jest" in c
    )


def _ensure_node_deps(repo_path: Path, work_dir: Path, source_repo: Path | None) -> None:
    """Make `node_modules` present in *work_dir* before a node test command runs.

    `node_modules` is gitignored, so a freshly created `git worktree add`
    checkout never has it — unlike a Python venv (physically present, so the
    runner's ``_venv_bin``/``_env_for`` reuse it automatically), leaving web
    tests non-hermetic in a task worktree (SCRUM-33/SCRUM-35). Mirrors that
    venv-reuse pattern for node: symlink the already-installed
    `node_modules` from the source checkout rather than reinstalling
    (`npm ci` in a fresh worktree is slow and network-dependent — itself an
    infra risk).

    *work_dir* maps to *source_repo* via the same path relative to
    *repo_path* (e.g. `<worktree>/web` -> `<source_repo>/web`). Best-effort:
    never raises — a residual missing-deps run is still caught by
    ``_INVOCATION_ERROR_PATTERNS`` below.
    """
    if source_repo is None:
        return
    work_dir = Path(work_dir)
    target = work_dir / "node_modules"
    if target.exists() or target.is_symlink():
        return  # already present (real dir or a prior symlink) — no-op
    try:
        rel = work_dir.resolve().relative_to(Path(repo_path).resolve())
    except (OSError, ValueError):
        rel = Path(".")
    src = Path(source_repo) / rel / "node_modules"
    try:
        if src.is_dir():
            work_dir.mkdir(parents=True, exist_ok=True)
            target.symlink_to(src, target_is_directory=True)
    except OSError as exc:
        log.warning(
            "node deps provisioning failed (work_dir=%s, source=%s): %s",
            work_dir, src, exc,
        )


def _env_for(repo_path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Process env for a repo command: the server's, plus the repo's venv."""
    run_env = {**os.environ, **(env or {})}
    bin_dir = _venv_bin(repo_path)
    if bin_dir is not None:
        run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env.get('PATH', '')}"
        run_env["VIRTUAL_ENV"] = str(bin_dir.parent)
        run_env.pop("PYTHONHOME", None)  # activate() clears this too
    return run_env


# Live test subprocesses, so the attempt teardown can kill any left running by
# a CANCELLATION before it removes the worktree. `nh pause` / stuck-reset unwind
# the awaiting coroutine, but asyncio.to_thread's thread (and its pytest
# subprocess) keep running; the teardown then rmtree's the worktree .venv out
# from under them (the xdist INTERNALERROR). terminate_running() kills them
# first. Keyed by id(proc); each entry holds the resolved cwd for prefix match.
_RUNNING_LOCK = threading.Lock()
_RUNNING: dict[int, tuple[str, "subprocess.Popen"]] = {}


def _register(work_dir: Path, proc: "subprocess.Popen") -> None:
    with _RUNNING_LOCK:
        _RUNNING[id(proc)] = (str(Path(work_dir).resolve()), proc)


def _deregister(proc: "subprocess.Popen") -> None:
    with _RUNNING_LOCK:
        _RUNNING.pop(id(proc), None)


def terminate_running(under: Path | str) -> int:
    """Kill (process-group) any live test subprocess whose cwd is inside *under*.
    Called by the attempt teardown BEFORE it removes the worktree, so a test
    orphaned by a cancellation is dead before rmtree deletes its .venv. Safe to
    call always: on a normal finish the process already deregistered, so this is
    a no-op — it can only improve the race, never worsen it. Returns the count."""
    root = str(Path(under).resolve())
    with _RUNNING_LOCK:
        procs = [p for (wd, p) in _RUNNING.values()
                 if wd == root or wd.startswith(root + os.sep)]
    killed = 0
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    return killed


def _run_shell(
    cmd: str, work_dir: Path, timeout: int, run_env: dict[str, str],
) -> tuple[int | None, str, bool]:
    """Run a shell test command, capturing stdout+stderr. Returns
    ``(returncode, output, timed_out)``.

    On timeout the WHOLE process group is killed (``start_new_session=True``
    puts the shell in its own group, ``killpg`` reaps it), so the shell's
    grandchildren — pytest and its ``-n auto`` xdist workers — die too instead
    of being orphaned. Orphaned workers keep the worktree's ``.venv`` open, and
    the attempt's teardown then rmtree's it out from under them (the xdist
    ``INTERNALERROR: .venv/bin/python`` seen in the 3-parallel run). Plain
    ``subprocess.run(timeout=…)`` only kills the direct child (the shell), so
    the workers survived."""
    proc = subprocess.Popen(
        cmd, cwd=work_dir, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=run_env, start_new_session=True,
    )
    _register(work_dir, proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "") + (err or ""), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()  # fallback: at least the direct child
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return None, (out or "") + (err or ""), True
    finally:
        _deregister(proc)


def run_tests(
    repo_path: Path, command: str | None = None, *,
    cwd: Path | None = None, timeout: int = 1800,
    env: dict[str, str] | None = None,
    source_repo: Path | None = None,
) -> TestRunResult:
    # timeout was 600s — shorter than a real suite in a fresh worktree venv
    # under parallel load. Every dogfood task of the first parallel run
    # (2026-07-11) failed as "0 passed, 0 failed, 1 errors": the TIMEOUT,
    # parsed as an error, invisible until failures carried their output.
    # 1800s gives honest room; a timeout still reads as failure (never a
    # false pass), and now says so in the record.
    repo_path = Path(repo_path)
    work_dir = Path(cwd) if cwd else repo_path  # Phase 6b: cross-repo cwd
    cmd = command or detect_command(repo_path)
    if not cmd:
        return TestRunResult(False, True, 0, 0, 0, "", "no test command detected")
    if source_repo is not None and _is_node_cmd(cmd):
        _ensure_node_deps(repo_path, work_dir, Path(source_repo))
    run_env = _env_for(repo_path, env)
    src_dir = work_dir / "src"
    if work_dir != repo_path and src_dir.is_dir():
        # The shared venv's editable install resolves the package to the MAIN
        # checkout, so a worktree's tests silently import main's code — the
        # python twin of the node-deps trap above (SCRUM-18 burned its whole
        # budget on 5 "failures" that were main's code, not the branch's).
        # sys.path (which PYTHONPATH feeds) is consulted before appended
        # editable finders, so the worktree's src/ must lead.
        prior = run_env.get("PYTHONPATH", "")
        run_env["PYTHONPATH"] = (
            f"{src_dir}{os.pathsep}{prior}" if prior else str(src_dir)
        )
    rc, output, timed_out = _run_shell(cmd, work_dir, timeout, run_env)
    if timed_out:
        return TestRunResult(True, False, 0, 0, 1, cmd, f"timed out after {timeout}s")
    passed, failed, errors = _parse_test_output(cmd, output)
    failing_tests = _pytest_failing_tests(output)
    ok = rc == 0
    if not ok and failed == 0 and errors == 0 and not failing_tests and _is_teardown_race(output):
        # INFRA, not a coder-bug: the tests already passed (the anchored
        # summary line shows zero failed/errors and nothing was named), but
        # the process exited non-zero from the late xdist teardown race.
        # Bounded to exactly ONE retry — same doctrine as the node-deps
        # invocation-error fix (SCRUM-35), applied to the in-runner retry
        # half rather than the base-tree-confirmation half (the race is
        # non-deterministic, so re-confirming against the base tree would
        # just be a second coin flip).
        log.warning("teardown-race signature in test output (cmd=%s, rc=%s); retrying once",
                    cmd, rc)
        rc_r, output_r, timed_out_r = _run_shell(cmd, work_dir, timeout, run_env)
        if timed_out_r:
            return TestRunResult(True, False, 0, 0, 1, cmd,
                                 f"timed out after {timeout}s", invocation_error=True)
        passed_r, failed_r, errors_r = _parse_test_output(cmd, output_r)
        failing_tests_r = _pytest_failing_tests(output_r)
        return TestRunResult(True, rc_r == 0, passed_r, failed_r, errors_r,
                             cmd, output_r[-8000:], failing_tests=failing_tests_r)
    if not ok and _is_invocation_error(rc, output, passed, failed, errors):
        retry_cmd = _fix_invocation(cmd, output, repo_path)
        if retry_cmd and retry_cmd != cmd:
            log.warning("test invocation error (cmd=%s, rc=%s), retrying with: %s",
                        cmd, rc, retry_cmd)
            rc2, output2, timed_out2 = _run_shell(retry_cmd, work_dir, timeout, run_env)
            if timed_out2:
                return TestRunResult(True, False, 0, 0, 1, retry_cmd,
                                     f"timed out after {timeout}s",
                                     invocation_error=True)
            passed2, failed2, errors2 = _parse_test_output(retry_cmd, output2)
            ok2 = rc2 == 0
            if _is_invocation_error(rc2, output2, passed2, failed2, errors2):
                return TestRunResult(True, False, passed2, failed2, errors2,
                                     retry_cmd, output2[-8000:],
                                     invocation_error=True)
            return TestRunResult(True, ok2, passed2, failed2, errors2,
                                 retry_cmd, output2[-8000:],
                                 failing_tests=_pytest_failing_tests(output2))
        # No fixable retry — mark as invocation error
        return TestRunResult(True, False, passed, failed, errors,
                             cmd, output[-8000:], invocation_error=True)
    return TestRunResult(True, ok, passed, failed, errors, cmd, output[-8000:],
                         failing_tests=failing_tests)


@dataclass
class LintResult:
    ran: bool
    ok: bool
    command: str
    output: str

    @property
    def summary(self) -> str:
        if not self.ran:
            return "no lint run"
        return f"lint {'PASS' if self.ok else 'FAIL'}"


def run_lint_on_changed(
    repo_path: Path,
    lint_cmd: str | None = None,
    changed_files: list[str] | None = None,
    *,
    timeout: int = 120,
) -> LintResult:
    """Run the repo's lint command scoped to changed files when possible.

    If *changed_files* is provided and the lint command supports appending file
    args (ruff, flake8, eslint, mypy), only those files are checked. This
    avoids noise from pre-existing violations in untouched code.

    Returns ``LintResult(ran=False)`` if no lint command is available.
    """
    repo_path = Path(repo_path)
    if not lint_cmd:
        return LintResult(False, True, "", "no lint command configured")

    cmd = lint_cmd
    if changed_files:
        # Scope to changed files for linters that accept file arguments.
        ext_files = [f for f in changed_files if not f.endswith("/")]
        if ext_files and _lint_supports_file_args(lint_cmd):
            cmd = f"{lint_cmd} {' '.join(ext_files)}"

    try:
        proc = subprocess.run(
            cmd, cwd=repo_path, shell=True, capture_output=True, text=True,
            timeout=timeout, env=_env_for(Path(repo_path)),
        )
    except subprocess.TimeoutExpired:
        return LintResult(True, False, cmd, f"lint timed out after {timeout}s")
    output = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    return LintResult(True, proc.returncode == 0, cmd, output)


def _lint_supports_file_args(cmd: str) -> bool:
    """Heuristic: common linters that accept file/path arguments."""
    for tool in ("ruff", "flake8", "pylint", "mypy", "eslint", "prettier", "black"):
        if tool in cmd.lower():
            return True
    return False


def run_command(
    repo_path: Path, command: str, *, timeout: int = 600
) -> tuple[bool, int, str]:
    """Run an arbitrary shell command in ``repo_path``; return (ok, exit_code,
    output_tail). Used by `nh onboard` to PROVE a derived install/lint command by
    actually running it — the literal command, no agent in the loop."""
    try:
        proc = subprocess.run(
            command, cwd=repo_path, shell=True, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, -1, f"timed out after {timeout}s"
    output = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    return proc.returncode == 0, proc.returncode, output


def _git_show(repo_path: Path, ref: str, path: str) -> str:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_path, capture_output=True, text=True,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _git_files(repo_path: Path, ref: str) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=repo_path, capture_output=True, text=True,
    )
    return [f for f in proc.stdout.splitlines() if f] if proc.returncode == 0 else []


def run_held_out_tests(repo_path: Path, *, timeout: int = 120) -> TestRunResult | None:
    """Run held-out tests if tests/held_out/ exists. Returns None if absent.

    The held-out directory is intentionally not mentioned to the implementing
    agent, so it cannot be gamed. These results are passed to the reviewer as
    additional evidence the implementer never saw.
    """
    held_path = Path(repo_path) / "tests" / "held_out"
    if not held_path.exists():
        return None
    cmd = f"pytest -q {held_path}"
    try:
        proc = subprocess.run(
            cmd, cwd=repo_path, shell=True, capture_output=True, text=True,
            timeout=timeout, env=_env_for(Path(repo_path)),
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(True, False, 0, 0, 1, cmd, f"timed out after {timeout}s")
    output = (proc.stdout or "") + (proc.stderr or "")
    passed, failed, errors = _parse_pytest(output)
    ok = proc.returncode == 0
    return TestRunResult(True, ok, passed, failed, errors, cmd, output[-4000:])


def tamper_check_between(
    repo_path: Path, before_ref: str = "HEAD~1", after_ref: str = "HEAD"
) -> tamper_guard.TamperReport:
    """Snapshot test files at two refs and run the tamper guard between them."""
    repo_path = Path(repo_path)
    before, after = {}, {}
    for path in _git_files(repo_path, before_ref):
        if tamper_guard.is_test_file(path):
            before[path] = _git_show(repo_path, before_ref, path)
    for path in _git_files(repo_path, after_ref):
        if tamper_guard.is_test_file(path):
            after[path] = _git_show(repo_path, after_ref, path)
    return tamper_guard.check(before, after)
