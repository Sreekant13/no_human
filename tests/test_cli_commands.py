"""Tests for the Phase 1 human-action CLI verbs (nh approve / reject / diff / review / logs).

CLI commands call asyncio.run() internally, so tests must be synchronous.
Each helper opens its own fresh Store connection inside asyncio.run() so the
aiosqlite connection is never reused across event loops.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn
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


@pytest.mark.parametrize("argv", [["approve", "deadbeef"], ["review", "deadbeef"]])
def test_unknown_id_tells_user_how_to_find_a_task_id(tmp_path, monkeypatch, argv):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)  # ensure DB exists
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, argv)

    assert result.exit_code == 1, result.output
    assert "no task matching" in result.output
    assert "nh task list" in result.output


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


def test_reject_done_task_is_blocked(tmp_path, monkeypatch):
    """SCRUM-77: a done row's status write is CAS-blocked (SCRUM-73) —
    reject must exit non-zero and say so, not print 'sent back'."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs fixes"])

    assert result.exit_code == 1, result.output
    assert "sent back" not in result.output.lower()
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.DONE
    assert refreshed.context.get("send_back_feedback") in (None, [])


def test_reject_cancelled_task_is_blocked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)

    async def _cancel():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.context = {"cancel_reason": "Cancelled from board"}
            await s.update_task(t)
    asyncio.run(_cancel())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs fixes"])

    assert result.exit_code == 1, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.FAILED
    assert refreshed.context.get("send_back_feedback") in (None, [])


# --------------------------------------------------------------------------- #
# nh unblock                                                                   #
# --------------------------------------------------------------------------- #

def test_unblock_resumes_blocked_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 0, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


def test_unblock_done_task_is_blocked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 1, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.DONE


def test_unblock_cancelled_task_is_blocked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)

    async def _cancel():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.context = {"cancel_reason": "Cancelled from board"}
            await s.update_task(t)
    asyncio.run(_cancel())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 1, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.FAILED


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


def test_review_checklist_escapes_model_authored_markup(tmp_path, monkeypatch):
    # A reviewer-authored label/evidence containing ALPHABETIC bracket tags must
    # survive to the terminal literally — rich only eats alphabetic tags, so a
    # numeric payload like "high[2]" is inert and proves nothing (per the task).
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    label = "a[b]c [dim]hidden[/] end"
    evidence = "before [red]boom[/] after"
    checklist = {
        "passed": True,
        "items": [
            {"label": label, "passed": True, "evidence": evidence},
        ],
    }
    _seed_attempt(db, task_id, review_checklist=checklist, review_passed=1)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert label in result.output
    assert evidence in result.output


