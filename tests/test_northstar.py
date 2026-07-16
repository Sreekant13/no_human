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


class _JudgeBackend:
    """Fake backend: returns `replies` in order. A reply is a str (final_text,
    is_error=False) or a (str, is_error) tuple."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def run(self, prompt, *, cwd=None, max_turns=10, effort="high"):
        from no_human.agent.claude_backend import AgentResult
        r = self.replies[min(self.calls, len(self.replies) - 1)]
        text, is_err = r if isinstance(r, tuple) else (r, False)
        self.calls += 1
        return AgentResult(final_text=text, num_turns=1, is_error=is_err,
                           tokens_used=1, session_id="s", stop_reason="end_turn")


_GOOD_JUDGE = ('JUDGE_JSON_START\n{"satisfied": true, "evidence": "ok"}\n'
               'JUDGE_JSON_END')
# START but no END marker — the real "Stream closed" truncation signature; the
# regex needs both markers, so this parses to "no JUDGE_JSON block found".
_TRUNCATED_JUDGE = 'JUDGE_JSON_START\n{"satisfied": true, "evidence": "partial'


def _no_backoff(monkeypatch):
    monkeypatch.setattr("no_human.eval.judge._RETRY_BACKOFF_S", 0)


@pytest.mark.asyncio
async def test_goal_judge_retries_once_on_transient_empty_reply(monkeypatch):
    """An EMPTY judge reply (Stream-closed) is transient: retry once rather than
    fail-close and discard the spec's measurement (lost ns-44d180f9 in v3)."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend(["", _GOOD_JUDGE])   # empty first, valid on retry
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2 and v.satisfied is True


@pytest.mark.asyncio
async def test_goal_judge_retries_on_truncated_block_reply(monkeypatch):
    """The likelier real signature: a non-empty reply TRUNCATED before the END
    marker → 'no JUDGE_JSON block found' → also retried (covers _JUDGE_TRANSIENT[1])."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend([_TRUNCATED_JUDGE, _GOOD_JUDGE])
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2 and v.satisfied is True


@pytest.mark.asyncio
async def test_goal_judge_retries_on_backend_is_error(monkeypatch):
    """The STRUCTURED transient signal: the backend flags is_error (review D2) →
    retry, even if the (garbage) text didn't happen to match a sentinel string."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend([("stream error text", True), _GOOD_JUDGE])
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2 and v.satisfied is True


