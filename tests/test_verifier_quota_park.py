"""RED-first tests: a verifier-gate judge call that dies on a session/weekly
usage-limit error must be routed through the SAME quota classifier the coder
path already uses (``no_human.core.bounds.quota_signal`` /
``quota_reason``), so the attempt PARKS as ``paused_quota`` instead of being
booked as ``infra: no-verdict`` — which today burns the verifier's one bounded
retry and can escalate a ``NOVEL_UNKNOWN`` "wall-killed-verifier" blocker to a
human for something that is not a bug, just a subscription limit.

Covers acceptance criterion 1 (and its negative control): a verifier round
dying on the exact live quota string parks the attempt as ``paused_quota``
and does not consume the bounded retry or raise a reviewer-unavailable
escalation, while a genuinely unparseable/broken judge response (no quota
phrase) keeps today's behaviour untouched.
"""

import pytest

from no_human.core.bounds import QuotaExhausted
from no_human.core.task import Task, TaskStatus
from no_human.review.reviewer import ReviewerUnavailable
from no_human.review.verifiers import Verifier, run_verifiers

from .test_e2e_orchestrator import FakeBackend, _config, _git, bare_repo, store  # noqa: F401
from .test_verifiers_gate import FakeReviewer, VERIFIER_YAML, _orch, _repo_with_a_verifier

_SESSION_LIMIT_TEXT = (
    "You've hit your session limit · resets 4:20am (Asia/Jerusalem)"
)

_DIFF_TEXT = (
    "diff --git a/src.py b/src.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src.py\n"
    "+++ b/src.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
    "+    # TODO fix\n"
)

_VERIFIER = Verifier(id="v1", statement="no TODOs", paths=("**/*.py",))


# ── unit: run_verifiers / _judge_once must propagate QuotaExhausted ────── #

async def test_judge_quota_error_propagates_not_no_verdict():
    """A judge callable that raises the exact live quota-limit string must
    surface as QuotaExhausted, never get swallowed into a deterministic
    no_verdict VerifierResult (today's behaviour: any BaseException from the
    judge becomes ``no verdict: judge raised Exception``)."""

    async def judge(prompt: str):
        raise Exception(_SESSION_LIMIT_TEXT)

    with pytest.raises(QuotaExhausted):
        await run_verifiers(
            judge,
            verifiers=[_VERIFIER],
            diff_text=_DIFF_TEXT,
            read_file=lambda path: "",
            changed_paths=["src.py"],
        )


async def test_judge_non_quota_error_still_no_verdict():
    """Negative control: a genuine, non-quota judge failure keeps today's
    no_verdict behaviour — this must never regress."""

    async def judge(prompt: str):
        raise RuntimeError("transport reset by peer")

    results = await run_verifiers(
        judge,
        verifiers=[_VERIFIER],
        diff_text=_DIFF_TEXT,
        read_file=lambda path: "",
        changed_paths=["src.py"],
    )

    assert len(results) == 1
    result = results[0]
    assert result.no_verdict is True
    assert "judge raised" in result.evidence


# ── orchestrator gate: _run_review must raise QuotaExhausted, not escalate ── #

async def test_verifier_quota_wall_raises_instead_of_escalating(store, tmp_path):
    """Mirrors test_verifiers_gate.py's own
    `test_no_verdict_escalates_instead_of_failing_the_round`, but the bounded
    judge dies on the exact live session-limit string instead of a plain
    timeout. That must never reach `ReviewerUnavailable` — it must raise
    `QuotaExhausted` on the FIRST call, without spending the one bounded
    retry the genuine-infra case is entitled to."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    from no_human.vcs.git import GitRepo
    repo = GitRepo(work)
    reviewer = FakeReviewer((None, _SESSION_LIMIT_TEXT))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    with pytest.raises(QuotaExhausted):
        await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 1, (
        "a quota wall must not consume the verifier's bounded retry — the "
        "retry exists for genuine infra flakiness, not a subscription limit")
    assert reviewer.review_calls == 0, (
        "a quota-parked round must never reach the agentic reviewer")


async def test_verifier_non_quota_no_verdict_still_escalates(store, tmp_path):
    """Negative control: a genuinely unparseable/broken verifier judge (no
    quota phrase) must keep today's behaviour — no-verdict, bounded retry
    consumed, and (after exhausting the retry) `ReviewerUnavailable` —
    never silently reclassified as a quota park."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    from no_human.vcs.git import GitRepo
    repo = GitRepo(work)
    reviewer = FakeReviewer((None, "timed out"))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    with pytest.raises(ReviewerUnavailable):
        await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 2, (
        "the bounded retry must still be spent on a genuine no-verdict")


# ── full pipeline: the whole task parks paused_quota, not FAILED/escalated ── #

async def test_verifier_session_limit_parks_the_whole_task(store, bare_repo, tmp_path):
    """Acceptance criterion 1, end-to-end: a coder attempt that produces a
    real diff, then hits the verifier gate where the judge dies on the exact
    live session-limit string, must park the WHOLE task as PAUSED_QUOTA — not
    fail the attempt, not consume the bounded retry, not raise a
    NOVEL_UNKNOWN reviewer-unavailable escalation."""
    (bare_repo / ".no_human").mkdir(parents=True, exist_ok=True)
    (bare_repo / ".no_human" / "verifiers.yaml").write_text(VERIFIER_YAML)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-qm", "add verifiers.yaml")
    _git(bare_repo, "push", "origin", "main")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    reviewer = FakeReviewer((None, _SESSION_LIMIT_TEXT))
    events = []
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer, event_sink=events.append)

    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)
    parked = await store.get_task(t.id)

    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"expected the task to park on the quota wall, got {outcome.status}: "
        f"{getattr(outcome, 'detail', None)}"
    )
    assert parked.blocker is None or parked.blocker.get("type") != "NOVEL_UNKNOWN", (
        "a session-limit quota wall must never surface as a NOVEL_UNKNOWN escalation"
    )
    used_attempts, _, _ = await store.lifetime_usage_by_class(t.id)
    assert used_attempts == 0, "a quota park must not consume the bounded retry"
    assert reviewer.bounded_calls == 1
