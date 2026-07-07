"""Tests for Track E: ponytail discipline in worker prompt and reviewer."""

from no_human.core.task import Task, TaskStatus


def _task(**kw):
    defaults = dict(
        id="aaa", source="test", title="Fix bug",
        status=TaskStatus.IMPLEMENTING,
        acceptance_criteria=["Bug is fixed", "Tests pass"],
    )
    defaults.update(kw)
    return Task(**defaults)


def test_worker_prompt_contains_ponytail_rule():
    """E1: worker prompt must contain the 'smallest change' ponytail rule."""
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    prompt = orch._build_implement_prompt(_task(), "/tmp/repo")
    assert "SMALLEST change" in prompt
    assert "speculative abstraction" in prompt
    assert "framework" in prompt


def test_reviewer_small_diff_no_scope_pass():
    """E2: small diffs (<150 lines) should NOT get the SCOPE pass."""
    from no_human.review.reviewer import _build_review_prompt
    task = _task()
    small_diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    prompt = _build_review_prompt(task, small_diff, "all passed", "")
    assert "SCOPE" not in prompt
    assert "STAGE 1" in prompt
    assert "CODE QUALITY" in prompt


def test_reviewer_large_diff_gets_scope_pass():
    """E2: large diffs (>150 lines) should trigger the SCOPE over-engineering pass."""
    from no_human.review.reviewer import _build_review_prompt
    task = _task()
    # Build a diff with >150 newlines
    large_diff = "\n".join([f"+line {i}" for i in range(200)])
    prompt = _build_review_prompt(task, large_diff, "all passed", "")
    assert "PASS 4: SCOPE" in prompt
    assert "SMALLEST change" in prompt
    assert "unnecessary abstractions" in prompt


def test_reviewer_scope_pass_threshold_boundary():
    """E2: exactly 150 newlines should NOT trigger the scope pass."""
    from no_human.review.reviewer import _build_review_prompt
    task = _task()
    diff = "\n".join([f"+line {i}" for i in range(150)])  # 149 newlines
    prompt = _build_review_prompt(task, diff, "all passed", "")
    assert "PASS 4: SCOPE" not in prompt

    diff_151 = "\n".join([f"+line {i}" for i in range(152)])  # 151 newlines
    prompt2 = _build_review_prompt(task, diff_151, "all passed", "")
    assert "PASS 4: SCOPE" in prompt2