@pytest.mark.asyncio
async def test_goal_judge_fails_closed_after_the_retry(monkeypatch):
    """A PERSISTENT transient still fails closed after the one retry — the retry
    rescues a blip, it does not loop or credit a non-answer."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend(["", ""])    # empty both times
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 2            # tried exactly twice, no infinite loop
    assert v.satisfied is False     # still fails closed


@pytest.mark.asyncio
async def test_goal_judge_does_not_retry_a_malformed_block(monkeypatch):
    """A malformed-JSON reply (BOTH markers present, bad JSON) means the judge
    answered substantively — a format bug, not a transient. Do NOT retry."""
    from no_human.eval.judge import GoalJudge
    _no_backoff(monkeypatch)
    be = _JudgeBackend(["JUDGE_JSON_START\n{not json}\nJUDGE_JSON_END"])
    v = await GoalJudge(backend=be).judge(
        request="r", criteria=[], agent_diff="d", outcome_status="awaiting_approval")
    assert be.calls == 1            # no retry
    assert v.satisfied is False


@pytest.mark.asyncio
async def test_intent_judge_also_retries_on_transient(monkeypatch):
    """IntentJudge (the replay eval path, `nh eval replay`) has the SAME
    transient-retry as GoalJudge — review D3 consistency."""
    from no_human.eval.judge import IntentJudge
    _no_backoff(monkeypatch)
    good = 'JUDGE_JSON_START\n{"match": true, "evidence": "ok"}\nJUDGE_JSON_END'
    be = _JudgeBackend(["", good])   # empty first, valid on retry
    v = await IntentJudge(backend=be).judge(
        task_title="t", criteria=[], agent_diff="d", known_good_diff="ref")
    assert be.calls == 2 and v.match is True


@pytest.mark.asyncio
async def test_score_diffs_against_pr_branch_not_head(tmp_path):
    """The orchestrator can leave the work-dir HEAD at base while the coder's
    commits live on the PR branch — the runner must diff against `pr_branch` or a
    REAL PR is fed to the judge as an empty diff and scored a 'fabrication' (found
    live: ns-f5cb4cb0's 3 review files were committed on the PR branch, HEAD at
    base → empty diff → false failure)."""
    import subprocess as sp
    from no_human.core.task import TaskStatus

    work = tmp_path / "work"
    work.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        sp.run(["git", *a], cwd=work, check=True, capture_output=True)
    (work / "base.txt").write_text("x")
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "base"], cwd=work, check=True, capture_output=True)
    base_sha = sp.run(["git", "rev-parse", "HEAD"], cwd=work,
                      capture_output=True, text=True).stdout.strip()
    # coder work on the PR branch, then HEAD goes BACK to base (the bug scenario)
    sp.run(["git", "checkout", "-b", "pr-x"], cwd=work, check=True, capture_output=True)
    (work / "review.md").write_text("the review deliverable")
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "coder work"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "checkout", "main"], cwd=work, check=True, capture_output=True)

    class _Task:
        context = {"pr_branch": "pr-x"}

    class _Outcome:
        status = TaskStatus.AWAITING_APPROVAL
        detail = ""
        report = ""
        task = _Task()

    judge = _StubGoalJudge()
    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=judge)
    spec = BenchTask(id="ns-x", title="t", request="r",
                     repo={"path": str(work), "pin": "HEAD"})
    # sanity: base..HEAD (the OLD behaviour) would be empty
    old = sp.run(["git", "diff", base_sha, "HEAD"], cwd=work,
                 capture_output=True, text=True).stdout
    assert "review.md" not in old
    # sanity: the file is absent from the work dir while HEAD is at base
    assert not (work / "review.md").exists()
    await runner._score(spec, _Outcome(), work, base_sha, attempts=[], elapsed=1.0)
    assert "review.md" in judge.seen["agent_diff"]
    assert "the review deliverable" in judge.seen["agent_diff"]
    # the runner checked out the PR branch, so the judge's own ls/git checks in
    # repo_path now SEE the deliverable (not just the agent_diff).
    assert (work / "review.md").exists()
    assert (work / "review.md").read_text() == "the review deliverable"


@pytest.mark.asyncio
async def test_holdout_runs_against_pr_branch_not_base(tmp_path):
    """Review positive finding: the HOLDOUT tests must run against the coder's
    work (on the PR branch), not the base pin — on master they ran at base and
    force-failed a passing deliverable. #94's checkout makes them run on the branch."""
    import subprocess as sp
    from no_human.core.task import TaskStatus

    work = tmp_path / "work"
    work.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"]):
        sp.run(["git", *a], cwd=work, check=True, capture_output=True)
    (work / "app.py").write_text("def f():\n    return 1\n")   # base: f()==1
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "base"], cwd=work, check=True, capture_output=True)
    base_sha = sp.run(["git", "rev-parse", "HEAD"], cwd=work,
                      capture_output=True, text=True).stdout.strip()
    sp.run(["git", "checkout", "-b", "pr-x"], cwd=work, check=True, capture_output=True)
    (work / "app.py").write_text("def f():\n    return 2\n")   # coder: f()==2
    sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "commit", "-m", "coder"], cwd=work, check=True, capture_output=True)
    sp.run(["git", "checkout", "main"], cwd=work, check=True, capture_output=True)

    class _Task:
        context = {"pr_branch": "pr-x"}

    class _Outcome:
        status = TaskStatus.AWAITING_APPROVAL
        detail = ""
        report = ""
        task = _Task()

    runner = NorthStarRunner({}, backend_factory=lambda s: None, goal_judge=None)
    spec = BenchTask(id="ns-h", title="t", request="r",
                     repo={"path": str(work), "pin": "HEAD"},
                     holdout="from app import f\ndef test_f():\n    assert f() == 2\n")
    score = await runner._score(spec, _Outcome(), work, base_sha, attempts=[], elapsed=1.0)
    # The holdout passed ONLY because it ran against the coder's branch (f()==2),
    # not base (f()==1) — on master this was False, forcing goal_satisfied False.
    assert score.mergeable is True
    assert score.goal_satisfied is True


def test_render_shows_per_project_breakdown(tmp_path):
    """The suite spans 8 real repos — the operator asked for MULTIPLE projects, so
    the report must break cost/quality down BY PROJECT. `project` also survives the
    save/load JSON round-trip."""
    from no_human.eval.northstar import BenchScore
    from no_human.eval.northstar_card import NorthStarCard, render_northstar_md

    def mk(tid, proj, sat):
        return BenchScore(
            task_id=tid, title="t", outcome_status="awaiting_approval",
            goal_satisfied=sat, escalated_honestly=False, mergeable=None,
            nh_tokens=1, nh_cache_tokens=0, nh_cache_creation_tokens=0, nh_turns=1,
            nh_wall_clock_s=1.0, orig_tokens=1, orig_cache_tokens=0,
            orig_cache_creation_tokens=0, orig_wall_clock_s=1.0, orig_corrections=0,
            subset="core", project=proj)

    c = NorthStarCard(scores=[mk("ns-a", "metrics-core", True), mk("ns-b", "metrics-core", False),
                              mk("ns-c", "analytics-export", True)], created_at="x", label="t")
    md = render_northstar_md(c)
    assert "## Per-project" in md
    assert "| metrics-core | 2 | 1/2" in md and "| analytics-export | 1 | 1/1" in md
    p = tmp_path / "card.json"
    c.save(p)
    assert NorthStarCard.load(p).scores[0].project == "metrics-core"