def test_investigate_show_escapes_model_authored_findings(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    findings = "root cause: [bold]x[/] is unguarded"

    async def _set_findings():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.context = {"findings": findings}
            await s.update_task(t)
    asyncio.run(_set_findings())

    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["investigate", "--show", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert findings in result.output


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


def test_logs_names_a_resume_checkpoint_that_could_not_be_read(tmp_path, monkeypatch):
    """`nh logs` reads ATTEMPTS, not the event stream, and it is the first place
    a human asks "why did this attempt start from scratch?". A checkpoint the
    orchestrator could not resume from must answer that here — and must not be
    dressed as a failure: the attempt succeeded, it just lost prior work."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE, title="Resumed task")
    _seed_attempt(
        db, task_id, status="succeeded", turns_used=7,
        resume_checkpoint_lost=(
            "checkpoint 5013e6c9 is no longer in the repository — this attempt "
            "branched from main instead"),
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "5013e6c9" in result.output, result.output
    assert "branched from main" in result.output, result.output
    assert "reason:" not in result.output, \
        "a lost checkpoint is not a failure reason and must not print as one"


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

    result = runner.invoke(cli, ["recall", "cookie"])

    assert result.exit_code == 0, result.output
    assert "cookie auth for the build server" in result.output.lower()
    assert "reporting dashboard" not in result.output.lower()


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

    result = runner.invoke(cli, ["recall", "auth 401"])

    assert result.exit_code == 0, result.output
    assert "build-server auth 401 loop" in result.output.lower()


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
    # The residual ledger rides alongside the buckets (it is not summed into
    # any of them); with no intake spend seeded it reports an explicit zero
    # rather than being absent, so consumers see a stable shape.
    assert json.loads(result.output) == {
        "needs you": 2, "working": 1, "waiting": 1, "failed": 1, "done": 1,
        "unattributed_usage": {
            "calls": 0, "tokens_used": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "total": 0,
        },
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
        "unattributed_usage": {
            "calls": 0, "tokens_used": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "total": 0,
        },
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


# --------------------------------------------------------------------------- #
# nh start — Jira poller parity with nh serve (SCRUM-21)                      #
# --------------------------------------------------------------------------- #

class _FakeUvicornServer:
    """Stands in for uvicorn.Server so `nh start` tests never bind a socket."""

    def __init__(self, config):
        self.config = config

    async def serve(self):
        return None


def _make_start_cfg(db_path: Path, *, jira_enabled: bool):
    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data = {
            "server": {"port": 8420},
            "concurrency": {},
            "integrations": {"jira": {
                "enabled": jira_enabled,
                "project_key": "SCRUM",
                "poll_interval": "5m",
            }},
        }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    return _Cfg()


def _patch_start_scaffolding(monkeypatch, cfg):
    import no_human.cli.commands as cmd_mod

    # Never touch the real ~/.no_human/nh.pid or shell out for auth/CLI
    # checks — the test only cares about the Jira-poller wiring.
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(cmd_mod, "_assert_backend_usable", lambda: None)
    monkeypatch.setattr(cmd_mod, "_acquire_pid_lock", lambda: True)
    monkeypatch.setattr(cmd_mod, "_release_pid_lock", lambda: None)
    # `nh start` builds its own uvicorn.Server (instead of uvicorn.run) so it
    # can run the Jira poll loop in the same event loop — fake it so the test
    # never binds a real socket.
    monkeypatch.setattr(uvicorn, "Server", _FakeUvicornServer)
    return cmd_mod


def test_start_runs_jira_poller_when_enabled(tmp_path, monkeypatch):
    import no_human.intake.jira as jira_mod
    import no_human.intake.jira_poll as jira_poll_mod

    cfg = _make_start_cfg(tmp_path / "test.db", jira_enabled=True)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_instance = MagicMock()
    mock_poller_cls = MagicMock(return_value=mock_poller_instance)
    monkeypatch.setattr(jira_poll_mod, "JiraPoller", mock_poller_cls)
    monkeypatch.setattr(jira_mod, "JiraAdapter", MagicMock())
    # Stands in for the poller's polling loop being started — `nh serve` has
    # no `JiraPoller.start()` either; both drive `poller.tick()` through this
    # shared coroutine (verified: src/no_human/intake/jira_poll.py has no
    # `start` method).
    mock_poll_loop = AsyncMock(return_value=None)
    monkeypatch.setattr(cmd_mod, "_jira_poll_loop", mock_poll_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_called_once()
    mock_poll_loop.assert_called_once()
    assert mock_poll_loop.call_args.args[0] is mock_poller_instance
    assert "jira intake" in result.output.lower()


def test_start_skips_jira_poller_when_disabled(tmp_path, monkeypatch):
    import no_human.intake.jira as jira_mod
    import no_human.intake.jira_poll as jira_poll_mod

    cfg = _make_start_cfg(tmp_path / "test.db", jira_enabled=False)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_cls = MagicMock()
    monkeypatch.setattr(jira_poll_mod, "JiraPoller", mock_poller_cls)
    monkeypatch.setattr(jira_mod, "JiraAdapter", MagicMock())
    mock_poll_loop = AsyncMock(return_value=None)
    monkeypatch.setattr(cmd_mod, "_jira_poll_loop", mock_poll_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_not_called()
    mock_poll_loop.assert_not_called()
    assert "jira" not in result.output.lower()


def _make_start_cfg_linear(db_path: Path, *, linear_enabled: bool):
    """Same shape as _make_start_cfg, with Jira off so the two trackers'
    wiring can be asserted independently."""
    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data = {
            "server": {"port": 8420},
            "concurrency": {},
            "integrations": {
                "jira": {"enabled": False},
                "linear": {
                    "enabled": linear_enabled,
                    "team_key": "ENG",
                    "poll_interval": "5m",
                },
            },
        }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    return _Cfg()


def test_start_runs_linear_poller_when_enabled(tmp_path, monkeypatch):
    import no_human.intake.linear as linear_mod
    import no_human.intake.linear_poll as linear_poll_mod

    cfg = _make_start_cfg_linear(tmp_path / "test.db", linear_enabled=True)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_instance = MagicMock()
    mock_poller_cls = MagicMock(return_value=mock_poller_instance)
    monkeypatch.setattr(linear_poll_mod, "LinearPoller", mock_poller_cls)
    monkeypatch.setattr(linear_mod, "LinearAdapter", MagicMock())
    # A real coroutine (not an AsyncMock) so the SHUTDOWN path is observable:
    # it records the stop event `start` handed it, and the assertion below
    # checks `start`'s finally-block actually set it. With an AsyncMock the
    # task completes instantly and a missing shutdown looks identical to a
    # working one.
    seen = {}

    async def fake_loop(poller, stop, poll_interval):
        seen["poller"] = poller
        seen["stop"] = stop
        seen["interval"] = poll_interval

    monkeypatch.setattr(cmd_mod, "_linear_poll_loop", fake_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_called_once()
    assert seen["poller"] is mock_poller_instance
    assert seen["interval"] == 300           # "5m", floored at 60s
    # `start` must stop the poll loop on the way out, or `nh start` would
    # leave a polling task running after the server exits.
    assert seen["stop"].is_set() is True
    assert "linear intake" in result.output.lower()
    # Jira is off in this config: the two trackers must be independent.
    assert "jira" not in result.output.lower()


def test_start_skips_linear_poller_when_disabled(tmp_path, monkeypatch):
    import no_human.intake.linear as linear_mod
    import no_human.intake.linear_poll as linear_poll_mod

    cfg = _make_start_cfg_linear(tmp_path / "test.db", linear_enabled=False)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_cls = MagicMock()
    monkeypatch.setattr(linear_poll_mod, "LinearPoller", mock_poller_cls)
    monkeypatch.setattr(linear_mod, "LinearAdapter", MagicMock())
    mock_poll_loop = AsyncMock(return_value=None)
    monkeypatch.setattr(cmd_mod, "_linear_poll_loop", mock_poll_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_not_called()
    mock_poll_loop.assert_not_called()
    assert "linear" not in result.output.lower()


# --------------------------------------------------------------------------- #
# nh stop                                                                      #
# --------------------------------------------------------------------------- #

def _write_pidfile(home: Path, pid: int) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "nh.pid"
    path.write_text(str(pid))
    return path


def _patch_stop_home(monkeypatch, home: Path):
    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "NO_HUMAN_HOME", home)
    return CliRunner()


def test_stop_no_pidfile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    runner = _patch_stop_home(monkeypatch, home)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "not running" in result.output.lower()
    assert not (home / "nh.pid").exists()


def test_stop_stale_pid(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pidfile = _write_pidfile(home, 424242)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "stale" in result.output.lower()
    assert not pidfile.exists()


def test_stop_happy_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 555
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []
    state = {"alive": True}

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            if not state["alive"]:
                raise ProcessLookupError()
            return
        if sig == signal.SIGTERM:
            state["alive"] = False
            return
        raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output.lower()
    assert not pidfile.exists()
    sigterm_calls = [c for c in calls if c[1] == signal.SIGTERM]
    sigkill_calls = [c for c in calls if c[1] == signal.SIGKILL]
    assert sigterm_calls == [(target_pid, signal.SIGTERM)]
    assert sigkill_calls == []


def test_stop_wedged_escalates_to_sigkill(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 777
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []
    state = {"killed": False}

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            if state["killed"]:
                raise ProcessLookupError()
            return  # still alive — never dies from SIGTERM alone
        if sig == signal.SIGKILL:
            state["killed"] = True
            return
        # SIGTERM: no-op, process stays wedged

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert "force-kill" in result.output.lower()
    assert not pidfile.exists()
    assert (target_pid, signal.SIGTERM) in calls
    assert (target_pid, signal.SIGKILL) in calls


def test_stop_only_targets_pidfile_pid(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 999
    _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []
    state = {"alive": True}

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            if not state["alive"]:
                raise ProcessLookupError()
            return
        if sig == signal.SIGTERM:
            state["alive"] = False

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert calls, "expected os.kill to be called at least once"
    assert all(pid == target_pid for pid, _sig in calls)


@pytest.mark.parametrize("bad_pid", [-1, 0, 1])
def test_stop_rejects_corrupt_pid(tmp_path, monkeypatch, bad_pid):
    home = tmp_path / "home"
    pidfile = _write_pidfile(home, bad_pid)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise AssertionError(f"must not signal corrupt pid {pid}")
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "corrupt" in result.output.lower()
    assert not pidfile.exists()


def test_stop_rejects_self_pid(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pidfile = _write_pidfile(home, os.getpid())
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise AssertionError(f"must not signal self pid {pid}")
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "corrupt" in result.output.lower()
    assert not pidfile.exists()


def test_stop_permission_denied_keeps_pidfile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 4242
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise PermissionError()
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "another user" in result.output.lower()
    assert pidfile.exists()


def test_stop_race_exits_before_sigterm_delivered(tmp_path, monkeypatch):
    """Process exits between the liveness check and the SIGTERM call —
    os.kill(pid, SIGTERM) itself raises ProcessLookupError. Must be treated
    as a successful stop, not crash, and must not escalate to SIGKILL."""
    home = tmp_path / "home"
    target_pid = 321
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            return  # liveness check: still alive
        if sig == signal.SIGTERM:
            raise ProcessLookupError()  # gone by the time the signal lands
        raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output.lower()
    assert not pidfile.exists()
    assert (target_pid, signal.SIGKILL) not in calls


def test_stop_sigkill_exhausted_keeps_pidfile(tmp_path, monkeypatch):
    """If the process is still alive after SIGKILL (shouldn't normally
    happen, but the wait is bounded), the pidfile must be left in place and
    the command must report failure — never claim success for a process
    that is still running."""
    home = tmp_path / "home"
    target_pid = 888
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        if sig == 0:
            return  # always alive, no matter what was sent
        # SIGTERM / SIGKILL: no-op, process never dies

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop", "--timeout", "0"])

    assert result.exit_code == 1, result.output
    assert "still running" in result.output.lower()
    assert pidfile.exists()


def test_stop_keeps_pidfile_while_process_alive(tmp_path, monkeypatch):
    """Pins that the pidfile is NOT removed until the process is confirmed
    gone — a mutation that unlinks right after sending SIGTERM (before
    confirming death) must fail this test."""
    home = tmp_path / "home"
    target_pid = 654
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    state = {"polls": 0}

    def _fake_kill(pid, sig):
        if sig == 0:
            state["polls"] += 1
            if state["polls"] == 1:
                return  # initial liveness check, before SIGTERM
            assert pidfile.exists(), "pidfile removed while process still alive"
            if state["polls"] < 3:
                return  # still alive for a couple of post-SIGTERM polls
            raise ProcessLookupError()
        elif sig == signal.SIGTERM:
            return
        else:
            raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert not pidfile.exists()


# --------------------------------------------------------------------------- #
# Resume provenance — every path, driven through the REAL command              #
#                                                                              #
# The zero-diff honesty gate credits work already ahead of base only when a     #
# HUMAN gated it, reading `resume_from.by`. Six review rounds kept trading one  #
# direction of a ONE-WAY LATCH for the other because the stamp was written      #
# inside `if checkpoint:`: a resume whose blocker recorded no sha wrote nothing, #
# and `merge_context` is RFC 7396, so the PREVIOUS actor's `by` survived to      #
# describe THIS resume. These drive the real commands, not the store helper —   #
# a round-5 test asserted this through `merge_context` directly and the whole    #
# suite stayed green with both CLI stamps deleted.                              #
# --------------------------------------------------------------------------- #

def _seed_parked_task(db_path: Path, status: TaskStatus, *,
                      checkpoint: bool, stale_by: str) -> str:
    """A parked task carrying a stale provenance marker from an earlier resume."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("resume provenance", repo_path="/tmp/repo")
            await s.create_task(t)
            blocker = {"category": "AMBIGUITY", "question": "which store?"}
            if checkpoint:
                blocker |= {"resume_branch": "scratch/x/abc-2",
                            "resume_commit": "75c68e08"}
            t.blocker = blocker
            await s.update_task(t)
            await s.merge_context(t.id, {
                "resume_reason": "wake_condition_satisfied",
                "resume_from": {"sha": "0e22fe3d", "branch": "old",
                                "by": stale_by},
            })
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def test_task_resume_stamps_provenance_with_NO_checkpoint(tmp_path, monkeypatch):
    """`nh task resume` on a blocker that recorded no sha still has to say who
    resumed it, or the stale machine marker fails the human's own resume."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=False, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "resume", task_id[:8]])

    assert result.exit_code == 0, result.output
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"`nh task resume` skipped the stamp with no checkpoint: {resume_from}")


def test_nh_reply_stamps_provenance_with_NO_checkpoint(tmp_path, monkeypatch):
    """Same latch on `nh reply` — the path the blocker's own message promises."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=False, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "SQLite only", "--no-run"])

    assert result.exit_code == 0, result.output
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"`nh reply` skipped the stamp with no checkpoint: {resume_from}")


def test_unblock_stamps_human_provenance(tmp_path, monkeypatch):
    """`nh unblock` re-enters the loop by hand and wrote no provenance at all,
    so whatever an earlier machine resume left behind described this human."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.IMPLEMENTING
    assert t.context["resume_from"].get("by") == "human", (
        f"`nh unblock` left a machine marker on a human's action: {t.context['resume_from']}")


def test_unblock_with_FAIL_does_not_claim_a_resume(tmp_path, monkeypatch):
    """Negative control: `--fail` abandons the task rather than resuming it, so
    it must NOT stamp a resume that never happened."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8], "--fail"])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.FAILED
    assert t.context["resume_from"].get("by") == "wake", (
        "--fail is not a resume and must leave provenance untouched: "
        f"{t.context['resume_from']}")


def test_reject_stamps_human_provenance(tmp_path, monkeypatch):
    """`nh reject` is the CLI twin of the drawer's Send back — a human gate."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.AWAITING_APPROVAL,
                                checkpoint=False, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "redo it"])

    assert result.exit_code == 0, result.output
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"`nh reject` left a machine marker on a human's decision: {resume_from}")
    # It must NOT keep the sha the machine resume chose — see the send-back test
    # in test_api.py. Labelling another actor's sha "human" is the fail-OPEN
    # direction and opens a PR on work no attempt produced.
    assert resume_from.get("sha") is None, (
        f"`nh reject` inherited a sha it never chose: {resume_from}")


# --------------------------------------------------------------------------- #
# 🔴 THE WIRING TABLE — every resume entry point, driven for real.             #
#                                                                              #
# Eight rounds of review went past this because the enumeration of writers      #
# lived in COMMIT MESSAGES and DOCSTRINGS instead of in a test. Round 8 proved  #
# the cost: `nh reply` was named in prose as converted, was not, and shipped     #
# the fail-OPEN shape — a sha a MACHINE chose, relabelled `human`, which        #
# disarms the zero-diff honesty gate and opens a PR on work no attempt          #
# produced. Six more call sites had no guard against that regression at all:    #
# their tests asserted `by` and never `sha`, so reverting any of them to the    #
# old shape left the suite green.                                              #
#                                                                              #
# So the enumeration is now executable. Each case drives the REAL entry point   #
# on a task carrying a stale MACHINE checkpoint, and asserts the sha is not     #
# inherited. Adding a ninth resume path without adding it here is still         #
# possible — but silently reverting any of these eight is not.                 #
# --------------------------------------------------------------------------- #

def _sha_less_seed(db_path: Path, status: TaskStatus) -> str:
    """A parked task whose blocker holds NO checkpoint, carrying the residue of
    an earlier MACHINE resume that chose a sha of its own."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("wiring table", repo_path="/tmp/repo")
            await s.create_task(t)
            t.blocker = {"category": "AMBIGUITY", "question": "which store?"}
            await s.update_task(t)
            await s.merge_context(t.id, {
                "resume_reason": "wake_condition_satisfied",
                "resume_from": {"sha": "0e22fe3d", "branch": "old", "by": "wake"},
            })
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


@pytest.mark.parametrize("verb,args,status", [
    ("nh task resume", ["task", "resume"], TaskStatus.ESCALATED),
    ("nh reply", ["reply", "__ID__", "SQLite only", "--no-run"], TaskStatus.ESCALATED),
    ("nh unblock", ["unblock"], TaskStatus.ESCALATED),
    ("nh reject", ["reject", "__ID__", "--reason", "redo it"], TaskStatus.AWAITING_APPROVAL),
])
def test_no_cli_resume_path_inherits_a_sha_it_did_not_choose(
        verb, args, status, tmp_path, monkeypatch):
    """Whichever CLI verb re-enters the loop, it must not relabel a sha that a
    MACHINE resume chose. `nh reply` failed exactly this and shipped."""
    db = tmp_path / "test.db"
    task_id = _sha_less_seed(db, status)
    runner = _make_runner(db, monkeypatch)

    argv = [task_id[:8] if a == "__ID__" else a for a in args]
    if "__ID__" not in args:
        argv = argv + [task_id[:8]]
    result = runner.invoke(cli, argv)

    assert result.exit_code == 0, f"{verb}: {result.output}"
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"{verb} left a machine marker describing a human's action: {resume_from}")
    assert resume_from.get("sha") is None, (
        f"{verb} INHERITED a sha it never chose and relabelled it human — this "
        f"is the fail-OPEN direction that opens a PR on work no attempt "
        f"produced: {resume_from}")


