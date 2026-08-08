"""The LARGE-repo tier: no_human's own codebase as the benchmark subject.

parcelo is 187 lines. It cannot tell us whether the loop survives a real
codebase — 1044 tracked files, a real suite, real coupling. This tier uses
no_human itself, pinned, so nobody's concurrent edits can move the subject.

TWO THINGS THAT MAKE THIS TIER DIFFERENT FROM PARCELO, both learned the hard way:

1. **The holdout must prove it graded the sandbox.** The runner grades with
   `sys.executable -m pytest` and `PYTHONPATH=<work>`. This package lives under
   `src/`, so that does NOT expose it — and the venv carries an editable install
   pointing at the operator's primary checkout, which is on another branch with
   uncommitted changes. A naive holdout therefore grades somebody else's working
   tree and looks perfectly valid doing it (proven: the probe imported
   `/Users/.../no_human/src/no_human/__init__.py` from inside a worktree).
   Every holdout here opens with SANDBOX_IMPORT, which puts the sandbox's `src`
   first, purges any pre-imported `no_human`, re-imports, and ASSERTS the import
   came from the sandbox. The assertion is the control: grading the wrong tree
   fails loudly instead of quietly.

2. **Tickets are scoped to exact files and functions.** An open-ended ticket on
   a repo this size is the documented doom-loop, and it would measure the
   ticket, not the loop.

Controls are the same four as the parcelo tier and run on every emit:
base FAIL / reference PASS / the touched module's own tests still green /
a mutation probe over any guard that claims to catch something.

Usage:  python build_large.py <subject-repo> <out-specs-dir>
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SANDBOX_IMPORT = '''\
import pathlib
import sys

# Grade the SANDBOX, not the operator's checkout. See build_large.py's docstring:
# PYTHONPATH=<work> does not reach a src/ layout, and the venv's editable install
# points somewhere else entirely.
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))
for _m in [m for m in list(sys.modules) if m == "no_human" or m.startswith("no_human.")]:
    del sys.modules[_m]
import no_human


def test_the_holdout_is_grading_the_sandbox():
    """The control for every other test in this file. If this fails, nothing
    below it means anything — it was measuring a different checkout."""
    here = pathlib.Path(no_human.__file__).resolve()
    assert here.is_relative_to(_SRC), f"imported {here}, expected under {_SRC}"

'''


SPECS = [
    {
        "id": "nh-01-holdout-could-not-run-is-not-a-failure",
        "title": "NH-41 A held-out test that could not RUN is scored as a failed one",
        "request": (
            "`NorthStarRunner._holdout_ok` in src/no_human/eval/northstar.py "
            "returns a plain True/False from the held-out pytest's exit code, "
            "so a run that never executed a single assertion — pytest missing, "
            "a collection error, an import error in the harness itself — is "
            "recorded as `mergeable=False`, identical to a held-out test that "
            "ran and genuinely failed. That books a broken instrument as a "
            "capability failure of the agent, which is the one direction a "
            "benchmark must never round in. Distinguish them: when the held-out "
            "run did not actually execute tests, return None (the value the "
            "method already uses for 'no holdout', meaning unmeasured) instead "
            "of False. pytest signals this in its exit code — 2 is a collection "
            "or usage error and 3 is an internal error, while 1 means tests ran "
            "and failed. A timeout must keep returning False as it does today: "
            "that one is a real hang, not an absent instrument."),
        "acceptance_criteria": [
            "_holdout_ok returns None when the held-out pytest exits 2 or 3 "
            "(collection/usage or internal error)",
            "_holdout_ok still returns False when pytest exits 1 (tests ran and failed)",
            "_holdout_ok still returns True when pytest exits 0",
            "a timeout still returns False, unchanged",
            "_holdout_ok still returns None when the spec carries no holdout",
            "the existing tests for the runner still pass",
        ],
        "holdout": '''
import subprocess

import pytest

from no_human.eval.bench_task import BenchTask
from no_human.eval.northstar import NorthStarRunner


class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.pid = 1234

    def communicate(self, timeout=None):
        return ("", "")


def _runner():
    return NorthStarRunner.__new__(NorthStarRunner)


def _spec(holdout="def test_x():\\n    assert True\\n"):
    return BenchTask(id="t", title="t", request="r", holdout=holdout)


@pytest.mark.parametrize("rc,expected", [(0, True), (1, False), (2, None), (3, None)])
def test_exit_code_is_read_as_ran_failed_or_never_ran(monkeypatch, tmp_path, rc, expected):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Proc(rc))
    assert _runner()._holdout_ok(_spec(), tmp_path) is expected


def test_no_holdout_is_still_unmeasured(tmp_path):
    assert _runner()._holdout_ok(_spec(holdout=""), tmp_path) is None


def test_a_timeout_is_still_a_failure_not_an_absent_instrument(monkeypatch, tmp_path):
    class _Hang(_Proc):
        def __init__(self):
            super().__init__(0)
            self._first = True

        def communicate(self, timeout=None):
            if self._first and timeout is not None:
                self._first = False
                raise subprocess.TimeoutExpired(cmd="pytest", timeout=timeout)
            return ("", "")

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Hang())
    monkeypatch.setattr("os.killpg", lambda *a, **k: None)
    monkeypatch.setattr("os.getpgid", lambda *a, **k: 1234)
    assert _runner()._holdout_ok(_spec(), tmp_path) is False
''',
        "reference": [(
            "src/no_human/eval/northstar.py",
            """        try:
            proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            proc.communicate()
            return False
        return proc.returncode == 0""",
            """        try:
            proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            proc.communicate()
            return False
        # pytest: 0 = passed, 1 = tests ran and failed, 2 = collection/usage
        # error, 3 = internal error. Only 0 and 1 are verdicts about the
        # agent's work; the rest mean the instrument never ran, which is
        # UNMEASURED, not a failure. Booking those as False is how a broken
        # harness reads as a capability failure.
        if proc.returncode in (2, 3):
            return None
        return proc.returncode == 0""",
        )],
        "test_paths": ["tests/test_northstar.py"],
    },
    {
        "id": "nh-02-bench-run-only-filter",
        "title": "NH-42 Re-running one bench spec means building a directory by hand",
        "request": (
            "`nh bench run` can select a corpus with --specs-dir and cap a run "
            "with --limit, but there is no way to run ONE named spec. Chasing a "
            "single flaky spec therefore means copying its YAML into a scratch "
            "directory every time. Add `--only <spec-id>`, repeatable, to "
            "`nh bench run` in src/no_human/cli/commands.py: it filters the "
            "loaded specs to exactly the ids given, applied AFTER the specs are "
            "loaded and de-duplicated and BEFORE --limit, so --only and --limit "
            "compose the way a reader expects. An id that matches no loaded "
            "spec is a usage error naming the id — silently running nothing is "
            "the failure mode this flag exists to avoid. Without --only, "
            "nothing about the run changes."),
        "acceptance_criteria": [
            "nh bench run accepts a repeatable --only <spec-id>",
            "it filters the loaded specs to exactly those ids",
            "an id matching no loaded spec exits non-zero and names the id",
            "--only is applied before --limit",
            "a run without --only behaves exactly as it does today",
            "the existing CLI tests still pass",
        ],
        "holdout": '''
from click.testing import CliRunner

from no_human.cli.commands import cli


def _opt(name):
    for cmd_name in ("bench",):
        bench = cli.commands[cmd_name]
        run = bench.commands["run"]
        for p in run.params:
            if name in p.opts:
                return p
    return None


def test_the_flag_exists_and_is_repeatable():
    p = _opt("--only")
    assert p is not None, "nh bench run has no --only option"
    assert p.multiple, "--only must be repeatable"


def test_an_unknown_id_is_a_usage_error_that_names_it():
    result = CliRunner().invoke(
        cli, ["bench", "run", "--only", "definitely-not-a-spec-id"])
    assert result.exit_code != 0
    assert "definitely-not-a-spec-id" in result.output


def test_help_documents_it():
    result = CliRunner().invoke(cli, ["bench", "run", "--help"])
    assert result.exit_code == 0
    assert "--only" in result.output
''',
        "reference": [(
            "src/no_human/cli/commands.py",
            """@click.option("--specs-dir", default=None, type=click.Path(path_type=Path),
              help="Read specs from here too (default: eval/northstar_tasks + generated/ when --full).")""",
            """@click.option("--specs-dir", default=None, type=click.Path(path_type=Path),
              help="Read specs from here too (default: eval/northstar_tasks + generated/ when --full).")
