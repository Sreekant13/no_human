"""The two test lanes are spelled identically in both places, and they partition
the suite.

`.github/workflows/ci.yml` and `scripts/run_tests.sh` each choose tests with a
`-m` marker expression, and the two are maintained by hand. Both files already
argue for this guard in prose:

  ci.yml          "every node is in exactly one, which tests/test_test_lanes.py
                   pins and the collection counts reconcile"
  CONTRIBUTING.md "a mistyped marker expression drops a node out of *both*, and
                   a test that runs nowhere looks exactly like a test that
                   passes"

Until this file existed those two sentences named a test that was not in the
tree (issue #109): it lived only in the private repo this project was developed
in, where four of its assertions checked for files that do not exist here, so
the prose was exported and the test was not.

This is the public-tree version. It checks the two properties that matter and
nothing that depends on the other repository.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RUN_TESTS_PATH = ROOT / "scripts" / "run_tests.sh"

#: `-m "<expression>"` as it appears inside ci.yml's computed selector, where
#: each branch is a single-quoted shell fragment, and in run_tests.sh, where it
#: is written directly.
_MARKER_RE = re.compile(r'-m\s+"([^"]+)"')

#: The summary line of `pytest --collect-only -q`, in both shapes:
#: `11710 tests collected in 7.00s`
#: `11467/11710 tests collected (243 deselected) in 10.70s`
_COLLECTED_RE = re.compile(r"^(\d+)(?:/(\d+))? tests collected", re.MULTILINE)


def _workflow_selectors() -> list[str]:
    """The marker expressions in the `Run tests` step, in source order.

    Order is the workflow's own: pull_request first, then workflow_dispatch.
    The third branch is the empty string (push to main) and carries no `-m`,
    so it is deliberately absent from this list.
    """
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["python"]["steps"]
    run_steps = [s for s in steps if s.get("name") == "Run tests"]
    assert len(run_steps) == 1, (
        f"expected exactly one 'Run tests' step in the Python job, "
        f"found {len(run_steps)}")
    return _MARKER_RE.findall(run_steps[0]["run"])


def _run_tests_selector(mode: str) -> str:
    """The marker expression `scripts/run_tests.sh <mode>` passes to pytest."""
    text = RUN_TESTS_PATH.read_text(encoding="utf-8")
    start = text.index(f"\n  {mode})")
    end = text.index("\n    ;;", start)
    found = _MARKER_RE.findall(text[start:end])
    assert len(found) == 1, (
        f"expected exactly one -m expression in run_tests.sh's {mode!r} arm, "
        f"found {found}")
    return found[0]


def test_the_pr_lane_is_spelled_identically_in_both_files():
    """A green `./scripts/run_tests.sh fast` is only worth something if it
    selected what CI's pull_request lane selects."""
    workflow = _workflow_selectors()
    assert workflow, "extracted no marker expressions from ci.yml"
    assert workflow[0] == _run_tests_selector("fast"), (
        f"ci.yml's pull_request lane selects {workflow[0]!r} but "
        f"run_tests.sh's `fast` selects {_run_tests_selector('fast')!r}. "
        "A green local run has stopped predicting the CI result.")


def test_the_nightly_lane_is_spelled_identically_in_both_files():
    workflow = _workflow_selectors()
    assert len(workflow) >= 2, (
        f"expected two marker expressions in ci.yml, found {workflow}")
    assert workflow[1] == _run_tests_selector("nightly"), (
        f"ci.yml's workflow_dispatch lane selects {workflow[1]!r} but "
        f"run_tests.sh's `nightly` selects {_run_tests_selector('nightly')!r}.")


def test_the_push_lane_still_applies_no_marker_filter():
    """The third branch of the computed selector is the empty string, so a push
    to main runs everything. Exactly two `-m` expressions is what says so."""
    workflow = _workflow_selectors()
    assert len(workflow) == 2, (
        f"expected exactly two -m expressions in the Run tests step (the "
        f"pull_request and workflow_dispatch lanes), found {workflow}. A third "
        "would mean the push lane stopped running the whole suite.")


def _collected(*marker_args: str) -> tuple[int, int]:
    """`(selected, total)` from a collect-only run. `total` equals `selected`
    when nothing was deselected."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", *marker_args],
        cwd=ROOT, capture_output=True, text=True)
    m = _COLLECTED_RE.search(proc.stdout)
    assert m is not None, (
        "could not parse a collection count from pytest's output; the summary "
        f"line format may have changed.\nstdout tail:\n{proc.stdout[-2000:]}")
    selected = int(m.group(1))
    return selected, int(m.group(2) or m.group(1))


@pytest.mark.slow
def test_every_node_is_in_exactly_one_lane():
    """The property the prose is actually about: a mistyped expression drops a
    node out of BOTH lanes, and a test that runs nowhere looks exactly like a
    test that passes.

    Marked slow because it collects the suite three times. That also puts it in
    the nightly lane, which is the lane whose exit code `scripts/nightly_eval.sh`
    folds into its verdict, so a partition that silently stops holding is caught
    by the run that cares most about it.
    """
    fast_expr, nightly_expr = _workflow_selectors()
    fast, total = _collected("-m", fast_expr)
    nightly, total_again = _collected("-m", nightly_expr)
    unfiltered, _ = _collected()

    assert total == total_again == unfiltered, (
        "the three collections disagree about how many tests exist "
        f"({total}, {total_again}, {unfiltered}); the tree changed underneath "
        "this test")
    assert fast + nightly == unfiltered, (
        f"the two lanes do not partition the suite: {fast} + {nightly} = "
        f"{fast + nightly}, but {unfiltered} tests exist. "
        f"{unfiltered - fast - nightly} node(s) run in NEITHER lane and would "
        "look exactly like passing tests.")