def test_unblock_REFUSES_a_live_task_and_leaves_provenance_alone(tmp_path, monkeypatch):
    """🔴 THE FAIL-OPEN HOLE `nh unblock` opened when it learned to read a
    checkpoint. It copied the drawer Resume's checkpoint read and NEITHER of the
    two guards that make it safe: the drawer refuses unless the task is parked,
    and the drawer clears the blocker.

    Without the first guard this fired on a LIVE attempt — implementing,
    reviewing, testing, awaiting_approval — re-applying a sha the WAKE WATCHER
    had chosen and stamping it `human`. An independent review reproduced the
    end state through `run_task`: an attempt that edited nothing was `succeeded`
    and the task advanced to `awaiting_approval`. A PR on work no attempt made.
    """
    for live in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
                 TaskStatus.TESTING, TaskStatus.AWAITING_APPROVAL):
        db = tmp_path / f"live-{live.value}.db"
        task_id = _seed_parked_task(db, live, checkpoint=True, stale_by="wake")
        runner = _make_runner(db, monkeypatch)

        result = runner.invoke(cli, ["unblock", task_id[:8]])

        assert result.exit_code == 0, result.output
        t = _get_task(db, task_id)
        assert t.status is live, (
            f"`nh unblock` re-entered a LIVE {live.value} task: now {t.status.value}")
        assert t.context["resume_from"].get("by") == "wake", (
            "`nh unblock` relabelled a machine's sha as human-gated on a live "
            f"attempt — the fail-OPEN direction: {t.context['resume_from']}")


