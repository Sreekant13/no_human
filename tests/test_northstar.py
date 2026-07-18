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
        # The setup re-points origin via `remote add` (post-clonefile it may
        # not exist to set-url) — redirect that add at the REAL repo to
        # simulate an escape.
        if args[:2] == ("remote", "add"):
            return real_git(cwd, "remote", "add", "origin", str(repo))
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_run_one_persists_events_to_bench_db(tmp_path):
    """Bench sandboxes must keep the run's event stream: supervisor/budget
    events flow through the orchestrator sink, and dropping them made the v9
    budget-class regression undrillable post hoc (fired-vs-ignored nudges were
    indistinguishable). run_one persists every event into the per-spec
    bench.db task_events table — stamped with ts + task_id like the product's
    _persisting path — while still forwarding to the caller's sink."""
    import json as _json
    import sqlite3

    from no_human.config import load_config

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    cfg = load_config(tmp_path / "config.yaml")
    forwarded: list[dict] = []
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(),
                             goal_judge=_StubGoalJudge(),
                             event_sink=forwarded.append)

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "awaiting_approval"
    db = sqlite3.connect(tmp_path / "wd" / "bench.db")
    rows = [_json.loads(r[0]) for r in
            db.execute("SELECT data FROM task_events").fetchall()]
    db.close()
    assert rows, "no events persisted to bench.db task_events"
    # Orchestrator lifecycle events made it in, stamped like the product path.
    assert any(e.get("source") == "orchestrator" for e in rows)
    assert all("ts" in e and "task_id" in e for e in rows)
    # The caller's sink still saw the stream (persistence is additive) —
    # every sink call both records and forwards, so the counts are EQUAL.
    assert forwarded, "caller event_sink no longer receives events"
    assert len(rows) == len(forwarded)


async def test_run_one_carries_an_event_digest_on_the_score(tmp_path):
    """bench.db dies with the sandbox cleanup, so completed specs were
    undrillable post hoc (v11 live: three 0/3-SAT early scores whose
    escalation REASONS were already deleted). The score itself must carry a
    capped digest of the event stream — it rides progress.json/latest.json
    and survives forever."""
    from no_human.config import load_config

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    cfg = load_config(tmp_path / "config.yaml")
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(),
                             goal_judge=_StubGoalJudge(),
                             event_sink=None)

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.events, "score carries no event digest"
    # Digest rows are compact: kind + truncated text, nothing unbounded.
    for e in score.events:
        assert set(e) <= {"kind", "text", "ts"}
        assert len(e.get("text") or "") <= 200
    assert any(e.get("kind") for e in score.events)
    # And it serializes — the freeze artifact is json.
    assert "events" in score.as_dict()


def test_event_digest_survives_the_card_round_trip(tmp_path):
    """r1 F1: --resume reloads progress.json via NorthStarCard.load, which
    rebuilt scores from an explicit keyword list and silently dropped the
    digest — every already-completed spec's events became [] in the final
    artifact, on exactly the crash-recovery path the digest exists for."""
    from no_human.eval.northstar import BenchScore
    from no_human.eval.northstar_card import NorthStarCard

    score = BenchScore(
        task_id="t1", title="x", outcome_status="escalated",
        goal_satisfied=False, escalated_honestly=True, mergeable=None,
        nh_tokens=1, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0,
        orig_tokens=2, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=2.0, orig_corrections=0,
        events=[{"kind": "advisory", "text": "answering pass failed",
                 "ts": 1.0}])
    card = NorthStarCard(label="t", scores=[score])
    path = tmp_path / "progress.json"
    card.save(path)
    loaded = NorthStarCard.load(path)
    assert loaded.scores[0].events == score.events


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


# ------------------------- kind classification ----------------------------- #

