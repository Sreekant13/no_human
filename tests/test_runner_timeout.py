"""The test runner's timeout budget (2026-07-11 incident)."""

import inspect

from no_human.testing import runner


def test_run_tests_timeout_has_room_for_worktree_suites():
    """600s was shorter than a real suite in a fresh worktree venv under
    parallel load — every task of the first parallel run failed as
    '0 passed, 0 failed, 1 errors': the TIMEOUT, parsed as an error,
    invisible until failure events carried their output."""
    sig = inspect.signature(runner.run_tests)
    assert sig.parameters["timeout"].default >= 1800
