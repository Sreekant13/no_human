"""Verifiers (review/verifiers.py) wired into `Orchestrator._run_review`.

Verifiers run BEFORE the agentic reviewer, merge into the round monotonically
(a passing verifier adds nothing; a failing one — `no_verdict` included —
ends the round without spending reviewer tokens), and every verdict is
persisted to `attempts.verifier_results` and `task.context["verifier_results"]`
keyed by the commit sha they judged.

`FakeReviewer` below implements BOTH `_run_bounded` (the verifier judge's
call — a DIFFERENT method than `.review()`) and `.review()` (the existing
agentic-reviewer chokepoint, reached only when the gate's verifiers all
pass or are skipped). Counting both separately is what lets these tests
assert "the reviewer never ran" while a verifier judge call still did.
"""

import json
import subprocess
from pathlib import Path

import pytest

from no_human.agent.backend import AgentResult
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision as RD
from no_human.review.reviewer import ReviewerUnavailable
from no_human.review.selfcheck import ChecklistItem as CI
from no_human.vcs.git import GitRepo

from .test_e2e_orchestrator import _config  # noqa: F401


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_a_verifier(tmp_path: Path, yaml_text: str) -> Path:
    """A repo with a `.no_human/verifiers.yaml` and one committed .py change
    over the seed commit — the shape `select()` needs to pick a rule up."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "src.py").write_text("def f():\n    return 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "checkout", "-qb", "feat/x")

    d = work / ".no_human"
    d.mkdir(parents=True, exist_ok=True)
    (d / "verifiers.yaml").write_text(yaml_text)
    (work / "src.py").write_text("def f():\n    return 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "the change")
    return work


VERIFIER_YAML = (
    "verifiers:\n"
    "  - id: no-todo\n"
    "    statement: The diff introduces no TODO comments.\n"
    "    paths: ['**/*.py']\n"
    "    severity: high\n"
)


def _ok_json(*, passed: bool, evidence: str = "e", file: str = "", line: int = 0) -> str:
    return _ok_json_for("no-todo", passed=passed, evidence=evidence, file=file, line=line)


def _ok_json_for(
    verifier_id: str, *, passed: bool, evidence: str = "e", file: str = "", line: int = 0,
) -> str:
    return (
        "VERIFIER_JSON_START\n"
        f'{{"verifier_id": {json.dumps(verifier_id)}, "passed": {"true" if passed else "false"}, '
        f'"evidence": {json.dumps(evidence)}, "file": {json.dumps(file)}, '
        f'"line": {line}, "comment": "c"}}\n'
        "VERIFIER_JSON_END"
    )


class FakeReviewer:
    """Both chokepoints, independently counted. `bounded_outcome` is either:
      - a plain string (final_text, wrapped into an `AgentResult`),
      - a 2-tuple ``(AgentResult | None, reason)`` returned verbatim — how a
        test simulates the backend timing out (`(None, "timed out")`), or
      - a LIST of either of the above, consumed one entry per `_run_bounded`
        call (the last entry repeats once the list is exhausted) — how a
        test sequences "no verdict, then a real verdict" across the judge's
        one bounded retry."""

    _on_event = None

    def __init__(self, bounded_outcome, review_decision=None):
        self.bounded_outcome = bounded_outcome
        self.review_decision = review_decision or RD(
            passed=True, checklist=[CI("it holds", True, "evidence")])
        self.bounded_calls = 0
        self.review_calls = 0

    async def _run_bounded(self, prompt, repo_path, *, max_turns, timeout, on_event):
        self.bounded_calls += 1
        outcome = self.bounded_outcome
        if isinstance(outcome, list):
            idx = min(self.bounded_calls - 1, len(outcome) - 1)
            outcome = outcome[idx]
        if isinstance(outcome, tuple):
            return outcome
        result = AgentResult(
            final_text=outcome, num_turns=1, is_error=False,
            tokens_used=500, session_id="s", stop_reason="end_turn",
            cache_read_tokens=10, cache_creation_tokens=5, output_tokens=100,
        )
        return result, "ok"

    async def review(self, task, **kw):
        self.review_calls += 1
        return self.review_decision


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path, reviewer, *, tests_cmd="true"):
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    cfg.data.setdefault("tests", {})["command"] = tests_cmd
    from no_human.core.orchestrator import Orchestrator as _Orch
    from .test_e2e_orchestrator import FakeBackend
    return _Orch(store, cfg.data, FakeBackend(lambda cwd: None),
                 SlackNotifier(None), reviewer=reviewer)


async def test_all_satisfied_runs_the_agentic_reviewer_and_records_every_verdict(
    store, tmp_path,
):
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer(_ok_json(passed=True))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.review_calls == 1, "a satisfied verifier must not skip the reviewer"
    assert reviewer.bounded_calls == 1
    assert decision.passed is True
    assert len(decision.verifiers) == 1
    assert decision.verifiers[0]["verifier_id"] == "no-todo"
    assert decision.verifiers[0]["passed"] is True
    assert decision.as_dict()["verifiers"] == decision.verifiers


async def test_a_failing_verifier_ends_the_round_before_the_reviewer(store, tmp_path):
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer(_ok_json(passed=False, evidence="found a TODO", file="src.py", line=1))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert decision.passed is False
    assert reviewer.review_calls == 0, (
        "a failing verifier must short-circuit the round before the agentic "
        "reviewer ever runs")
    assert reviewer.bounded_calls == 1
    (item,) = decision.checklist
    assert item.label == "rule:no-todo"
    assert item.passed is False

    (round_1,) = task.context["review_history"]
    assert round_1["sha"] == repo.head_sha()


async def test_no_verdict_escalates_instead_of_failing_the_round(store, tmp_path):
    """This used to be `test_no_verdict_fails_closed`, and its old name told
    the whole bug: a judge that never reaches a verdict was rendered as a
    FAILING checklist item — a defect nobody found, billed to the coder as
    one to fix. That is the exact anti-pattern `reviewer.py`'s
    `ReviewerUnavailable` exists to stop for the agentic reviewer; this test
    now asserts the verifier gate mirrors it: one bounded retry, and if the
    retry ALSO reaches no verdict, the round escalates (raises) instead of
    returning a failing `ReviewDecision`."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer((None, "timed out"))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    with pytest.raises(ReviewerUnavailable):
        await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 2, (
        "one bounded retry — not zero (would skip the retry) and not more "
        "(would keep retrying an unavailable judge forever)")
    assert reviewer.review_calls == 0, (
        "an unavailable verifier must never reach the agentic reviewer")