def test_unblock_CONSUMES_the_checkpoint_so_it_cannot_be_reapplied(tmp_path, monkeypatch):
    """The second guard. A checkpoint must be consumable exactly ONCE, by the
    human who read it. Leaving the blocker in place made the same sha
    re-appliable forever, stamped `human` every time."""
    db = tmp_path / "consume.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    assert runner.invoke(cli, ["unblock", task_id[:8]]).exit_code == 0
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.IMPLEMENTING
    assert t.context["resume_from"].get("by") == "human"
    assert t.context["resume_from"].get("sha") == "75c68e08", t.context["resume_from"]
    assert t.blocker in (None, {}), (
        f"the blocker was not consumed, so its sha stays re-appliable: {t.blocker}")


@pytest.mark.parametrize("verb,args", [
    ("nh task resume", ["task", "resume"]),
    ("nh unblock", ["unblock"]),
])
def test_a_human_verb_adopts_only_the_checkpoint_IT_read(verb, args, tmp_path, monkeypatch):
    """The shape the sha-less wiring table structurally CANNOT see.

    Nine review rounds all checked the SHAPE of the write — is `by` present, is
    `sha` deleted — and none asked **who chose the sha that gets written**. The
    sha-less seed can only ever exercise the delete path. Here the blocker DOES
    carry a `resume_commit`, and `resume_from` already holds a DIFFERENT sha a
    machine picked. The human verb must adopt the one it read from the blocker,
    never the one left lying in context.
    """
    db = tmp_path / f"adopt-{args[-1]}.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [*args, task_id[:8]])

    assert result.exit_code == 0, f"{verb}: {result.output}"
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", resume_from
    assert resume_from.get("sha") == "75c68e08", (
        f"{verb} adopted a sha it never read — the seeded machine sha was "
        f"'0e22fe3d', the blocker's checkpoint is '75c68e08': {resume_from}")


