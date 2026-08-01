"""Tests for the Phase 1 human-action CLI verbs (nh approve / reject / diff / review / logs).

CLI commands call asyncio.run() internally, so tests must be synchronous.
Each helper opens its own fresh Store connection inside asyncio.run() so the
aiosqlite connection is never reused across event loops.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# Helpers — each opens a fresh Store connection in its own asyncio.run()      #
# --------------------------------------------------------------------------- #

def _seed_task(db_path: Path, status: TaskStatus, *, title="Test task") -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def _seed_attempt(db_path: Path, task_id: str, **fields) -> str:
    async def _go():
        async with Store(db_path) as s:
            aid = await s.create_attempt(task_id, 1)
            if fields:
                await s.update_attempt(aid, **fields)
            return aid
    return asyncio.run(_go())


def _get_task(db_path: Path, task_id: str) -> Task:
    async def _go():
        async with Store(db_path) as s:
            return await s.find_task(task_id)
    return asyncio.run(_go())


def _make_runner(path: Path, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = path  # assign after class def — class body can't see enclosing locals

    # Patch where the names are USED (commands.py has `from ..config import load_config`)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    return CliRunner()


# --------------------------------------------------------------------------- #
# nh approve                                                                   #
# --------------------------------------------------------------------------- #

def test_approve_awaiting_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_attempt(db, task_id, pr_url="https://example.com/pr/1")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "approved" in result.output.lower()
    assert "https://example.com/pr/1" in result.output

    refreshed = _get_task(db, task_id)
    assert refreshed.context.get("approved_at") is not None


def test_approve_completes_an_already_satisfied_task(tmp_path, monkeypatch):
    """PR #101 review HIGH: an already-satisfied claim has no PR — 'merge it
    in your git host' is a dead end. Approval IS the confirmation → DONE."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)

    async def _ctx():
        async with Store(db) as s:
            await s.merge_context(task_id, {"already_satisfied_report":
                "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1"})
    asyncio.run(_ctx())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "already satisfied" in result.output.lower()
    assert "merge the pr" not in result.output.lower()
    refreshed = _get_task(db, task_id)
    assert refreshed.status is TaskStatus.DONE
    assert refreshed.context.get("approved_at") is not None