async def test_no_verdict_then_a_real_verdict_uses_the_retry_result(store, tmp_path):
    """The retry is not decorative: if it comes back with an actual verdict,
    that verdict is what governs the round — the round must NOT escalate,
    and (since the verifier is satisfied) must proceed to the agentic
    reviewer exactly as if the first call had succeeded."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer([(None, "timed out"), _ok_json(passed=True)])
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 2, "the retry is the second bounded call"
    assert reviewer.review_calls == 1, (
        "a verdict recovered on retry is a satisfied verifier — the round "
        "must proceed to the agentic reviewer, not escalate")
    assert decision.passed is True
    assert decision.verifiers[0]["passed"] is True
    assert decision.verifiers[0]["unavailable"] is False
    assert decision.verifiers[0]["no_verdict"] is False


MIXED_VERIFIER_YAML = (
    "verifiers:\n"
    "  - id: no-todo\n"
    "    statement: The diff introduces no TODO comments.\n"
    "    paths: ['**/*.py']\n"
    "    severity: high\n"
    "  - id: other-rule\n"
    "    statement: Some other rule entirely.\n"
    "    paths: ['**/*.py']\n"
    "    severity: medium\n"
)


async def test_a_genuine_failure_is_never_swallowed_by_an_unavailable_sibling(
    store, tmp_path,
):
    """A round can have BOTH a genuinely failing verifier and one that never
    reaches a verdict (even after retry). The genuine failure must still
    fail the round — an early/naive "any unavailable verifier escalates"
    check would silently drop it on the floor just because another rule in
    the same round happened to be unavailable. The unavailable rule must
    stay visible (advisory), never rendered as passing, and the round must
    NOT raise `ReviewerUnavailable` — a real finding exists, so this is a
    genuine review FAIL, not an infra escalation."""
    work = _repo_with_a_verifier(tmp_path, MIXED_VERIFIER_YAML)
    repo = GitRepo(work)
    # Verifiers run in YAML order: "no-todo" gets one call (genuine failure,
    # parseable JSON, no retry needed); "other-rule" then gets two calls
    # (first + retry), both with no parseable marker — unavailable.
    reviewer = FakeReviewer([
        _ok_json_for("no-todo", passed=False, evidence="found a TODO", file="src.py", line=1),
        "no marker in this response at all",
        "still no marker on the retry",
    ])
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert decision.passed is False, "a genuine failure must still fail the round"
    assert reviewer.review_calls == 0, (
        "a failing round (genuine or not) never reaches the agentic reviewer")
    assert reviewer.bounded_calls == 3

    by_label = {item.label: item for item in decision.checklist}
    assert by_label["rule:no-todo"].passed is False
    assert by_label["rule:no-todo"].severity == "high", (
        "the genuine failure keeps its authored severity, unaffected by the "
        "unavailable sibling")
    assert by_label["rule:other-rule"].passed is False, (
        "an unavailable rule must never render as satisfied, even when "
        "folded into a failing round")
    assert by_label["rule:other-rule"].severity == "low", (
        "an unavailable rule is advisory — it must not itself have failed "
        "the round (the no-todo rule did)")


async def test_no_yaml_skips_and_runs_the_reviewer(store, tmp_path):
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    # Remove the yaml the fixture wrote — simulate "no verifiers configured".
    (work / ".no_human" / "verifiers.yaml").unlink()
    repo = GitRepo(work)
    reviewer = FakeReviewer(_ok_json(passed=True))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 0, "no yaml means no judge call at all"
    assert reviewer.review_calls == 1
    assert decision.verifiers == []
    assert decision.passed is True


async def test_disabled_skips_and_runs_the_reviewer(store, tmp_path):
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer(_ok_json(passed=True))
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    cfg.data.setdefault("tests", {})["command"] = "true"
    cfg.data["verifiers"] = {"enabled": False}
    from no_human.core.orchestrator import Orchestrator as _Orch
    from .test_e2e_orchestrator import FakeBackend
    orch = _Orch(store, cfg.data, FakeBackend(lambda cwd: None),
                 SlackNotifier(None), reviewer=reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 0
    assert reviewer.review_calls == 1
    assert decision.verifiers == []


async def test_none_selected_for_the_changed_paths_skips(store, tmp_path):
    yaml_text = (
        "verifiers:\n"
        "  - id: docs-only\n"
        "    statement: Docs are formatted.\n"
        "    paths: ['docs/**/*.md']\n"
        "    severity: low\n"
    )
    work = _repo_with_a_verifier(tmp_path, yaml_text)  # changes src.py, not docs/
    repo = GitRepo(work)
    reviewer = FakeReviewer(_ok_json(passed=True))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 0
    assert reviewer.review_calls == 1
    assert decision.verifiers == []


async def test_verifier_spend_lands_on_the_review_usage_columns(store, tmp_path):
    """AC 5. The pass path folds verifier spend into `decision`, which the
    caller's `_record_review_usage` (a SET, not additive) then writes — so
    the spend must be inside `decision`, never a second DB write. The fail
    path must ALSO carry the spend, since the reviewer never runs to add its
    own."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)

    # Pass path: reviewer contributes its own tokens too.
    reviewer = FakeReviewer(
        _ok_json(passed=True),
        review_decision=RD(passed=True, checklist=[CI("ok", True, "e")], tokens_used=1000),
    )
    orch = _orch(store, tmp_path, reviewer)
    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)
    decision = await orch._run_review(task, repo, attempt_id, base="main")
    assert decision.tokens_used == 1500, (
        "verifier spend (500) did not fold into the reviewer's own 1000")

    # Fail path: no reviewer contribution at all, but verifier spend still lands.
    reviewer2 = FakeReviewer(_ok_json(passed=False, evidence="e", file="src.py", line=1))
    orch2 = _orch(store, tmp_path, reviewer2)
    task2 = Task.new("t2", repo_path=str(work))
    await store.create_task(task2)
    attempt_id2 = await store.create_attempt(task2.id, 1)
    decision2 = await orch2._run_review(task2, repo, attempt_id2, base="main")
    assert decision2.tokens_used == 500


async def test_verifier_results_persist_on_the_attempt_row_and_in_task_context(
    store, tmp_path,
):
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer(_ok_json(passed=True))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    await orch._run_review(task, repo, attempt_id, base="main")

    head = repo.head_sha()
    assert task.context["verifier_results"][head][0]["verifier_id"] == "no-todo"

    row = await store._fetchone(
        "SELECT verifier_results FROM attempts WHERE id = ?", (attempt_id,))
    assert row is not None
    stored = json.loads(row[0])
    assert stored[0]["verifier_id"] == "no-todo"
    assert stored[0]["passed"] is True
