"""Tests for C2: structured handoff artifact persistence and resume injection."""

from no_human.core.task import Task, TaskStatus


def _task(**kw):
    defaults = dict(
        id="abc123", source="test", title="Fix the widget",
        status=TaskStatus.IMPLEMENTING,
        acceptance_criteria=["Widget works", "Tests pass"],
    )
    defaults.update(kw)
    return Task(**defaults)


def test_resume_digest_includes_handoff():
    """When task.context has a handoff, _resume_digest should surface it."""
    from no_human.core.orchestrator import Orchestrator
    # Minimal orchestrator — we only need the _resume_digest method.
    orch = object.__new__(Orchestrator)
    task = _task(context={
        "handoff": {
            "summary": "Implemented the widget parser but tests are still failing on edge case X.",
            "changed_files": ["src/widget.py", "tests/test_widget.py"],
            "commit": "deadbeef",
            "turns_used": 38,
        }
    })
    digest = orch._resume_digest(task)
    assert "previous attempt ran out of turns" in digest
    assert "38" in digest
    assert "widget parser" in digest
    assert "src/widget.py" in digest


def test_resume_digest_no_handoff():
    """Without a handoff, _resume_digest should not crash or mention handoff."""
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)
    task = _task(context={})
    digest = orch._resume_digest(task)
    assert "previous attempt ran out of turns" not in digest


def test_resume_digest_handoff_plus_blocker():
    """Handoff and blocker info should both appear."""
    from no_human.core.orchestrator import Orchestrator
    from no_human.blockers import Blocker, BlockerCategory
    orch = object.__new__(Orchestrator)
    blocker = Blocker(
        category=BlockerCategory.AMBIGUITY,
        root_cause_hypothesis="need DB schema",
    )
    task = _task(
        blocker=blocker.to_dict(),
        context={
            "handoff": {
                "summary": "Partial impl done.",
                "changed_files": [],
                "commit": "",
                "turns_used": 40,
            }
        },
    )
    digest = orch._resume_digest(task)
    assert "previous attempt ran out of turns" in digest
    assert "missing_context" in digest.lower() or "need DB schema" in digest


def test_resume_digest_review_feedback_preserved():
    """Existing review feedback injection should still work alongside handoff."""
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)
    task = _task(context={
        "review_feedback": [
            {"label": "Missing null check", "file": "src/a.py", "line": 10,
             "comment": "Add null check"},
        ],
        "handoff": {
            "summary": "Done except for reviewer findings.",
            "changed_files": ["src/a.py"],
            "commit": "abc",
            "turns_used": 35,
        },
    })
    digest = orch._resume_digest(task)
    assert "Missing null check" in digest
    assert "previous attempt ran out of turns" in digest