def test_bench_task_is_classified_like_the_product():
    """The product front door (`nh` → classify_kind) routes report-shaped work
    to its read-only pipelines; the bench must replay through the SAME routing
    or every review/question/plan spec grinds the feature pipeline (v6
    taxonomy, 2026-07-16: 4 "is my answer the deliverable?" parks + report-
    shaped 8M budget burns)."""
    from no_human.eval.northstar import _bench_task

    review = _bench_task(
        _spec(Path("."), title="review this PR https://forge.example/x/y/pull/42",
              request="review it as an experienced engineer"),
        Path("/tmp/work"))
    assert review.kind == "code_review"

    repo_review = _bench_task(
        _spec(Path("."), title="do an in-depth code review of the export module",
              request="review it as an experienced engineer"),
        Path("/tmp/work"))
    assert repo_review.kind == "investigation"

    question = _bench_task(
        _spec(Path("."), title="what are the allowed columns in the export endpoint",
              request="answer with citations"),
        Path("/tmp/work"))
    assert question.kind == "investigation"

    plain = _bench_task(
        _spec(Path("."), title="Add a dark-mode toggle to the settings page",
              request="implement it"),
        Path("/tmp/work"))
    assert plain.kind == "feature"
    assert plain.repo_path == "/tmp/work"
    assert plain.acceptance_criteria == []


@pytest.mark.asyncio
async def test_done_report_terminal_is_a_gate_state(tmp_path):
    """Review D9: report-deliverable pipelines (investigation / design_doc /
    clean code_review) terminate DONE with the report as the deliverable —
    the scorer must judge that report, not auto-fail the status."""
    from no_human.core.task import TaskStatus
    repo = _src_repo(tmp_path)
    runner = NorthStarRunner({}, backend_factory=lambda s: None)
    score = await runner._score(
        _spec(repo), _FakeOutcome(TaskStatus.DONE), tmp_path, "sha",
        attempts=[{"tokens_used": 100, "cache_read_tokens": 0, "turns_used": 2}],
        elapsed=2.0)
    assert "did not reach the human gate" not in (score.notes or "")
    assert score.goal_satisfied is not False  # judge decides (None here: no judge)


def test_sandbox_copy_is_instant_isolated_and_survives_a_dirty_source(tmp_path):
    """v8's two crashes were `git clone --no-hardlinks` COPYING a 3.8GB
    object store onto a starved disk. The fix is an APFS clonefile copy —
    instant at any size — and review of the hardlink alternative PROVED
    shared object inodes can be written through into the operator's live
    repo, so the property pinned here is ISOLATION: no object inode is
    shared. The source may also be DIRTY (v7's ns-4092c756 crashed checking
    out over one); the sandbox must come up clean at the pin regardless."""
    import subprocess
    from types import SimpleNamespace
    from no_human.eval.northstar import _setup_sandbox

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "a.txt").write_text("x" * 4096)
    for cmd in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                "commit", "-qm", "init"], ["gc", "-q"]):
        subprocess.run(["git", "-C", str(src), *cmd], check=True)
    # Dirty the source: a tracked-file edit AND an untracked file.
    (src / "a.txt").write_text("DIRTY")
    (src / "untracked.tmp").write_text("stray")

    spec = SimpleNamespace(repo={"path": str(src), "pin": "HEAD"})
    # Production pre-creates the workdir; without it cp -Rc fails on
    # the missing parent and the test silently pins the FALLBACK git
    # clone instead of the clonefile path (review F3).
    (tmp_path / "wd").mkdir(exist_ok=True)
    work = _setup_sandbox(spec, tmp_path / "wd")

    # Isolation: no shared object inodes with the source store.
    src_packs = {p.name: p.stat().st_ino
                 for p in (src / ".git" / "objects" / "pack").glob("*.pack")}
    assert src_packs, "fixture must have a pack"
    for p in (work / ".git" / "objects" / "pack").glob("*.pack"):
        assert p.stat().st_ino != src_packs.get(p.name), (
            f"{p.name} shares an inode with the source — the write-through "
            "corruption vector is back")
    # Clean at the pin despite the dirty source.
    assert (work / "a.txt").read_text() == "x" * 4096
    assert not (work / "untracked.tmp").exists()
    # And the source's dirt was untouched.
    assert (src / "a.txt").read_text() == "DIRTY"