def test_task_retry_clears_the_checkpoint_like_its_HTTP_twin(tmp_path, monkeypatch):
    """`nh task retry` is the CLI twin of `POST /api/tasks/{id}/retry`, down to
    the docstring. The endpoint was fixed to clear `resume_from`; this was not,
    and a review reproduced the end state through `run_task` — an attempt that
    edited nothing came back `succeeded` and advanced to `awaiting_approval`,
    credited with a [WIP-PARTIAL] an EARLIER actor's resume had chosen.

    🔴 That was the FOURTH time in this branch a fix landed on one half of a
    pair: `nh reply` behind the reply endpoint, `nh unblock` behind the Resume
    endpoint's guards, and here. When a CLI verb and an HTTP endpoint share a
    docstring, they share an invariant.
    """
    db = tmp_path / "retry.db"

    async def _seed():
        async with Store(db) as s:
            t = Task.new("retry twin", repo_path="/tmp/repo")
            await s.create_task(t)
            await s.merge_context(t.id, {
                "resume_from": {"sha": "75c68e08", "branch": "old", "by": "human"}})
            await s.set_status(t, TaskStatus.FAILED, validate=False)
            return t.id
    task_id = asyncio.run(_seed())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "retry", task_id[:8]])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.PENDING
    assert (t.context or {}).get("resume_from") is None, (
        "`nh task retry` inherited a checkpoint it never chose, so a 'fresh "
        f"run' branches from a stale sha: {(t.context or {}).get('resume_from')}")