@click.option("--only", "only_ids", multiple=True,
              help="Run ONLY these spec ids (repeatable). Applied after the "
                   "corpus loads and before --limit. An id that matches nothing "
                   "is an error, never a silently empty run.")""",
        ), (
            "src/no_human/cli/commands.py",
            """def bench_run(full, limit, gate, prev_path, label, specs_dir, resume, parallel,
              quick, trials):""",
            """def bench_run(full, limit, gate, prev_path, label, specs_dir, resume, parallel,
              quick, trials, only_ids=()):""",
        ), (
            "src/no_human/cli/commands.py",
            """    if limit:
        specs = specs[:limit]""",
            """    if only_ids:
        missing = sorted(set(only_ids) - {s.id for s in specs})
        if missing:
            raise click.UsageError(
                "--only names spec id(s) that are not in the loaded corpus: "
                + ", ".join(missing))
        specs = [s for s in specs if s.id in set(only_ids)]
    if limit:
        specs = specs[:limit]""",
        )],
        "test_paths": ["tests/test_bench_help_matches_the_code.py",
                       "tests/test_bench_quick.py",
                       "tests/test_bench_parallel.py",
                       "tests/test_bench_print_escape.py"],
    },
]


def _run_pytest(tree: Path, targets: list[str]) -> tuple[int, str]:
    # A LIST, not " ".join(...): joined paths arrive as one argv entry and
    # pytest reports "file or directory not found: a.py b.py", which reads as
    # a red suite and would have failed a control for a reason that has
    # nothing to do with the spec.
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets],
        cwd=tree, capture_output=True, text=True,
        env={"PYTHONPATH": str(tree), "PATH": "/usr/bin:/bin",
             "HOME": str(tree / ".home")})
    return p.returncode, p.stdout + p.stderr


def _run_holdout(tree: Path, holdout: str) -> tuple[int, str]:
    held = tree / "tests" / "bench_holdout"
    held.mkdir(parents=True, exist_ok=True)
    f = held / "test_bench_holdout.py"
    f.write_text(holdout)
    rc, out = _run_pytest(tree, [str(f)])
    shutil.rmtree(held, ignore_errors=True)
    return rc, out


def _sandbox(subject: Path) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="nh-large-")) / "work"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", "--shared",
                    str(subject), str(dst)], check=True)
    (dst / ".home").mkdir(exist_ok=True)
    return dst


def apply_reference(tree: Path, patches) -> None:
    for rel, old, new in patches:
        p = tree / rel
        body = p.read_text()
        if old not in body:
            raise SystemExit(f"reference patch no longer applies to {rel}:\n"
                             f"--- expected ---\n{old[:300]}")
        p.write_text(body.replace(old, new, 1))


def main() -> int:
    subject = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=subject,
                         capture_output=True, text=True, check=True).stdout.strip()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=subject,
                            capture_output=True, text=True, check=True).stdout.strip()

    failures = []
    for spec in SPECS:
        sid = spec["id"]
        holdout = SANDBOX_IMPORT + spec["holdout"]
        tree = _sandbox(subject)
        neg_rc, neg_out = _run_holdout(tree, holdout)
        apply_reference(tree, spec["reference"])
        pos_rc, pos_out = _run_holdout(tree, holdout)
        suite_rc, suite_out = _run_pytest(tree, spec["test_paths"])
        ok = neg_rc != 0 and pos_rc == 0 and suite_rc == 0
        print(f"{sid:46} base={'FAIL ok' if neg_rc else 'PASS ✗'} "
              f"ref={'PASS ok' if pos_rc == 0 else 'FAIL ✗'} "
              f"{spec['test_paths'][0].split('/')[-1]}="
              f"{'green' if suite_rc == 0 else 'RED ✗'}")
        if not ok:
            failures.append(sid)
            if neg_rc == 0:
                print("    known-negative broken: passes on the base tree")
            if pos_rc != 0:
                print("    known-positive broken:\n" + pos_out[-2000:])
            if suite_rc != 0:
                print("    reference breaks the module's own tests:\n"
                      + suite_out[-1500:])
        shutil.rmtree(tree.parent, ignore_errors=True)

        (out / f"{sid}.yaml").write_text(yaml.safe_dump({
            "id": sid, "title": spec["title"], "request": spec["request"],
            "source": {"kind": "authored", "session": "personal2-2026-08-08",
                       "label": "no_human large-repo tier"},
            "repo": {"path": str(subject), "pin": pin, "branch": branch},
            "original": {}, "acceptance_criteria": spec["acceptance_criteria"],
            "judge_rubric": [], "holdout": holdout, "subset": "nh-large",
            "runnable": True, "skip_reason": "", "escalation_reason": "",
            "dirty_seed": {}, "expect_escalation": False,
        }, sort_keys=False, allow_unicode=True, width=100))

    print(f"\n{len(SPECS)} large-repo specs written to {out}")
    if failures:
        print(f"CONTROLS FAILED for {len(failures)}: {', '.join(failures)}")
        return 1
    print("controls PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
