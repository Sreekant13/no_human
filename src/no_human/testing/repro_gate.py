"""The reproduction-test gate: deterministic proof a diff does what it claims.

The coder records the tests that demonstrate its change in
``.no_human/repro_tests.json`` (``{"tests": ["tests/test_x.py::test_y", …]}``
— pytest node ids; the file is never committed, ``.no_human/**`` is excluded
from every commit). The gate then proves both directions before a single
reviewer token is spent:

  fails-before: the listed tests, copied into a worktree at the merge base,
                must FAIL there — otherwise they don't demonstrate the change
                (a bugfix that "reproduces" on healthy code reproduces nothing).
  passes-after: the same tests must PASS on the attempt's tree — otherwise the
                change doesn't do what its own tests claim.

Research basis: Agentless / SWE-Doctor / Google BRT — cogenerated
reproduction tests beat LLM review on cost and rival it on defect yield.
This gate is complementary to the adversarial reviewer, not a replacement.

Degradation is loud, never silent: no manifest → ``waived`` (the doctor
counts waiver rate), broken harness → ``error`` (never a fail), and the
whole gate sits behind ``repro_gate.mode`` (off | advisory | required).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = ".no_human/repro_tests.json"
_RUN_TIMEOUT = 600


@dataclass
class ReproResult:
    verdict: str                    # "pass" | "fail" | "waived" | "error"
    tests: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"verdict": self.verdict, "tests": self.tests,
                "reasons": self.reasons}


def read_manifest(repo_path: Path) -> list[str]:
    """The declared repro tests, or [] (no manifest / unreadable / empty)."""
    p = repo_path / MANIFEST
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, list):
        return []
    return [str(t).strip() for t in tests if str(t).strip()]


def _test_files(tests: list[str]) -> list[str]:
    """The file part of each pytest node id, deduplicated, order kept."""
    seen: dict[str, None] = {}
    for t in tests:
        seen.setdefault(t.split("::", 1)[0], None)
    return list(seen)


def _run_pytest(tests: list[str], cwd: Path, env: dict) -> tuple[bool, bool, str]:
    """(ran, all_passed, tail_of_output). Never raises.

    ``ran`` is False when pytest could not be launched or execute at all
    (missing interpreter, timeout, collection error with 0 tests) — an
    ENVIRONMENT failure, which the caller must classify as "error" (advisory)
    rather than "fail" (a real fails-before/passes-after verdict). Uses
    ``sys.executable`` — the interpreter no_human runs under, which has
    pytest — because a bare ``python`` does not exist in a uv/venv project
    (the 2026-07-11 bug: every bugfix task escalated on
    "could not run pytest: No such file or directory: 'python'")."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "--no-header", *tests],
            cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, False, f"timed out after {_RUN_TIMEOUT}s"
    except OSError as exc:
        return False, False, f"could not run pytest: {exc}"
    out = (proc.stdout + proc.stderr)[-2000:]
    # pytest exit 5 = "no tests collected" — an environment/selection problem,
    # not a test verdict. Anything that ran at least one test gives 0-4.
    ran = proc.returncode != 5 and "no tests ran" not in out.lower()
    return ran, proc.returncode == 0, out


def run_repro_gate(repo_path: Path, base_ref: str) -> ReproResult:
    """Prove fails-before / passes-after for the declared repro tests.

    ``base_ref`` is the code before this task's changes (the merge base of the
    attempt branch and the target branch). The before-run copies the test
    files into a temporary worktree at ``base_ref`` and runs them with the
    primary repo's venv on PYTHONPATH-priority, so worktree code shadows any
    editable install of the primary checkout.
    """
    tests = read_manifest(repo_path)
    if not tests:
        return ReproResult("waived", reasons=[f"no {MANIFEST} manifest"])

    from .runner import _env_for  # venv-aware env (the c0df0da lesson)
    env = _env_for(repo_path)

    # passes-after first: cheapest to check, and a manifest whose tests fail
    # on the attempt's own tree is wrong regardless of the before-state.
    missing = [f for f in _test_files(tests) if not (repo_path / f).is_file()]
    if missing:
        return ReproResult("fail", tests=tests, reasons=[
            f"declared test file(s) missing from the attempt tree: {missing} "
            "— a listed repro test may not be deleted"])
    after_env = {**env, "PYTHONPATH": os.pathsep.join(
        [str(repo_path), str(repo_path / "src"), env.get("PYTHONPATH", "")])}
    ran_after, ok_after, out_after = _run_pytest(tests, repo_path, after_env)
    if not ran_after:
        # Could not RUN the tests (env/interpreter/collection) — "can't verify"
        # is not "doesn't pass". Advisory error, never blocks the task.
        return ReproResult("error", tests=tests, reasons=[
            f"repro tests could not be executed:\n{out_after}"])
    if not ok_after:
        return ReproResult("fail", tests=tests, reasons=[
            "passes-after failed — the declared repro tests do not pass on "
            f"the attempt's own tree:\n{out_after}"])

    # fails-before: worktree at base_ref + the test files from the after tree.
    tmp = Path(tempfile.mkdtemp(prefix="nh-repro-"))
    worktree = tmp / "base"
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), base_ref],
            cwd=repo_path, capture_output=True, text=True,
        )
        if added.returncode != 0:
            return ReproResult("error", tests=tests, reasons=[
                f"could not build the base worktree at {base_ref}: "
                f"{added.stderr.strip()[:300]}"])
        for f in _test_files(tests):
            src, dst = repo_path / f, worktree / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        before_env = {**env, "PYTHONPATH": os.pathsep.join(
            [str(worktree), str(worktree / "src"), env.get("PYTHONPATH", "")])}
        ran_before, ok_before, out_before = _run_pytest(tests, worktree, before_env)
        if not ran_before:
            return ReproResult("error", tests=tests, reasons=[
                f"base-tree repro run could not execute:\n{out_before}"])
        if ok_before:
            return ReproResult("fail", tests=tests, reasons=[
                "fails-before failed — the declared repro tests already pass "
                "on the base code, so they do not demonstrate this change"])
        return ReproResult("pass", tests=tests)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=repo_path, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