def test_approve_wrong_status(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "not awaiting_approval" in output or "cannot approve" in output


def test_approve_unknown_id(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)  # ensure DB exists
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", "deadbeef"])

    assert result.exit_code != 0
    assert "no task" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh reject                                                                    #
# --------------------------------------------------------------------------- #

def test_reject_stores_feedback_and_resets(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs better tests"])

    assert result.exit_code == 0, result.output
    assert "sent back" in result.output.lower()

    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert any("better tests" in f["message"] for f in feedback)


def test_reject_unknown_id(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", "deadbeef", "--reason", "nope"])

    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# nh diff                                                                      #
# --------------------------------------------------------------------------- #

def test_diff_no_commit(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["diff", task_id[:8]])

    assert result.exit_code == 0
    assert "no commit" in result.output.lower()


def test_diff_git_failure_handled(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)

    # Override repo_path to a nonexistent dir after seeding
    async def _patch_repo():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.repo_path = str(tmp_path / "nonexistent_repo")
            await s.update_task(t)
    asyncio.run(_patch_repo())

    _seed_attempt(db, task_id, commit_sha="abc123def456")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["diff", task_id[:8]])

    # Must not crash — output contains a useful message
    assert result.exit_code == 0
    lower = result.output.lower()
    assert "abc123" in result.output or "git" in lower or "failed" in lower


# --------------------------------------------------------------------------- #
# nh review                                                                    #
# --------------------------------------------------------------------------- #

def test_review_shows_checklist(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    checklist = {
        "passed": True,
        "items": [
            {"label": "Tests pass", "passed": True, "evidence": "208 passed"},
            {"label": "No regressions", "passed": True, "evidence": "tamper guard clean"},
        ],
    }
    _seed_attempt(db, task_id, review_checklist=checklist, review_passed=1)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Tests pass" in result.output
    assert "208 passed" in result.output
    assert "PASSED" in result.output.upper()


def test_review_no_checklist(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0
    assert "no review" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh logs                                                                      #
# --------------------------------------------------------------------------- #

def test_logs_shows_attempts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Hard task")
    _seed_attempt(
        db, task_id,
        turns_used=42, tokens_used=15000,
        failure_reason="max_turns exceeded",
        status="failed",
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Hard task" in result.output
    assert "42" in result.output
    assert "max_turns" in result.output


def test_logs_no_attempts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.PENDING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0
    assert "no attempts" in result.output.lower()


def test_test_cmd_help():
    """nh test --help works without any bootstrap or auth."""
    runner = CliRunner()
    result = runner.invoke(cli, ["test", "--help"])
    assert result.exit_code == 0
    assert "fast" in result.output
    assert "full" in result.output
    assert "slow" in result.output
    assert "zero llm tokens" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh agents                                                                     #
# --------------------------------------------------------------------------- #

def test_agents_shows_active(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, title="doing work")
    _seed_attempt(db, task_id, turns_used=5, tokens_used=1234)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["agents"])

    assert result.exit_code == 0, result.output
    assert "doing work" in result.output
    assert "implementing" in result.output.lower()


def test_agents_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE, title="finished")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["agents"])

    assert result.exit_code == 0
    assert "no active" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh recall (B2): agentic-grep search over tasks/attempts/memories/history      #
# --------------------------------------------------------------------------- #

def _seed_memory(db_path: Path, *, mem_type="fact", content="") -> str:
    async def _go():
        async with Store(db_path) as s:
            return await s.add_memory(
                mem_type=mem_type, title=content[:40], content=content,
                confirmed=True,
            )
    return asyncio.run(_go())


def _seed_history_cache(db_path: Path, *, title="", findings="") -> None:
    async def _go():
        async with Store(db_path) as s:
            await s.history_cache_put(
                content_sig=f"sig-{title}", cascade_id="cascade-1",
                title=title, findings_json=findings,
            )
    asyncio.run(_go())


def test_recall_finds_matching_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE, title="Add cookie auth for the build server")
    _seed_task(db, TaskStatus.DONE, title="unrelated reporting dashboard work")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "jenkins"])

    assert result.exit_code == 0, result.output
    assert "jenkins cookie auth" in result.output.lower()
    assert "clickhouse" not in result.output.lower()


def test_recall_finds_matching_memory(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_memory(db, mem_type="anti_pattern",
                content="Never hardcode the build-server password in a script.")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "jenkins password"])

    assert result.exit_code == 0, result.output
    assert "memory" in result.output.lower()
    assert "anti_pattern" in result.output.lower()


def test_recall_finds_matching_history_cache(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_history_cache(db, title="Debugging the build-server auth 401 loop",
                        findings="{}")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "sso 401"])

    assert result.exit_code == 0, result.output
    assert "jenkins sso 401 loop" in result.output.lower()


def test_recall_shows_attempt_outcome_and_pr(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL, title="add mul() to calc")
    _seed_attempt(db, task_id, pr_url="https://example.com/pr/9", status="succeeded")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "mul"])

    assert result.exit_code == 0, result.output
    assert "https://example.com/pr/9" in result.output


def test_recall_no_matches(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE, title="totally different work")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "nonexistent-xyz-term"])

    assert result.exit_code == 0, result.output
    assert "no matches" in result.output.lower()


