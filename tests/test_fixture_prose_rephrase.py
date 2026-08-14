"""Repro/regression guard for the recreation-directive fixture-prose needle
this task removes (see the task description / PLAN.md — not spelled here on
purpose, so this guard is never itself a new hit of the thing it checks for,
and neither is its own filename: an earlier draft of this guard's path
itself matched the needle regex below, so it was renamed — every tracked
path is scanned along with file contents, and a guard is not exempt from
its own check).

Ahead of the history recreation (operator directive 2026-08-12), four SHIPPED
files carried employer-context phrasing as test-fixture prose that a new
recreation-directive needle over git history would catch (the live publish
guard in `vendor_terms.py` does not — it only covers a different, employer-
term inventory). This test makes the absence of that phrasing a live,
automated check instead of a one-off grep: none of the four files (nor the
frozen recall control-case's on-disk copy of the same prose) may spell it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Built from parts rather than a literal, so this file's own source is never
#: a fresh hit of the needle it scans for.
_NEEDLE = re.compile("data" + "[ _/-]" + "export", re.IGNORECASE)

#: The four shipped fixture files named by the recreation-prerequisite task,
#: plus the frozen recall control-case's on-disk copy of the same prose
#: (mirrors tests/test_bench_publish.py:551 pre-sweep; regenerated only via
#: `eval/reviewer_recall/materialize_base.py`, never hand-edited).
_CHECKED_PATHS = [
    "tests/test_eval_acts.py",
    "tests/test_classify.py",
    "tests/test_bench_publish.py",
    "eval/reviewer_recall/cases/control-bench-baseline/base/tests/test_bench_publish.py",
]


def test_fixture_prose_no_longer_spells_the_needle():
    violations = []
    for relpath in _CHECKED_PATHS:
        text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _NEEDLE.search(line):
                violations.append(f"{relpath}:{lineno}")
    assert violations == [], (
        "these lines still spell the recreation-directive needle this task "
        f"rephrases: {violations}")
    # The guard's own path must never become a fresh hit of the needle it
    # scans for (the exact defect that sent an earlier draft of this file
    # back — its path matched its own regex). Checked here rather than as a
    # second test function, to keep this file to a single new test.
    assert not _NEEDLE.search(str(Path(__file__)))
