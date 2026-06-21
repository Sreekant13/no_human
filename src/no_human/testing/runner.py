"""Local test runner + git-backed snapshots for the tamper guard.

Phase 0 runs the existing suite locally and reports pass/fail with the raw
output as evidence (no assertions without the command + output). CI triggering
arrives in Phase 3.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import tamper_guard


@dataclass
class TestRunResult:
    ran: bool
    ok: bool
    passed: int
    failed: int
    errors: int
    command: str
    output: str

    @property
    def summary(self) -> str:
        if not self.ran:
            return "no tests run"
        return f"{'PASS' if self.ok else 'FAIL'}: {self.passed} passed, {self.failed} failed, {self.errors} errors"


def detect_command(repo_path: Path) -> str | None:
    """Best-effort test command detection."""
    if (repo_path / "pyproject.toml").exists() or list(repo_path.glob("test*")) \
            or (repo_path / "tests").exists():
        if (repo_path / "uv.lock").exists():
            return "uv run pytest -q"
        return "pytest -q"
    if (repo_path / "package.json").exists():
        return "npm test --silent"
    if (repo_path / "pom.xml").exists():
        return "mvn -q test"
    return None


_PYTEST_SUMMARY = re.compile(r"(\d+) passed|(\d+) failed|(\d+) error")


def _parse_pytest(output: str) -> tuple[int, int, int]:
    passed = failed = errors = 0
    for m in re.finditer(r"(\d+)\s+(passed|failed|error[s]?)", output):
        n, kind = int(m.group(1)), m.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        else:
            errors = n
    return passed, failed, errors


def run_tests(repo_path: Path, command: str | None = None, *, timeout: int = 600) -> TestRunResult:
    repo_path = Path(repo_path)
    cmd = command or detect_command(repo_path)
    if not cmd:
        return TestRunResult(False, True, 0, 0, 0, "", "no test command detected")
    try:
        proc = subprocess.run(
            cmd, cwd=repo_path, shell=True, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(True, False, 0, 0, 1, cmd, f"timed out after {timeout}s")
    output = (proc.stdout or "") + (proc.stderr or "")
    passed, failed, errors = _parse_pytest(output)
    ok = proc.returncode == 0
    return TestRunResult(True, ok, passed, failed, errors, cmd, output[-8000:])


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