def test_recall_respects_limit(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    for i in range(5):
        _seed_task(db, TaskStatus.DONE, title=f"widget task number {i}")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "widget", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "3 more" in result.output.lower()


# --------------------------------------------------------------------------- #
# A CLI in-process run (`--run`) must also persist its events                  #
# --------------------------------------------------------------------------- #

def test_persisting_sink_records_stamps_and_forwards():
    from no_human.cli.commands import _persisting

    class FakePersister:
        def __init__(self):
            self.recorded = []

        def record(self, e):
            self.recorded.append(e)

    p = FakePersister()
    seen = []
    sink = _persisting(p, "task-abc", seen.append)

    sink({"kind": "tool_use", "tool_name": "Read"})
    # A subagent carries the SDK's own dispatch id — it must survive, or every
    # subagent collapses onto one node in the System view.
    sink({"kind": "subagent_start", "task_id": "sdk-dispatch-1"})

    assert len(p.recorded) == 2
    assert seen == p.recorded, "the console sink still sees every event"

    assert p.recorded[0]["task_id"] == "task-abc"
    assert p.recorded[0]["ts"] > 0
    assert p.recorded[1]["task_id"] == "sdk-dispatch-1"


def test_persisting_sink_does_not_overwrite_an_existing_ts():
    from no_human.cli.commands import _persisting

    class FakePersister:
        def __init__(self):
            self.recorded = []

        def record(self, e):
            self.recorded.append(e)

    p = FakePersister()
    sink = _persisting(p, "task-abc", lambda e: None)
    sink({"kind": "state", "ts": 123.0})
    assert p.recorded[0]["ts"] == 123.0


# --------------------------------------------------------------------------- #
# nh reply --choose  (D14: only a human applies a blocker option's action)     #
# --------------------------------------------------------------------------- #

def _seed_blocked_task(db_path: Path) -> str:
    from no_human.blockers import Blocker, BlockerCategory, BlockerOption

    async def _go():
        async with Store(db_path) as s:
            t = Task.new("scope explosion", repo_path="/tmp/repo")
            await s.create_task(t)
            t.blocker = Blocker(
                category=BlockerCategory.SCOPE_EXPLOSION,
                confidence=0.9,
                question="This change exceeds the safety size limits.",
                options=[
                    BlockerOption(label="split into smaller tasks"),
                    BlockerOption(
                        label="raise the limit for this task",
                        action={"set_task_config": {"max_lines_changed": 700}},
                    ),
                ],
                resume_branch="scratch/x/abc-2",
                resume_commit="75c68e08",
            ).to_dict()
            await s.update_task(t)
            await s.set_status(t, TaskStatus.ESCALATED, validate=False)
            return t.id
    return asyncio.run(_go())


def test_reply_choose_applies_the_options_action(tmp_path, monkeypatch):
    """'raise the limit for this task' has to actually raise the limit — before
    D14 the same blocker was regenerated on the next attempt."""
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "--choose", "2", "--no-run"])

    assert result.exit_code == 0, result.output
    assert "max_lines_changed=700" in result.output

    t = _get_task(db, task_id)
    assert t.config["max_lines_changed"] == 700
    assert t.status is TaskStatus.IMPLEMENTING
    reply = t.context["human_replies"][-1]
    assert reply["answer"] == "raise the limit for this task"
    assert reply["applied"] == "max_lines_changed=700"
    # And it resumes from the checkpoint rather than from base (D15).
    assert t.context["resume_from"]["sha"] == "75c68e08"


def test_reply_choose_without_an_action_is_plain_free_text(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "--choose", "1", "--no-run"])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.config == {}  # nothing applied
    assert t.context["human_replies"][-1]["answer"] == "split into smaller tasks"


def test_reply_rejects_an_out_of_range_choice(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "--choose", "9", "--no-run"])

    assert result.exit_code != 0
    assert "between 1 and 2" in result.output
    assert _get_task(db, task_id).config == {}


def test_reply_needs_exactly_one_of_answer_or_choose(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    both = runner.invoke(cli, ["reply", task_id[:8], "an answer", "--choose", "1"])
    neither = runner.invoke(cli, ["reply", task_id[:8]])

    assert both.exit_code != 0 and "not both" in both.output
    assert neither.exit_code != 0 and "not both" in neither.output


# --------------------------------------------------------------------------- #
# nh status --json                                                             #
# --------------------------------------------------------------------------- #

def test_status_json_bucket_counts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_task(db, TaskStatus.IMPLEMENTING)
    _seed_task(db, TaskStatus.PAUSED_QUOTA)
    _seed_task(db, TaskStatus.FAILED)
    _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "needs you": 2, "working": 1, "waiting": 1, "failed": 1, "done": 1,
    }