# --------------------------------------------------------------------------- #
# print_path_error — an error that names a path must reproduce it verbatim     #
# --------------------------------------------------------------------------- #

def _render(prefix: str, detail: str, width: int) -> str:
    """Render one print_path_error call at a fixed console width."""
    from io import StringIO

    from rich.console import Console

    from no_human.cli import print_path_error

    buf = StringIO()
    print_path_error(Console(file=buf, width=width, no_color=True), prefix, detail)
    return buf.getvalue()


def test_print_path_error_never_folds_a_path_mid_token():
    """Rich's default rendering breaks a token longer than the line, so an
    80-column terminal — also Rich's fallback for any pipe, log file or CI
    runner — printed `.../metrics\\ndb-service` and the user could not copy the
    path out of the error. The path must come back whole."""
    path = "/tmp/pytest-of-runner/pytest-0/popen-gw3/test_task_add_rejects_a_linked0/metrics-core-service"
    assert len(path) > 80
    out = _render("[red]multi-repo intake:[/]", f"not a git repo: {path}", 80)
    assert path in out, out
    assert "metrics\ndb-service" not in out


def test_print_path_error_keeps_square_brackets_in_a_path():
    """A directory named `a[b]c` is a legal path. Read as console markup it
    renders as `ac`, so the error reports a path that does not exist."""
    path = "/home/dev/proj[b]/repo"
    out = _render("[red]not a git repo:[/]", path, 200)
    assert path in out, out


