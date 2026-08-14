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
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..profile import ProjectProfile

MANIFEST = ".no_human/repro_tests.json"
_RUN_TIMEOUT = 600
# The sanity pre-run's own bound (SCRUM-78) — independent of _RUN_TIMEOUT so a
# broken/hung runner never costs a full fails-before/passes-after budget just
# to prove it can launch at all. A bare invocation may legitimately run a
# runner's whole default suite (jest/go test/etc. with no path given), so a
# large healthy suite can still time out here; that reads as an honest
# advisory "error" (never a false pass/fail), which is the fail-closed
# contract this gate promises — this ticket trades precision/cost, not safety.
# Kept well under _RUN_TIMEOUT (pinned by a test) so it can't silently
# drift back toward the old 600s cost.
_SANITY_TIMEOUT = 60


@dataclass
class ReproResult:
    verdict: str                    # "pass" | "fail" | "waived" | "error"
    tests: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    resume_shape: bool = False

    def to_json(self) -> dict:
        return {"verdict": self.verdict, "tests": self.tests,
                "reasons": self.reasons, "resume_shape": self.resume_shape}


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


def _pytest_python(repo_path: Path) -> str | None:
    """The interpreter to run the repro tests with, or None if none is usable.

    Normally ``sys.executable`` — the interpreter no_human runs under, which has
    pytest — because a bare ``python`` does not exist in a uv/venv project
    (the 2026-07-11 bug: every bugfix task escalated on "could not run pytest:
    No such file or directory: 'python'").

    But in a PyInstaller-frozen build ``sys.executable`` is the frozen ``nh``
    binary, NOT a Python interpreter. Invoking it with ``-m pytest`` re-runs the
    click CLI (nh_entry.main), which exits non-zero without running a single
    test — so the gate read ``ran=True, all_passed=False`` and returned a
    confident but FALSE ``fail`` for every bugfix. In a frozen build fall back to
    a real interpreter: the target repo's own venv first (it has the repo's deps
    and pytest), then ``python3``/``python`` on PATH. None → the caller fails
    closed to ``error`` (advisory), never a false pass/fail."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    from .runner import _venv_bin

    from .runner import _IS_WINDOWS

    bin_dir = _venv_bin(repo_path)
    if bin_dir is not None:
        # `_venv_bin` returns `<venv>\Scripts` on Windows, where the
        # interpreter is `python.exe` — an extensionless `python` there is not
        # a file, so this returned a path that does not exist.
        return str(bin_dir / ("python.exe" if _IS_WINDOWS else "python"))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _run_pytest(
    tests: list[str], cwd: Path, env: dict, python: str,
) -> tuple[bool, bool, str]:
    """(ran, all_passed, tail_of_output). Never raises.

    ``ran`` is False when pytest could not be launched or execute at all
    (missing interpreter, timeout, pytest not importable, collection error with
    0 tests) — an ENVIRONMENT failure, which the caller must classify as "error"
    (advisory) rather than "fail" (a real fails-before/passes-after verdict).
    ``python`` is resolved by :func:`_pytest_python` — ``sys.executable`` in a
    normal install, a real interpreter in a frozen build where ``sys.executable``
    is the nh binary."""
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "-x", "-q", "--no-header", *tests],
            cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, False, f"timed out after {_RUN_TIMEOUT}s"
    except OSError as exc:
        return False, False, f"could not run pytest: {exc}"
    out = (proc.stdout + proc.stderr)[-2000:]
    # The interpreter can't even load pytest (a bare system python3 fallback,
    # or the frozen binary re-running the CLI). That is an environment failure,
    # NOT a test verdict — treat as not-ran so the caller returns "error", never
    # a false "fail".
    if "No module named pytest" in out or "No module named 'pytest'" in out:
        return False, False, out
    # pytest exit 5 = "no tests collected" — an environment/selection problem,
    # not a test verdict. Anything that ran at least one test gives 0-4.
    ran = proc.returncode != 5 and "no tests ran" not in out.lower()
    return ran, proc.returncode == 0, out


def _is_python_profile(profile: "ProjectProfile | None") -> bool:
    """True when the repro run should go through pytest — the default when no
    profile is given (unchanged pre-SCRUM-65 behaviour), or when the profile's
    declared ecosystem is unset or python-flavoured ("python-pytest" etc.).
    Any other declared ecosystem ("node", "maven", "go", ...) routes through
    the profile's own ``test_cmd`` instead — pytest cannot run JS/Go/Java
    tests, so keeping this pytest-only silently no-ops the repro guarantee for
    every non-Python repo (SCRUM-65)."""
    if profile is None:
        return True
    eco = (profile.ecosystem or "").strip().lower()
    return not eco or eco.startswith("python")


def _parse_test_cmd(test_cmd: str | None) -> list[str] | None:
    """The profile's ``test_cmd`` split into argv, or None if missing, blank,
    unparseable (e.g. mismatched quotes), or containing an un-interpolated
    template placeholder — the caller fails closed to 'error' rather than
    guess an invocation or forward a literal ``{token}`` to a foreign runner
    (SCRUM-65 review item 4)."""
    if not test_cmd or not test_cmd.strip():
        return None
    try:
        argv = shlex.split(test_cmd)
    except ValueError:
        return None
    if not argv:
        return None
    if any("{" in tok or "}" in tok for tok in argv):
        return None
    return argv


def _test_cmd_targets(tests: list[str]) -> list[str]:
    """Test-target arguments for a non-Python ``test_cmd``.

    pytest node ids (``path::case``) mean nothing to jest/mocha/go test — a
    raw ``::`` would reach a foreign runner verbatim (SCRUM-65 review item 4).
    Strip to the deduplicated file part, same convention as ``_test_files``."""
    return _test_files(tests)


def _runner_sanity_check(argv: list[str], cwd: Path, env: dict) -> tuple[bool, str]:
    """Prove the runner behind ``argv`` is actually AVAILABLE in ``cwd`` —
    before trusting any exit code from a test-targeted invocation there as a
    genuine fails-before verdict.

    A ``git worktree add`` checkout of ``base_ref`` contains only tracked
    files — installed deps (node_modules, a vendored module cache, ...) are
    not — so an otherwise-working test_cmd can fail to even launch in that
    isolated worktree for an ENVIRONMENT reason that has nothing to do with
    the bug under test (SCRUM-65 review: a JS repro's fails-before step
    exited 127/1 from a missing runner/dependency, and any non-zero exit was
    being read as "bug reproduced" — a vacuous, always-true repro). We never
    provision deps here (no npm install — slow and out of scope); an honest
    refusal is the correct outcome.

    SCRUM-78 tried to let a runner that RAN but found no tests / reported
    real failures ("ran, exit != 0") through, distinguishing it from "never
    started", using elapsed wall-clock as the discriminator. Review rejected
    that: a slow environment failure (e.g. a missing-dependency import that
    takes over a second to raise) is indistinguishable from a slow real
    failure by timing alone, and misclassifying it as "ran" resurrects the
    exact vacuous-repro bug SCRUM-65 fixed (proven by
    ``test_non_python_slow_missing_dep_is_advisory_error_not_pass`` in
    tests/test_repro_gate.py, which fails a pure-timing discriminator). A
    generic probe (``--version``/``--help``) doesn't generalize either —
    ``test_cmd`` is an arbitrary profile-supplied string across every
    ecosystem, and a probe a healthy runner fails is a new false-refusal
    class. There is no reliable way to tell "ran, found failures" apart from
    "never really started" without provisioning the worktree's dependencies,
    which is out of scope here — so this refuses on ANY non-zero exit,
    matching the pre-SCRUM-78 contract. What SCRUM-78 keeps: a short,
    independent timeout (``_SANITY_TIMEOUT`` vs the old ``_RUN_TIMEOUT``) and
    a distinct, honest reason string per launch-failure mode, including a
    process killed by a signal (crash/OOM) — which "ran to completion" never
    covers, timing or not."""
    if not argv:
        return False, "no test runner command configured"
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_SANITY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"sanity check timed out after {_SANITY_TIMEOUT}s (runner hung "
            "or stalled)")
    except OSError as exc:
        return False, f"test runner could not be launched (system error): {exc}"
    out = (proc.stdout + proc.stderr)[-2000:]
    if proc.returncode < 0:
        return False, (
            f"test runner was killed by signal {-proc.returncode} "
            f"(crash/OOM) — not a test verdict:\n{out}")
    if proc.returncode == 127:
        return False, f"test runner not found (command not found):\n{out}"
    if proc.returncode == 126:
        return False, f"test runner is not executable (exit 126):\n{out}"
    if proc.returncode == 0:
        return True, out
    return False, (
        f"test runner exited {proc.returncode} when launched bare with no "
        "test-file arguments in the isolated base worktree — a dependency "
        "failure and a real test failure are indistinguishable here without "
        f"provisioning deps, so this refuses rather than risk a fabricated "
        f"fails-before verdict:\n{out}")


def _run_test_cmd(
    argv: list[str], tests: list[str], cwd: Path, env: dict,
) -> tuple[bool, bool, str]:
    """(ran, all_passed, tail_of_output) for a non-Python ``profile.test_cmd``.

    Mirrors ``_run_pytest``'s contract but stays language-agnostic: the
    touched test files are appended as positional arguments (works across
    jest, mocha, go test, etc. without special syntax), and ``ran`` is False
    when the command itself could not be launched, OR when it exits 126/127
    ("command not found" / "not executable" — the shell's own signal that
    nothing ran, not a test verdict). pytest's exit-code-5 / 'no tests ran'
    conventions don't generalize across test runners, so we don't try to
    guess collection state generically beyond that."""
    try:
        proc = subprocess.run(
            [*argv, *tests], cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, False, f"timed out after {_RUN_TIMEOUT}s"
    except OSError as exc:
        return False, False, f"could not run test_cmd: {exc}"
    out = (proc.stdout + proc.stderr)[-2000:]
    if proc.returncode in (126, 127):
        return False, False, (
            f"test runner exited {proc.returncode} (not found / not "
            f"executable — environment failure, not a test verdict):\n{out}")
    return True, proc.returncode == 0, out


def run_repro_gate(
    repo_path: Path, base_ref: str, profile: "ProjectProfile | None" = None,
    *, resume_shape: bool = False,
) -> ReproResult:
    """Prove fails-before / passes-after for the declared repro tests.

    ``base_ref`` is the code before this task's changes (the merge base of the
    attempt branch and the target branch). The before-run copies the test
    files into a temporary worktree at ``base_ref`` and runs them with the
    primary repo's venv on PYTHONPATH-priority, so worktree code shadows any
    editable install of the primary checkout.

    ``profile`` is the repo's confirmed :class:`ProjectProfile` (or None). A
    Python profile (or no profile — the historical default) runs the repro
    tests with pytest, unchanged. Any other declared ecosystem routes the same
    fails-before/passes-after proof through the profile's own ``test_cmd``
    instead (SCRUM-65), so the guarantee is not pytest-only.

    ``resume_shape`` (keyword-only, default ``False``) covers a RESUMED
    attempt: one that branches from a ``[WIP-BLOCKED]``/``[WIP-PARTIAL]``
    checkpoint rather than from the task's true base. The caller is
    responsible for handing this function a ``base_ref`` that resolves to
    that TRUE pre-work base (never the checkpoint itself — see
    ``Orchestrator._repro_base_ref``); this flag only changes the WORDING of
    the fail reasons and stamps ``resume_shape`` on the result, so a resumed
    task's send-back message reads as "this resumed attempt proved nothing"
    rather than the misleading "the base code already does this". The
    red-first contract is NOT relaxed: both directions (fail-on-base AND
    pass-on-tip) are still required for ``pass``, exactly as the default
    path requires. When ``resume_shape`` is ``False`` (the default) this
    function's behaviour, reasons and return values are byte-identical to
    before this parameter existed.
    """
    tests = read_manifest(repo_path)
    if not tests:
        return ReproResult("waived", reasons=[f"no {MANIFEST} manifest"])

    python: str | None = None
    test_argv: list[str] | None = None
    if _is_python_profile(profile):
        # In a frozen build sys.executable is the nh binary, not a Python
        # interpreter — resolve a real one, or fail closed to "error" (a
        # false verdict is worse than an honest "could not verify").
        python = _pytest_python(repo_path)
        if python is None:
            return ReproResult("error", tests=tests, reasons=[
                "no Python interpreter available to run the repro tests "
                "(frozen build with no target-repo venv and no python on "
                "PATH) — the gate fails closed rather than guess a verdict"])
    else:
        test_argv = _parse_test_cmd(profile.test_cmd if profile else None)
        if test_argv is None:
            return ReproResult("error", tests=tests, reasons=[
                "no usable profile.test_cmd to run the repro tests for "
                f"ecosystem {(profile.ecosystem if profile else '')!r} "
                "(missing or unparseable) — the gate fails closed rather "
                "than guess a verdict"])

    def _run(tests_: list[str], cwd: Path, env: dict) -> tuple[bool, bool, str]:
        if python is not None:
            return _run_pytest(tests_, cwd, env, python)
        return _run_test_cmd(test_argv, tests_, cwd, env)

    # pytest keeps its node ids (``path::case``); a foreign runner only gets
    # file paths — a raw "::" would reach jest/mocha/go test verbatim.
    target_files = tests if python is not None else _test_cmd_targets(tests)

    from .runner import _env_for  # venv-aware env (the c0df0da lesson)
    env = _env_for(repo_path)

    # passes-after first: cheapest to check, and a manifest whose tests fail
    # on the attempt's own tree is wrong regardless of the before-state.
    missing = [f for f in _test_files(tests) if not (repo_path / f).is_file()]
    if missing:
        return ReproResult("fail", tests=tests, reasons=[
            f"declared test file(s) missing from the attempt tree: {missing} "
            "— a listed repro test may not be deleted"])
    if python is not None:
        after_env = {**env, "PYTHONPATH": os.pathsep.join(
            [str(repo_path), str(repo_path / "src"), env.get("PYTHONPATH", "")])}
    else:
        after_env = env
    ran_after, ok_after, out_after = _run(target_files, repo_path, after_env)
    if not ran_after:
        # Could not RUN the tests (env/interpreter/collection) — "can't verify"
        # is not "doesn't pass". Advisory error, never blocks the task.
        return ReproResult("error", tests=tests, reasons=[
            f"repro tests could not be executed:\n{out_after}"])
    if not ok_after:
        if resume_shape:
            return ReproResult("fail", tests=tests, resume_shape=True, reasons=[
                "resume-shape: pass-on-tip failed — the declared repro "
                "tests do not pass on the attempt's own tree:\n"
                f"{out_after}"])
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
        if test_argv is not None:
            # The worktree has only tracked files — no installed deps. Prove
            # the runner itself works there BEFORE trusting any exit code
            # from the real (test-targeted) invocation as a fails-before
            # verdict; a broken/dep-less runner must never read as "the bug
            # reproduced" (SCRUM-65 review).
            sane, sane_reason = _runner_sanity_check(test_argv, worktree, env)
            if not sane:
                return ReproResult("error", tests=tests, reasons=[
                    "repro runner unavailable in the isolated base worktree "
                    "— the gate fails closed rather than read an environment "
                    f"failure as a fails-before verdict: {sane_reason}"])
        for f in _test_files(tests):
            src, dst = repo_path / f, worktree / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if python is not None:
            before_env = {**env, "PYTHONPATH": os.pathsep.join(
                [str(worktree), str(worktree / "src"), env.get("PYTHONPATH", "")])}
        else:
            before_env = env
        ran_before, ok_before, out_before = _run(target_files, worktree, before_env)
        if not ran_before:
            return ReproResult("error", tests=tests, reasons=[
                f"base-tree repro run could not execute:\n{out_before}"])
        if ok_before:
            if resume_shape:
                return ReproResult("fail", tests=tests, resume_shape=True, reasons=[
                    "resume-shape: fails-before failed — the declared repro "
                    "tests already pass on the resumed base, so this attempt "
                    "has no proof of change"])
            return ReproResult("fail", tests=tests, reasons=[
                "fails-before failed — the declared repro tests already pass "
                "on the base code, so they do not demonstrate this change"])
        return ReproResult("pass", tests=tests, resume_shape=resume_shape)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=repo_path, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