def test_status_json_exit_zero(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output


def test_status_default_unchanged(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_task(db, TaskStatus.IMPLEMENTING)
    _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "needs you" in result.output
    assert "working" in result.output
    assert "done" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_status_json_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "needs you": 0, "working": 0, "waiting": 0, "failed": 0, "done": 0,
    }


def test_approve_with_a_stale_claim_and_a_real_pr_does_not_auto_done(tmp_path, monkeypatch):
    """PR #101 round-2 MEDIUM: after a claim is sent back and a later attempt
    ships a REAL PR, the stale already_satisfied_report must not hijack the
    approval into a false DONE — the human still has a PR to merge."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_attempt(db, task_id, pr_url="https://github.com/o/r/pull/7")

    async def _ctx():
        async with Store(db) as s:
            await s.merge_context(task_id, {"already_satisfied_report":
                "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1"})
    asyncio.run(_ctx())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "merge the pr" in result.output.lower()
    assert "https://github.com/o/r/pull/7" in result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL


# ------------------------- interactive grill (B2) -------------------------- #

@pytest.mark.asyncio
async def test_cli_grill_writes_shared_qa_surface(tmp_path, monkeypatch):
    """#121 reviewer gap: the GrillResult path had no test. The human-answered
    Q&A must land on the SAME audit surface the unattended grill uses
    (context['intake_qa'], source=human) and stamp grill_complete so the
    orchestrator's auto-grill never re-asks."""
    from no_human.cli import commands as cmds
    from no_human.config import load_config
    from no_human.intake.grill import GrillQuestion, GrillResult

    steps = [
        GrillQuestion(round=1, question="Which repo?", suggestions=["A", "B"]),
        GrillResult(title="refined title", description="refined desc",
                    acceptance_criteria=["AC1"]),
    ]

    async def fake_grill_step(*a, **k):
        return steps.pop(0)

    monkeypatch.setattr(cmds, "ClaudeBackend", lambda **k: object())
    monkeypatch.setattr("no_human.intake.grill.grill_step", fake_grill_step)
    monkeypatch.setattr("click.prompt", lambda *a, **k: "repo A please")

    cfg = load_config(tmp_path / "config.yaml")
    t = Task.new("raw title", repo_path="/r")
    out = await cmds._run_cli_grill(cfg, t)

    assert out.title == "refined title"
    assert out.acceptance_criteria == ["AC1"]
    ctx = out.context or {}
    assert ctx["grill_complete"] is True
    qa = ctx["intake_qa"]
    assert len(qa) == 1
    assert qa[0]["question"] == "Which repo?"
    assert qa[0]["answer"] == "repo A please"
    assert qa[0]["source"] == "human"


def test_logs_reports_SPEND_not_just_non_cache_tokens(tmp_path, monkeypatch):
    """`nh logs` under-reported a live runaway by ~5500x.

    `attempts.tokens_used` holds NON-CACHE tokens only, while the budget guard
    enforces `tokens_used + cache_read_tokens`. Cache reads dominate real burn,
    so an attempt aborted at 4,054,229 displayed as `tokens=731` — the one
    attempt that needed attention looked like the cheapest thing that ever ran.

    NOTE ON THIS TEST'S OWN HISTORY: the first version asserted the bare string
    "4,054,229", which the seeded `failure_reason` echoed back on the next
    line — so restoring the bug (`_plain + _read` -> `_plain`) left it green.
    It now asserts the RENDERED FIELD, `spend=...`, and the seeded reason
    deliberately contains no digits that could satisfy it.

    Numbers are the real ones from task fa7be197.
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Runaway")
    _seed_attempt(
        db, task_id, tokens_used=731,          # turns NULL, as on a real abort
        cache_read_tokens=4_053_498, cache_creation_tokens=197_948,
        plan_tokens_used=5_046, plan_cache_read_tokens=334_396,
        plan_cache_creation_tokens=45_165,
        utility_tokens_used=2_395, utility_cache_read_tokens=136_632,
        utility_cache_creation_tokens=217_009,
        status="failed",
        failure_reason="budget-abort: crossed the per-attempt cap",
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])
    out = result.output.replace("\n", "")     # the line wraps at 80 cols

    assert result.exit_code == 0, result.output
    # THE assertion: the guard's own number, as a rendered field.
    assert "spend=4,054,229" in out, result.output
    # Cache CREATION is billed at the fresh rate and the cap ignores it, so a
    # spend-only line hides ~33% of the dollar cost. burn must show it.
    # burn is the ATTEMPT's total, not the coder session's. The plan and
    # utility sessions on this row add 740,643 tokens — 15% of the tokens and
    # 34% of the dollars. Presenting the coder's number as the total is the
    # same "partial number shown as a total" defect the coder-only display
    # had, one tier up; cost.js's header records this repo shipping it twice.
    assert "burn=4,992,820" in out, result.output
    # Components stay visible so "why" is answerable.
    assert "non-cache 731" in out and "cache-read 4,053,498" in out
    assert "cache-creation 197,948" in out
    # A NULL turns column must not print the literal "None".
    assert "turns=None" not in out, result.output


def test_logs_says_UNKNOWN_rather_than_zero_when_tokens_are_null(
        tmp_path, monkeypatch):
    """13 of 127 live attempt rows have a NULL `tokens_used`.

    The previous version of this test claimed the CACHE columns could be NULL;
    they cannot — `db.py` declares them `INTEGER DEFAULT 0`, and 0 of 127 live
    rows are NULL. So it guarded a branch that never runs. `tokens_used` NULL
    is the real case, and without it the total is genuinely unknown: printing
    "0" would be a claim, the same kind of untrue display as `turns=None`.
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Unknown spend")
    _seed_attempt(db, task_id, turns_used=3, cache_read_tokens=900,
                  status="failed")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])
    out = result.output.replace("\n", "")

    assert result.exit_code == 0, result.output
    assert "spend=?" in out, result.output
    assert "burn=?" in out, result.output
    assert "spend=0" not in out, "0 is a claim; the value is unknown"
    # The component that IS known is still reported.
    assert "cache-read 900" in out