def test_sandbox_copy_strips_source_hooks(tmp_path):
    """Clone parity: a file copy carries ACTIVE hooks (metrics-core-query-service
    ships a pre-push) that would execute foreign code on sandbox pushes —
    git clone never copies hooks."""
    import subprocess
    from types import SimpleNamespace
    from no_human.eval.northstar import _setup_sandbox

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    hook = src / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    # Production pre-creates the workdir; without it cp -Rc fails on
    # the missing parent and the test silently pins the FALLBACK git
    # clone instead of the clonefile path (review F3).
    (tmp_path / "wd").mkdir(exist_ok=True)
    work = _setup_sandbox(SimpleNamespace(repo={"path": str(src), "pin": "HEAD"}),
                          tmp_path / "wd")
    assert not (work / ".git" / "hooks" / "pre-push").exists(), (
        "the source's active pre-push rode into the sandbox — it would "
        "execute on every coder push")


def test_sandbox_strips_every_ride_along_remote(tmp_path):
    """Review F4: the copy carries ALL of the source's remotes while the
    push-proof guard resolves only origin — an upstream remote would be a
    guard-invisible escape. After setup, origin→local bare is the ONLY
    remote."""
    import subprocess
    from types import SimpleNamespace
    from no_human.eval.northstar import _setup_sandbox

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(src), "remote", "add", "origin",
                    "https://example.com/REAL.git"], check=True)
    subprocess.run(["git", "-C", str(src), "remote", "add", "upstream",
                    "https://example.com/REAL-UPSTREAM.git"], check=True)

    (tmp_path / "wd").mkdir(exist_ok=True)
    work = _setup_sandbox(SimpleNamespace(repo={"path": str(src), "pin": "HEAD"}),
                          tmp_path / "wd")
    remotes = subprocess.run(["git", "remote", "-v"], cwd=work,
                             capture_output=True, text=True).stdout
    assert "upstream" not in remotes and "example.com" not in remotes, remotes
    assert str(tmp_path / "wd" / "remote.git") in remotes


@pytest.mark.slow
@pytest.mark.asyncio
async def test_bench_grill_runs_in_the_sandbox_not_the_source(tmp_path, monkeypatch):
    """No-leak property for the §6 intake grill: when the bench replays a
    spec, the grill's repo evidence comes from the PINNED SANDBOX copy —
    never the operator's source repo (whose HEAD may already contain the
    historical solution)."""
    from no_human.config import load_config
    from no_human.intake import evaluator as ev

    seen = {}

    async def _fake_grill(title, description, criteria, repo_path, *,
                          backend=None, model=None, questions=None):
        seen["repo_path"] = str(repo_path)
        return None
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    cfg = load_config(tmp_path / "config.yaml")
    runner = NorthStarRunner(cfg.data, backend_factory=lambda s: _FixBackend(),
                             reviewer=_PassReviewer(),
                             goal_judge=_StubGoalJudge())

    score = await runner.run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "awaiting_approval"
    assert seen, "the bench pipeline never ran the intake grill"
    work = str((tmp_path / "wd" / "work").resolve())
    assert seen["repo_path"] in (work, str(tmp_path / "wd" / "work"))
    assert seen["repo_path"] != str(repo)


@pytest.mark.asyncio
async def test_judge_evidence_notes_keep_2000_chars(tmp_path):
    """#119 pin (review r1 finding): a silent revert to the old 400-char cap
    would re-amputate judge rationales (ns-7ef821b2 was cut mid-'BUT …') and
    only be noticed at the next drill. Feed 3000 chars, expect exactly 2000."""
    from no_human.core.task import TaskStatus
    from no_human.eval.judge import GoalVerdict

    class _LongEvidenceJudge:
        async def judge(self, **kwargs):
            return GoalVerdict(satisfied=False, evidence="E" * 3000)

    repo = _src_repo(tmp_path)
    spec = _spec(repo)
    runner = NorthStarRunner({}, backend_factory=lambda s: None,
                             goal_judge=_LongEvidenceJudge())

    score = await runner._score(
        spec, _FakeOutcome(TaskStatus.AWAITING_APPROVAL), repo, "HEAD", [], 1.0)
    assert len(score.notes) == 2000