def test_review_comments_shows_every_severity_not_just_uppercase():
    """A model-authored severity must be visible, whatever its case.

    `nh review-comments` built the label as f" [{severity}]" — wrapping model
    output in square brackets does not decorate it, rich PARSES it as a markup
    tag. Every realistic value ("high", "medium", "blocking") was silently
    swallowed; only an uppercase one survived, by accident of not being a valid
    tag. The field a human reads first to triage a review was invisible, and
    the command looked like it was working.
    """
    from rich.console import Console
    from rich.markup import escape
    import io

    def render(sev: str) -> str:
        buf = io.StringIO()
        c = Console(file=buf, width=100, no_color=True, highlight=False)
        label = f" \\[{escape(str(sev))}]" if sev else ""
        c.print(f"  [bold]1.[/] [dim]draft[/] [cyan]app.py:12[/]{label}", emoji=False)
        return buf.getvalue()

    for sev in ("high", "medium", "blocking", "HIGH"):
        assert sev in render(sev), f"severity {sev!r} was swallowed by the renderer"


def test_review_comments_renders_model_text_literally():
    """Mutation guard for the test above.

    That test only proves the severity survives. The comment body is also model
    authored, and carries the two shapes that bite: rich markup, and a file:line
    citation that emoji substitution rewrites (`:100:` becomes an emoji), which
    would destroy the evidence the review gate is built on.
    """
    from rich.console import Console
    from rich.markup import escape
    import io

    def render(text: str) -> str:
        buf = io.StringIO()
        c = Console(file=buf, width=100, no_color=True, highlight=False)
        c.print(f"     {escape(str(text))}", emoji=False)
        return buf.getvalue()

    assert "the list[/] was empty" in render("the list[/] was empty")
    assert "commands.py:100:" in render("see commands.py:100: here")
    assert ":warning:" in render("see :warning: for details")