def test_agents_table_shows_BURN_not_non_cache_coder_tokens(tmp_path, monkeypatch):
    """The Agent Sessions table is what an operator watches a runaway on, so
    it was the worst place to print the smallest number: it carried the same
    5500x under-report `nh logs` did (`tokens_used` is NON-CACHE CODER tokens).
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, title="Runaway")
    _seed_attempt(
        db, task_id, tokens_used=731,
        cache_read_tokens=4_053_498, cache_creation_tokens=197_948,
        plan_tokens_used=5_046, plan_cache_read_tokens=334_396,
        plan_cache_creation_tokens=45_165,
        utility_tokens_used=2_395, utility_cache_read_tokens=136_632,
        utility_cache_creation_tokens=217_009,
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["agents"])
    out = result.output.replace("\n", "").replace(" ", "")

    assert result.exit_code == 0, result.output
    assert "4,992,820" in out, result.output
    # The old value must be gone, not merely joined by the new one.
    assert "731" not in out.replace("4,992,820", ""), result.output


def test_burn_includes_the_REVIEWER_session(tmp_path, monkeypatch):
    """The reviewer's tokens are part of the attempt's burn, and nothing saw
    them.

    The other fixtures model attempt #1 of task fa7be197, which ABORTED before
    review — so all six `review_*` columns are legitimately 0 there, and
    dropping the whole review group from `_TOKEN_GROUPS` survived the entire
    suite. This models attempt #2 of the SAME task, which completed and did
    run a reviewer; every number below is that row verbatim.

    Live impact of the blind spot: 4 attempts in the operator's DB carry review
    burn, up to 300,236 tokens.
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL, title="Reviewed")
    _seed_attempt(
        db, task_id, turns_used=39,
        tokens_used=12_665, cache_read_tokens=2_146_223,
        cache_creation_tokens=49_005,
        review_tokens_used=2_701, review_cache_read_tokens=59_624,
        review_cache_creation_tokens=31_159,
        utility_tokens_used=474, utility_cache_read_tokens=141_771,
        utility_cache_creation_tokens=149_727,
        status="succeeded",
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])
    out = result.output.replace("\n", "")

    assert result.exit_code == 0, result.output
    # spend is the CODER session only — what the cap enforces.
    assert "spend=2,158,888" in out, result.output
    # burn is the whole attempt. Drop the review group and this is 2,499,865.
    assert "burn=2,593,349" in out, result.output
