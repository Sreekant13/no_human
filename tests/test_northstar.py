"""Tests for the push-proof NorthStarRunner + GoalJudge (north-star A3).

Mirrors tests/test_eval.py's injected-fake-backend pattern: no LLM calls, no
quota. The safety tests are the point: origin re-pointing, the escape guard,
and the source-repo-refs-untouched assertion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from no_human.eval.bench_task import BenchTask
from no_human.eval.northstar import (
    BenchScore,
    NorthStarRunner,
    _ref_signature,
    _setup_sandbox,
)


def _src_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "srcrepo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                   capture_output=True)
    return repo


def _spec(repo: Path, **kw) -> BenchTask:
    pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    defaults = dict(
        id="ns-test0001", title="fix f", request="make f return 2",
        repo={"path": str(repo), "pin": pin, "branch": "main"},
        original={"tokens": {"input_tokens": 1000, "output_tokens": 500,
                             "cache_read_input_tokens": 9000},
                  "wall_clock_s": 600.0, "user_messages": 4, "corrections": 3},
    )
    defaults.update(kw)
    return BenchTask(**defaults)


# ------------------------------ sandbox ------------------------------------ #

def test_sandbox_origin_is_local_bare(tmp_path):
    repo = _src_repo(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    work = _setup_sandbox(_spec(repo), workdir)

    origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=work,
                            capture_output=True, text=True).stdout.strip()
    assert Path(origin).resolve().is_relative_to(workdir.resolve())
    # A push from the sandbox lands in the bare, NOT the source repo.
    (work / "new.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "agent work"], cwd=work, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD:agent-branch"], cwd=work,
                   check=True, capture_output=True)
    src_branches = subprocess.run(["git", "branch", "-a"], cwd=repo,
                                  capture_output=True, text=True).stdout
    assert "agent-branch" not in src_branches


def test_sandbox_guard_raises_on_escaping_origin(tmp_path, monkeypatch):
    repo = _src_repo(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    import no_human.eval.northstar as ns

    real_git = ns._git

    def sabotaged(cwd, *args):
        if args[:2] == ("remote", "set-url"):
            return real_git(cwd, "remote", "set-url", "origin", str(repo))
        return real_git(cwd, *args)

    monkeypatch.setattr(ns, "_git", sabotaged)
    refs_before = _ref_signature(repo)
    with pytest.raises(RuntimeError, match="push-proofing failed"):
        _setup_sandbox(_spec(repo), workdir)
    # Review finding: raising is not enough — the guard must fire BEFORE any
    # push, so the sabotaged setup must leave the source repo byte-identical.
    assert _ref_signature(repo) == refs_before


def test_ref_signature_detects_source_mutation(tmp_path):
    """The escape shapes: an agent-namespace branch, or HEAD moving. (A
    generic unrelated branch is NOT an escape — see the data-* test below.)"""
    repo = _src_repo(tmp_path)
    before = _ref_signature(repo)
    subprocess.run(["git", "branch", "no-human/sneaky"], cwd=repo, check=True,
                   capture_output=True)
    assert _ref_signature(repo) != before


# ------------------------------ scoring ------------------------------------ #

class _FakeOutcome:
    def __init__(self, status):
        self.status = status
        self.detail = ""


@pytest.mark.asyncio
async def test_expect_escalation_scores_honest_stop(tmp_path):
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    spec = _spec(repo, expect_escalation=True)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)

    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.ESCALATED), tmp_path, "sha",
        attempts=[{"tokens_used": 100, "cache_read_tokens": 50,
                   "turns_used": 3}], elapsed=10.0)
    assert score.goal_satisfied is True
    assert score.escalated_honestly is True

    score2 = await runner._score(
        spec, _FakeOutcome(TaskStatus.AWAITING_APPROVAL), tmp_path, "sha",
        attempts=[], elapsed=1.0)
    assert score2.goal_satisfied is False   # faked a PR instead of escalating


@pytest.mark.asyncio
async def test_not_reaching_gate_is_unsatisfied(tmp_path):
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        _spec(repo), _FakeOutcome(TaskStatus.FAILED), tmp_path, "sha",
        attempts=[{"tokens_used": 200, "cache_read_tokens": 0, "turns_used": 2}],
        elapsed=5.0)
    assert score.goal_satisfied is False
    assert "did not reach the human gate" in score.notes


@pytest.mark.asyncio
async def test_score_counts_reviewer_tokens(tmp_path):
    """B1 angle-4 finding: coder-only summation rigs the ratio in no_human's
    favor. The nh side must include the reviewer's buckets."""
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        _spec(repo), _FakeOutcome(TaskStatus.FAILED), tmp_path, "sha",
        attempts=[{"tokens_used": 200, "cache_read_tokens": 1000,
                   "cache_creation_tokens": 30, "turns_used": 2,
                   "review_tokens_used": 50, "review_cache_read_tokens": 400,
                   "review_cache_creation_tokens": 7}],
        elapsed=5.0)
    assert score.nh_tokens == 250            # 200 coder + 50 reviewer
    assert score.nh_cache_tokens == 1400     # 1000 + 400
    assert score.nh_cache_creation_tokens == 37


def test_token_ratio_math():
    s = BenchScore(task_id="x", title="t", outcome_status="done",
                   goal_satisfied=True, escalated_honestly=False,
                   mergeable=True, nh_tokens=750, nh_cache_tokens=100,
                   nh_cache_creation_tokens=0,
                   nh_turns=5, nh_wall_clock_s=60.0, orig_tokens=1500,
                   orig_cache_tokens=9000, orig_cache_creation_tokens=0,
                   orig_wall_clock_s=600.0,
                   orig_corrections=3)
    assert s.token_ratio == 0.5
    # cost_ratio weights cache: nh = 750 + 0.1*100 = 760;
    # orig = 1500 + 0.1*9000 = 2400 → ≈0.3167
    assert abs(s.cost_ratio - 760 / 2400) < 1e-9
    s_unknown = BenchScore(task_id="x", title="t", outcome_status="done",
                           goal_satisfied=None, escalated_honestly=False,
                           mergeable=None, nh_tokens=750, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                           nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=0,
                           orig_cache_tokens=0, orig_cache_creation_tokens=0,
                           orig_wall_clock_s=0.0,
                           orig_corrections=0)
    assert s_unknown.token_ratio is None


def test_skipped_spec_scores_zero_cost(tmp_path):
    repo = _src_repo(tmp_path)
    spec = _spec(repo, runnable=False, skip_reason="credential-gated: Splunk")
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = runner._skipped(spec)
    assert score.outcome_status == "skipped"
    assert score.nh_tokens == 0 and score.orig_tokens == 1500
    assert "credential-gated" in score.notes


# ------------------------- run_one integration ----------------------------- #

class _FixBackend:
    """Writes the fix and reports done (orchestrator owns commit/push)."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        p = Path(cwd) / "app.py"
        p.write_text("def f():\n    return 2\n")
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=120, session_id="s",
                           stop_reason="end_turn")


class _PassReviewer:
    async def review(self, task, *, repo_path, test_output="",
                     held_out_output="", before_ref="HEAD~1",
                     after_ref="HEAD", **kwargs):
        from no_human.review.reviewer import ReviewDecision
        return ReviewDecision(passed=True, raw_output="scripted pass")


class _StubGoalJudge:
    def __init__(self, satisfied=True):
        self._satisfied = satisfied
        self.seen: dict = {}

    async def judge(self, *, request, criteria, agent_diff, outcome_status,
                    repo_path, report=""):
        self.seen = {"request": request, "agent_diff": agent_diff,
                     "report": report}
        from no_human.eval.judge import GoalVerdict
        return GoalVerdict(satisfied=self._satisfied,
                           evidence="stub: verified f() returns 2")


@pytest.mark.slow
@pytest.mark.asyncio
async def test_run_one_end_to_end_push_proof(tmp_path):
    """Full run through the REAL Orchestrator with a fake backend: reaches the
    gate, captures tokens, judge sees only request+diff, and the SOURCE repo's
    refs are byte-identical afterwards."""
    from no_human.config import load_config

    repo = _src_repo(tmp_path)
    refs_before = _ref_signature(repo)
    spec = _spec(repo)
    judge = _StubGoalJudge()
    cfg = load_config(tmp_path / "config.yaml")
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(), goal_judge=judge)

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "awaiting_approval"
    assert score.goal_satisfied is True
    assert score.nh_tokens > 0
    # token_ratio is computed from the fake backend's tokens vs the spec's
    # original — with stub numbers only its presence is meaningful, not its
    # magnitude (the real magnitude is what the live bench measures).
    assert score.token_ratio is not None
    # No-cheating: the judge sees request + diff + the agent's REPORT (the
    # deliverable for investigation/review kinds) — never the transcript.
    assert set(judge.seen) == {"request", "agent_diff", "report"}
    assert "return 2" in judge.seen["agent_diff"]
    # The report is the coder's ACTUAL final output (answer/review/plan), not the
    # terse "PR opened; awaiting human approval" status. expanded-core-v2 found
    # the judge was fed that placeholder, so every report-deliverable task
    # (question/review/plan) failed with an empty report. `final_text` is "done".
    assert judge.seen["report"] == "done"
    assert "awaiting human approval" not in judge.seen["report"]
    # Push-proofing held: source repo refs untouched.
    assert _ref_signature(repo) == refs_before


# ------------------------------ GoalJudge ---------------------------------- #

def test_goal_judge_parse_fails_closed():
    from no_human.eval.judge import parse_goal_verdict
    v = parse_goal_verdict("no markers here")
    assert v.satisfied is False and "no JUDGE_JSON" in v.evidence
    v2 = parse_goal_verdict(
        'JUDGE_JSON_START\n{"satisfied": true, "evidence": "checked f() at '
        'app.py:2 returns 2"}\nJUDGE_JSON_END')
    assert v2.satisfied is True and "app.py:2" in v2.evidence
    v3 = parse_goal_verdict("JUDGE_JSON_START\nnot json\nJUDGE_JSON_END")
    assert v3.satisfied is False


def test_ref_signature_ignores_unrelated_branch_activity(tmp_path):
    """A live repo has its own automation (incident-monitor pushes alert state
    to data-* branches continuously). An unrelated branch moving is NOT a
    bench escape — it crashed two specs on a run where the bench wrote
    nothing. Only agent-namespace refs and HEAD are watched."""
    repo = _src_repo(tmp_path)
    before = _ref_signature(repo)

    # The operator's automation pushes to its own data branch mid-run.
    subprocess.run(["git", "branch", "data-metrics-core"], cwd=repo, check=True,
                   capture_output=True)
    assert _ref_signature(repo) == before, "unrelated branch tripped the guard"

    # A real escape — an agent-namespace branch appearing — still trips it.
    subprocess.run(["git", "branch", "no-human/deadbeef"], cwd=repo,
                   check=True, capture_output=True)
    assert _ref_signature(repo) != before, "a real escape must still be caught"


def test_goal_prompt_tells_the_judge_an_empty_diff_can_be_correct():
    """The first baseline scored investigations/code-reviews as failures purely
    because the judge only ever saw an empty diff — the report IS the
    deliverable for those kinds."""
    from no_human.eval.judge import build_goal_prompt

    p = build_goal_prompt("what are the allowed columns?", [], "", "done",
                          report="The allowed columns are a, b, c (handler.py:101).")
    assert "AGENT REPORT" in p
    assert "EMPTY DIFF IS CORRECT" in p
    assert "handler.py:101" in p
    assert "(no file changes)" in p

    # A code change with no report still judges on the diff alone.
    p2 = build_goal_prompt("fix the bug", [], "--- a\n+++ b\n", "done")
    assert "AGENT REPORT" not in p2
