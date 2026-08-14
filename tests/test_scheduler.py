"""Phase 7.3/7.4: the concurrent scheduler — pool cap, no double-dispatch,
shared-quota gate, wake integration. Uses a controllable fake orchestrator so the
scheduling logic is tested in isolation (the real run_task is covered elsewhere)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from no_human.core.db import Store
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


class FakeOrch:
    """Records run_task calls; optionally blocks on a gate; sets a terminal DB
    status so finished tasks aren't re-claimed."""

    def __init__(self, store, *, hold=None, terminal=TaskStatus.AWAITING_APPROVAL,
                 quota_first=False, quota_resets=None):
        self.store = store
        self.hold = hold
        self.terminal = terminal
        self.quota_first = quota_first
        self.quota_resets = quota_resets
        self.started: list[str] = []
        self.max_concurrent = 0
        self._active = 0

    async def run_task(self, task):
        self.started.append(task.id)
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.quota_first and len(self.started) == 1:
                task.wake_check_at = self.quota_resets
                await self.store.set_status(task, TaskStatus.PAUSED_QUOTA,
                                            validate=False)
                return SimpleNamespace(status=TaskStatus.PAUSED_QUOTA, task=task)
            await self.store.set_status(task, self.terminal, validate=False)
            return SimpleNamespace(status=self.terminal, task=task)
        finally:
            self._active -= 1


async def _mk_tasks(store, n):
    ids = []
    for i in range(n):
        t = Task.new(f"task {i}", repo_path="/tmp/x")
        await store.create_task(t)
        ids.append(t.id)
    return ids


async def test_pool_cap_and_no_double_dispatch(store):
    hold = asyncio.Event()
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    await _mk_tasks(store, 3)

    started1 = await sched.tick()
    assert len(started1) == 2                 # capped at max_workers
    assert len(sched.inflight) == 2

    started2 = await sched.tick()
    assert started2 == []                      # pool full → nothing new
    assert len(sched.inflight) == 2            # no double-dispatch

    hold.set()
    await asyncio.sleep(0.05)                   # let the 2 finish
    assert len(sched.inflight) == 0

    started3 = await sched.tick()
    assert len(started3) == 1                   # the third task now runs
    await asyncio.sleep(0.05)
    assert fake.max_concurrent == 2             # never exceeded the cap


async def test_inflight_task_not_reclaimed(store):
    fake = FakeOrch(store, hold=asyncio.Event())  # never releases
    sched = Scheduler(store, lambda task=None: fake, max_workers=4)
    ids = await _mk_tasks(store, 2)

    await sched.tick()
    assert len(sched.inflight) == 2
    # A second tick must not re-dispatch the same still-running tasks.
    again = await sched.tick()
    assert again == []
    assert sched.inflight == set(ids)


async def test_quota_pause_gates_the_whole_pool(store):
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    resets = (now + timedelta(hours=1)).isoformat()
    fake = FakeOrch(store, quota_first=True, quota_resets=resets)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    await _mk_tasks(store, 1)

    await sched.tick(now=now)
    await asyncio.sleep(0.05)                    # first task parks PAUSED_QUOTA
    assert sched._quota_cooldown_until is not None

    # A new task arrives, but the pool is paused until the reset time.
    await _mk_tasks(store, 1)
    during = await sched.tick(now=now + timedelta(minutes=10))
    assert during == []                           # gated pool-wide

    after = await sched.tick(now=now + timedelta(hours=2))
    assert len(after) == 1                         # resumes once quota is back


@pytest.fixture
def _infra_breaker():
    """The breaker is a process-wide singleton (`core/infra_breaker.py`) —
    reset it around the test so it cannot leak into (or be polluted by)
    any other test in the same worker process."""
    from no_human.core.infra_breaker import infra_breaker as _get

    _get().reset()
    yield _get()
    _get().reset()


async def test_infra_breaker_pauses_the_whole_pool_and_lapsing_resumes_it(
        store, _infra_breaker):
    """INCIDENT 2026-08-13: 3 consecutive zero-token/auth SDK failures across
    distinct tasks means the account or transport itself is down — the WHOLE
    pool must stop dispatching, via the same cooldown gate a single-task
    quota park already uses, and resume once it lapses (the existing quota
    watcher's job; this test proves the scheduler side only)."""
    _infra_breaker.record_infra_failure("task-a")
    _infra_breaker.record_infra_failure("task-b")
    _infra_breaker.record_infra_failure("task-c")
    assert _infra_breaker.tripped() is not None

    events = []
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                      on_event=lambda kind, text: events.append((kind, text)))
    await _mk_tasks(store, 1)
    now = datetime(2026, 8, 13, 17, 14, tzinfo=timezone.utc)

    during = await sched.tick(now=now)
    assert during == [], "the fleet breaker must gate dispatch pool-wide"
    assert sched._quota_cooldown_until is not None
    assert any(kind == "quota_pause" for kind, _ in events)

    # Lapse the cooldown (the existing quota-watcher's job in production) and
    # tick again: dispatch resumes and the breaker's streak is cleared, or a
    # single stale incident would pause the fleet forever.
    sched._quota_cooldown_until = now - timedelta(seconds=1)
    after = await sched.tick(now=now + timedelta(seconds=5))
    assert len(after) == 1
    assert _infra_breaker.tripped() is None


def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_work_repo(tmp_path, name):
    import subprocess
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / name
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest.mark.slow
async def test_two_repos_run_concurrently_in_worktrees(store, tmp_path):
    """Phase 7 DoD: two tasks in DIFFERENT repos run through the pool, each in its
    own worktree, both open a PR — with no git corruption."""
    from no_human.agent.claude_backend import AgentResult
    from no_human.config import load_config
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier
    from no_human.review.reviewer import ReviewDecision
    from no_human.review.selfcheck import ChecklistItem
    from no_human.vcs import GitRepo

    repo_a = _make_work_repo(tmp_path, "metrics-core")
    repo_b = _make_work_repo(tmp_path, "analytics-export")

    def mutate(cwd):
        from pathlib import Path
        (Path(cwd) / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (Path(cwd) / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    class Backend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            mutate(cwd)
            return AgentResult(final_text="done", num_turns=1, is_error=False,
                               tokens_used=10, session_id="s", stop_reason="end_turn")

    class Reviewer:
        async def review(self, task, *, repo_path, **kw):
            return ReviewDecision(passed=True,
                                  checklist=[ChecklistItem("ok", True, "calc.py:3")])

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["concurrency"] = {"enabled": True, "max_workers": 2,
                               "worktree_root": str(tmp_path / "wt")}

    def factory(task=None):
        return Orchestrator(store, cfg.data, Backend(), SlackNotifier(None),
                            reviewer=Reviewer())

    ta = Task.new("metrics-core story", repo_path=str(repo_a))
    tb = Task.new("analytics-export story", repo_path=str(repo_b))
    await store.create_task(ta)
    await store.create_task(tb)

    sched = Scheduler(store, factory, max_workers=2)
    started = await sched.tick()
    assert len(started) == 2
    await sched.drain()

    a2 = await store.get_task(ta.id)
    b2 = await store.get_task(tb.id)
    assert a2.status == TaskStatus.AWAITING_APPROVAL
    assert b2.status == TaskStatus.AWAITING_APPROVAL
    # Worktrees cleaned up in both repos; primary checkouts untouched.
    assert all("/wt/" not in w for w in GitRepo(repo_a).list_worktrees())
    assert all("/wt/" not in w for w in GitRepo(repo_b).list_worktrees())
    assert "mul" not in (repo_a / "calc.py").read_text()


async def test_wake_watcher_ticked_and_implementing_is_claimable(store):
    class FakeWake:
        def __init__(self):
            self.ticked = False

        async def tick(self, *, now=None, active_ids=None):
            self.ticked = True

    wake = FakeWake()
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=2, wake_watcher=wake)

    # A task already in IMPLEMENTING (e.g. just resumed) is claimable.
    t = Task.new("resumed", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    started = await sched.tick()
    assert wake.ticked
    assert t.id in started


# --------------------------------------------------------------------------- #
# PR-E: ReanalysisJob                                                          #
# --------------------------------------------------------------------------- #

from no_human.core.scheduler import ReanalysisJob


@pytest.mark.asyncio
async def test_reanalysis_due_after_interval(store):
    """ReanalysisJob is due immediately (last_run=0), then not due after running."""
    job = ReanalysisJob(store, interval_seconds=60)
    assert job.due()
    # Simulate a run completing.
    job._last_run = __import__("time").time()
    assert not job.due()


@pytest.mark.asyncio
async def test_reanalysis_maybe_run_skips_when_not_due(store):
    """maybe_run returns None when not due."""
    import time as _time
    job = ReanalysisJob(store, interval_seconds=9999)
    job._last_run = _time.time()  # just ran
    result = await job.maybe_run()
    assert result is None


def _fixed_transcripts(monkeypatch):
    """Feed the job one transcript instead of scanning the developer's machine.

    ``ReanalysisJob._run`` → ``TranscriptIngester.ingest`` → ``extract_transcripts``,
    which hunts for a live IDE process and raises ``IDENotRunningError`` when it
    finds none. That made both tests below pass or fail on whether an editor
    happened to be open, which is neither a property of the scheduler nor
    reproducible for anyone else running the suite.

    Only the machine scan is replaced. Everything the tests actually assert on —
    analysis, enqueueing, the transcript cache, the dedupe key — runs for real.
    The patch targets the name bound in ``ingester``'s namespace, which is what
    ``ingest`` resolves at call time.
    """
    from no_human.history.extractor import Message, Transcript

    transcript = Transcript(
        cascade_id="cascade-reanalysis", title="A session", created="2026-07-01",
        messages=[
            Message("assistant", "I'll take a look.", "STEP"),
            Message("user", "never commit secrets to the repo", "STEP"),
        ],
    )
    monkeypatch.setattr("no_human.history.ingester.extract_transcripts",
                        lambda **kw: [transcript])
    return transcript


@pytest.mark.asyncio
async def test_reanalysis_maybe_run_produces_result(store, monkeypatch):
    """maybe_run returns a populated result when due."""
    _fixed_transcripts(monkeypatch)
    job = ReanalysisJob(store, interval_seconds=60, days=1)
    # Due because _last_run is 0.
    result = await job.maybe_run()
    assert result is not None
    assert result["transcripts"] == 1
    # The transcript carries a user correction the heuristic analyzer flags, so
    # this asserts the pipeline actually produced something rather than merely
    # returning a dict with the right keys.
    assert result["proposed"] > 0
    assert "duplicates" in result


@pytest.mark.asyncio
async def test_reanalysis_dedup_across_runs(store, monkeypatch):
    """Re-running over the same transcript proposes nothing new — both layers.

    Dedup is two independent mechanisms, and the old assertion (``duplicates >=
    0``, true of any count) could not detect either one breaking. Layer 1 is the
    transcript cache: an unchanged transcript is never re-analyzed. Layer 2 is
    the content-based dedupe key: even when analysis IS redone, a finding
    already in the queue is counted as a duplicate rather than enqueued twice.
    """
    _fixed_transcripts(monkeypatch)
    job = ReanalysisJob(store, interval_seconds=0, days=1)

    r1 = await job.maybe_run()
    assert r1 is not None and r1["proposed"] > 0

    # Layer 1 — same transcript, still cached: nothing is re-analyzed. Asserted
    # on `findings`, not `proposed`: layer 2 would hold `proposed` at 0 even
    # with the cache broken, so `proposed` alone cannot tell the layers apart
    # and a cache regression would slip through green.
    job._running = False  # reset guard
    job._last_run = 0     # force re-run
    r2 = await job.maybe_run()
    assert r2 is not None
    assert r2["findings"] == 0, "cached transcript was re-analyzed"
    assert r2["proposed"] == 0, "cached transcript was re-proposed"

    # Layer 2 — drop the cache so the same findings are produced again. They are
    # already queued, so they must land as duplicates, not as new proposals.
    await store.history_cache_clear()
    job._running = False
    job._last_run = 0
    r3 = await job.maybe_run()
    assert r3 is not None
    assert r3["proposed"] == 0, "re-analyzed findings were proposed a second time"
    assert r3["duplicates"] > 0, "dedupe key did not recognize the repeat findings"


@pytest.mark.asyncio
async def test_reanalysis_survives_no_ide_running(store, monkeypatch, caplog):
    """No IDE running is the ORDINARY case on a clean install, not a failure.

    ``ReanalysisJob`` is due immediately on every fresh boot (``_last_run=0``),
    and ``TranscriptIngester.ingest`` was the one caller of
    ``extract_transcripts`` that did not catch ``IDENotRunningError`` the way
    ``nh history``/``nh bench build``/the onboarding scan already do — so it
    propagated out of ``maybe_run`` and, via ``Scheduler.tick``, became a
    "re-analysis failed: No Windsurf language server found. Is the IDE
    running?" WARNING on every single startup. This must degrade silently to
    zero Windsurf transcripts instead.
    """
    import logging

    from no_human.history.extractor import IDENotRunningError

    def _raise(**kw):
        raise IDENotRunningError("No Windsurf language server found. Is the IDE running?")

    monkeypatch.setattr("no_human.history.ingester.extract_transcripts", _raise)
    job = ReanalysisJob(store, interval_seconds=60, days=1)

    with caplog.at_level(logging.WARNING):
        result = job.due()
        assert result
        out = await job.maybe_run()

    assert out is not None, "a missing IDE must not raise out of maybe_run"
    assert out["transcripts"] == 0
    assert not any(
        "re-analysis failed" in r.message for r in caplog.records
    ), f"missing-IDE degraded to a boot warning: {[r.message for r in caplog.records]}"
    assert not any(
        "No Windsurf language server found" in r.message for r in caplog.records
        if r.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_scheduler_tick_triggers_reanalysis(store):
    """Scheduler.tick() triggers the re-analysis job when it's due."""
    events = []
    job = ReanalysisJob(store, interval_seconds=0, days=1)
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(
        store, lambda task=None: fake, max_workers=1,
        on_event=lambda k, t: events.append((k, t)),
        reanalysis_job=job,
    )
    await sched.tick()
    # Job ran — even if no proposals, no error should have occurred.


# --------------------------------------------------------------------------- #
# Memory lifecycle C: RetirementSweepJob                                      #
# --------------------------------------------------------------------------- #

from no_human.core.scheduler import RetirementSweepJob


@pytest.mark.asyncio
async def test_retirement_sweep_due_immediately_then_not(store):
    """due() is False immediately after maybe_run() — the same shape as
    ReanalysisJob's due-after-interval test."""
    job = RetirementSweepJob(store, interval_seconds=60)
    assert job.due()  # _last_run == 0.0 at construction
    result = await job.maybe_run()
    assert result is not None
    assert not job.due()


@pytest.mark.asyncio
async def test_retirement_sweep_maybe_run_skips_when_not_due(store):
    job = RetirementSweepJob(store, interval_seconds=9999)
    job._last_run = time.time()  # just ran
    assert await job.maybe_run() is None


@pytest.mark.asyncio
async def test_retirement_sweep_archives_old_unconfirmed_proposals(store):
    from no_human.learning import TYPE_SKILL

    old_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="old", content="x", confirmed=False)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
        "%Y-%m-%d %H:%M:%S")
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (cutoff, old_id))
    await store.db.commit()

    job = RetirementSweepJob(store, interval_seconds=60, archive_after_days=45)
    result = await job.maybe_run()
    assert result == {"archived": 1}

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (old_id,))
    assert row["archived"] == 1


@pytest.mark.asyncio
async def test_retirement_sweep_last_run_advances_even_if_run_raises(store, monkeypatch):
    """A raising `_run` must still advance `_last_run`, or a persistently
    failing sweep would retry every single tick instead of backing off."""
    job = RetirementSweepJob(store, interval_seconds=60)

    async def _boom():
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(job, "_run", _boom)
    assert job.due()
    with pytest.raises(RuntimeError):
        await job.maybe_run()
    assert not job.due(), "_last_run must advance even though _run raised"
    assert not job._running


@pytest.mark.asyncio
async def test_scheduler_disabled_retirement_job_never_writes(store):
    """`enabled=False` (the job simply never being constructed/passed) means
    the scheduler's tick never touches the memories table."""
    from no_human.learning import TYPE_SKILL

    old_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="old", content="x", confirmed=False)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
        "%Y-%m-%d %H:%M:%S")
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (cutoff, old_id))
    await store.db.commit()

    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(
        store, lambda task=None: fake, max_workers=1,
        on_event=lambda k, t: None,
        retirement_job=None,  # disabled
    )
    await sched.tick()

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (old_id,))
    assert row["archived"] == 0


@pytest.mark.asyncio
async def test_scheduler_tick_triggers_retirement_sweep(store):
    from no_human.learning import TYPE_SKILL

    old_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="old", content="x", confirmed=False)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
        "%Y-%m-%d %H:%M:%S")
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (cutoff, old_id))
    await store.db.commit()

    events = []
    job = RetirementSweepJob(store, interval_seconds=0, archive_after_days=45)
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(
        store, lambda task=None: fake, max_workers=1,
        on_event=lambda k, t: events.append((k, t)),
        retirement_job=job,
    )
    await sched.tick()

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (old_id,))
    assert row["archived"] == 1
    assert any(k == "memory_sweep" for k, _ in events)


# --------------------------------------------------------------------------- #
# WikiRefreshJob                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wiki_refresh_job_due_timing():
    from no_human.core.scheduler import WikiRefreshJob
    # FakeBackend not needed — only testing due() logic.
    job = WikiRefreshJob(None, None, interval_seconds=60)
    assert job.due()  # first call always due
    job._last_run = time.time()
    assert not job.due()  # just ran, not due
    job._last_run = time.time() - 61
    assert job.due()  # past interval


@pytest.mark.asyncio
async def test_wiki_refresh_job_skips_matching_commit(store, tmp_path):
    """WikiRefreshJob skips repos where HEAD == wiki_commit (no-op)."""
    from no_human.core.scheduler import WikiRefreshJob

    job = WikiRefreshJob(store, None, interval_seconds=0)
    # No projects → nothing to do.
    result = await job.maybe_run()
    assert result == []


# --------------------------------------------------------------------------- #
# _summarize_event                                                             #
# --------------------------------------------------------------------------- #

from no_human.core.scheduler import _summarize_event


def test_summarize_event_tool_use_read():
    ev = {"kind": "tool_use", "tool_name": "Read", "tool_input": {"file_path": "/a/b/foo.py"}}
    assert _summarize_event(ev) == "reading foo.py"


def test_summarize_event_tool_use_edit():
    ev = {"kind": "tool_use", "tool_name": "Edit", "tool_input": {"file_path": "/x/bar.js"}}
    assert _summarize_event(ev) == "editing bar.js"


def test_summarize_event_tool_use_bash():
    ev = {"kind": "tool_use", "tool_name": "Bash", "tool_input": {"command": "pytest -x"}, "text": ""}
    assert _summarize_event(ev) == "running: pytest -x"


def test_summarize_event_state():
    ev = {"kind": "state", "text": "implementing"}
    assert _summarize_event(ev) == "implementing"


def test_summarize_event_commit():
    ev = {"kind": "commit", "text": "abc1234 fix bug"}
    assert _summarize_event(ev) == "committing changes"


def test_summarize_event_tests():
    ev = {"kind": "tests", "text": "3 passed"}
    assert _summarize_event(ev) == "running tests"


def test_summarize_event_irrelevant():
    ev = {"kind": "unknown_event", "text": "noise"}
    assert _summarize_event(ev) is None


# --------------------------------------------------------------------------- #
# _check_compound_parent via Scheduler                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_check_compound_parent_marks_done(store):
    """When all sub-tasks are DONE, the parent transitions to DONE."""
    parent = Task.new("compound", repo_path="/tmp/r")
    await store.create_task(parent)
    await store.set_status(parent, TaskStatus.IMPLEMENTING, validate=False)
    await store.set_status(parent, TaskStatus.COMPOUND_PARENT, validate=False)

    sub = Task.new("sub", repo_path="/tmp/r", parent_id=parent.id)
    sub.status = TaskStatus.DONE
    await store.create_task(sub)

    events = []
    sched = Scheduler(
        store, lambda task=None: None, max_workers=1,
        on_event=lambda k, t: events.append((k, t)),
    )

    await sched._check_compound_parent(sub)

    refreshed = await store.get_task(parent.id)
    assert refreshed.status == TaskStatus.DONE
    assert any(k == "compound_resolved" for k, _ in events)


@pytest.mark.asyncio
async def test_check_compound_parent_ignores_non_compound(store):
    """If parent is not COMPOUND_PARENT, _check_compound_parent is a no-op."""
    parent = Task.new("normal", repo_path="/tmp/r")
    await store.create_task(parent)

    sub = Task.new("sub", repo_path="/tmp/r", parent_id=parent.id)
    sub.status = TaskStatus.DONE
    await store.create_task(sub)

    sched = Scheduler(store, lambda task=None: None, max_workers=1)
    await sched._check_compound_parent(sub)

    refreshed = await store.get_task(parent.id)
    assert refreshed.status == TaskStatus.PENDING  # unchanged


@pytest.mark.asyncio
async def test_live_status_populated_via_sink(store):
    """_sink callback populates _live_status on the scheduler."""
    hold = asyncio.Event()

    class FakeOrchWithSink:
        async def run_task(self, task):
            # Simulate a tool_use event via the _sink callback
            self._sink({
                "kind": "tool_use",
                "tool_name": "Read",
                "tool_input": {"file_path": "/a/b/test.py"},
                "text": "Read test.py",
            })
            hold.set()
            await asyncio.sleep(0.01)
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchWithSink()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await hold.wait()
    assert sched.get_live_status(t.id) == "reading test.py"

    await asyncio.sleep(0.05)  # let run finish
    # After task finishes, live_status should be cleared.
    assert sched.get_live_status(t.id) is None


@pytest.mark.asyncio
async def test_events_persisted_to_store_on_task_finish(store):
    """After a run finishes, its events are durably saved via store.save_events
    so they survive a server restart (Activity/System tabs)."""
    hold = asyncio.Event()

    class FakeOrchWithSink:
        async def run_task(self, task):
            self._sink({"kind": "tool_use", "tool_name": "Read",
                        "tool_input": {"file_path": "/a/b/test.py"}, "text": "Read test.py"})
            self._sink({"kind": "result", "text": "done"})
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            hold.set()
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchWithSink()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await hold.wait()
    await asyncio.sleep(0.05)  # let the finally block's save_events land

    persisted = await store.list_events(t.id)
    # 3, not 2: `set_status(..., DONE, event=...)` now persists its own
    # completion event atomically with the status write (no silent done).
    assert len(persisted) == 3
    assert persisted[0]["kind"] == "tool_use"
    assert persisted[1]["kind"] == "result"


@pytest.mark.asyncio
async def test_sink_preserves_subagent_task_id(store):
    """A subagent event's own task_id (the SDK's per-dispatch Task-tool id,
    e.g. from claude_backend.py's subagent_start meta) must survive _sink
    untouched — it is a different concept from "which no_human task is
    this" and must not be overwritten. Regression for a bug where every
    distinct subagent dispatch collapsed to one node in the System view
    because _sink unconditionally stamped the outer task's id over it."""
    hold = asyncio.Event()

    class FakeOrchWithSubagentEvents:
        async def run_task(self, task):
            self._sink({"kind": "subagent_start", "text": "Research A",
                        "task_id": "sdk-dispatch-aaa"})
            self._sink({"kind": "subagent_start", "text": "Research B",
                        "task_id": "sdk-dispatch-bbb"})
            # An ordinary event with no task_id of its own still gets the
            # no_human task's id backfilled (existing, still-needed behavior).
            self._sink({"kind": "tool_use", "tool_name": "Read"})
            hold.set()
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchWithSubagentEvents()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await hold.wait()

    events = sched.task_events(t.id)
    subagent_ids = {e["task_id"] for e in events if e["kind"] == "subagent_start"}
    assert subagent_ids == {"sdk-dispatch-aaa", "sdk-dispatch-bbb"}, (
        "distinct subagent dispatches must keep distinct task_ids"
    )
    tool_use = next(e for e in events if e["kind"] == "tool_use")
    assert tool_use["task_id"] == t.id  # backfilled, no id of its own


# --------------------------------------------------------------------------- #
# Events are persisted while the run is live, not only after it ends           #
# --------------------------------------------------------------------------- #

async def test_events_are_persisted_mid_run(store):
    """A crash used to lose the whole history: save_events ran once, in the
    `finally`. Mid-run the API served 133 events while task_events held 0."""
    mid_run_rows: list[int] = []

    class FakeOrchObservingItsOwnPersistence:
        _sink = staticmethod(lambda e: None)

        async def run_task(self, task):
            for i in range(5):
                self._sink({"kind": "tool_use", "tool_name": f"Read{i}"})
            # Give the flusher a chance to run while we are still "in" the task.
            await asyncio.sleep(0.05)
            mid_run_rows.append(len(await store.list_events(task.id)))
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchObservingItsOwnPersistence()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    sched._EVENT_FLUSH_INTERVAL = 0.01
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    while sched.inflight:
        await asyncio.sleep(0.01)

    assert mid_run_rows == [5], "events must reach SQLite before the run ends"
    # 6, not 5: the final flush must not re-insert what was already written,
    # plus the one completion event `set_status(..., DONE, event=...)` now
    # persists atomically with the status write.
    assert len(await store.list_events(t.id)) == 6


async def test_flushed_events_are_not_duplicated_by_the_final_flush(store):
    """save_events INSERTs. Handing it the full buffer on every flush would
    write each event once per flush."""
    class SlowFakeOrch:
        _sink = staticmethod(lambda e: None)

        async def run_task(self, task):
            for i in range(3):
                self._sink({"kind": "tool_use", "tool_name": f"Read{i}"})
                await asyncio.sleep(0.03)   # several flush intervals elapse
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    sched = Scheduler(store, lambda task=None: SlowFakeOrch(), max_workers=1)
    sched._EVENT_FLUSH_INTERVAL = 0.01
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    while sched.inflight:
        await asyncio.sleep(0.01)

    persisted = await store.list_events(t.id)
    # 4, not 3: `set_status(..., DONE, event=...)` persists its own
    # completion event atomically with the status write.
    assert len(persisted) == 4
    tool_use = [e for e in persisted if e.get("kind") == "tool_use"]
    assert [e["tool_name"] for e in tool_use] == ["Read0", "Read1", "Read2"]


# --------------------------------------------------------------------------- #
# The pool is never wider than the worktree isolation allows                   #
# --------------------------------------------------------------------------- #

def test_pool_is_clamped_to_one_when_concurrency_is_disabled():
    """The live config was exactly this, and the server announced
    '2 worker(s) · concurrent' while parallelism was off — so two tasks would
    be dispatched into a pool the operator never asked for."""
    from no_human.core.scheduler import resolve_max_workers

    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": False, "max_workers": 2}})
    assert workers == 1
    assert warning and "concurrency.enabled is false" in warning


def test_an_explicit_worker_flag_is_clamped_too():
    from no_human.core.scheduler import resolve_max_workers

    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": False, "max_workers": 1}}, override=4)
    assert workers == 1, "a flag must not buy unisolated parallelism"
    assert warning


def test_concurrency_enabled_honours_the_configured_width():
    from no_human.core.scheduler import resolve_max_workers

    # `cpu_count` is pinned so this asserts the honouring, not the machine:
    # the width is now also bounded from above by cpu_count//3, so on a small
    # box an unpinned 3 or 4 would be clamped and this test would report a
    # machine size as a regression.
    assert resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 3}}, cpu_count=12) == (3, None)
    assert resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 1}},
        override=4, cpu_count=12) == (4, None)


def test_resolve_max_workers_defaults_are_serial_and_silent():
    from no_human.core.scheduler import resolve_max_workers

    assert resolve_max_workers({}) == (1, None)
    assert resolve_max_workers({"concurrency": {}}) == (1, None)
    # A single worker with concurrency off is the normal case: no warning.
    assert resolve_max_workers({"concurrency": {"enabled": False, "max_workers": 1}}) == (1, None)
    # Degenerate values never produce a zero-width pool.
    assert resolve_max_workers({"concurrency": {"enabled": True, "max_workers": 0}}) == (1, None)


def test_explicit_serve_flag_enables_isolated_pool():
    """SCRUM-10: `nh serve --max-workers N` must run the pool without a
    config edit — the opposite of resolve_max_workers's clamp, since the
    flag itself is what turns isolation on for this invocation."""
    from no_human.core.scheduler import resolve_serve_pool

    # cpu_count pinned: on a 4-core CI runner the sanity ceiling is
    # max(2, 4//3) == 2 and this test would fail for machine size, not
    # behavior — the exact class the clamp change pinned everywhere else.
    workers, enabled, error = resolve_serve_pool(
        {"concurrency": {"enabled": False, "max_workers": 1}}, cli_workers=3,
        cpu_count=12)
    assert (workers, enabled, error) == (3, True, None)


def test_serve_without_flag_and_disabled_refuses():
    """Absent flag = unchanged: still refuses to serve when concurrency is
    off in config, exactly like before this feature existed."""
    from no_human.core.scheduler import resolve_serve_pool

    workers, enabled, error = resolve_serve_pool(
        {"concurrency": {"enabled": False, "max_workers": 1}}, cli_workers=None)
    assert enabled is False
    assert error is not None


def test_serve_without_flag_honours_config():
    """Absent flag = unchanged: an already-enabled config drives the pool
    width exactly as before."""
    from no_human.core.scheduler import resolve_serve_pool

    assert resolve_serve_pool(
        {"concurrency": {"enabled": True, "max_workers": 2}}, cli_workers=None,
    ) == (2, True, None)


def test_serve_without_flag_defaults_to_two_when_enabled_and_unset():
    """Absent flag = unchanged: serve()'s historical default was 2 workers
    when concurrency.enabled is true but max_workers isn't set — must not
    silently drop to resolve_max_workers's 1-worker override default."""
    from no_human.core.scheduler import resolve_serve_pool

    assert resolve_serve_pool(
        {"concurrency": {"enabled": True}}, cli_workers=None,
    ) == (2, True, None)


def test_serve_flag_rejects_non_positive():
    """CLI hygiene: zero/negative --max-workers is rejected with a clear
    error rather than silently degrading to some other width."""
    from no_human.core.scheduler import resolve_serve_pool

    workers, enabled, error = resolve_serve_pool({}, cli_workers=0)
    assert error is not None
    assert workers == 0

    workers, enabled, error = resolve_serve_pool({}, cli_workers=-1)
    assert error is not None
    assert workers == 0


# --------------------------------------------------------------------------- #
# ... and never wider than the machine can carry                               #
# --------------------------------------------------------------------------- #

def test_the_ceiling_is_a_third_of_the_cores_with_a_floor_of_two():
    from no_human.core.scheduler import pool_width_ceiling

    assert pool_width_ceiling(12) == 4
    assert pool_width_ceiling(9) == 3
    # The floor keeps the shipped default (2) reachable on a small machine
    # instead of silently serialising it.
    assert pool_width_ceiling(6) == 2
    assert pool_width_ceiling(1) == 2


def test_a_width_above_the_ceiling_is_clamped_with_a_stated_reason():
    """`nh start --workers 64` was accepted in full: both resolvers bounded
    the pool from below only."""
    from no_human.core.scheduler import clamp_pool_width

    width, reason = clamp_pool_width(64, cpu_count=12)
    assert width == 4
    assert reason, "a clamp the operator cannot see is the silent accept again"
    # It says which width is running, which was asked for, and why.
    assert "64" in reason and "4" in reason
    # The why is what this ceiling actually is — a sanity bound on absurd
    # widths, derived from each worker owning a nested subprocess tree and a
    # pytest -n run. It must NOT be sold as a stability guarantee: crashes at
    # small widths are a separate, known issue and this does not fix them.
    assert "sanity ceiling" in reason
    assert "nested agent subprocesses" in reason and "pytest -n" in reason
    assert "known separate issue" in reason
    # And it cites no particular machine's saved config as its evidence.
    assert "this install" not in reason and "config records" not in reason


def test_a_width_at_or_below_the_ceiling_is_untouched_and_silent():
    from no_human.core.scheduler import clamp_pool_width

    assert clamp_pool_width(4, cpu_count=12) == (4, None)   # exactly at it
    assert clamp_pool_width(3, cpu_count=12) == (3, None)
    assert clamp_pool_width(1, cpu_count=12) == (1, None)
    assert clamp_pool_width(2, cpu_count=1) == (2, None)    # the floor


def test_resolve_max_workers_clamps_the_flag_and_the_config():
    from no_human.core.scheduler import resolve_max_workers

    # The flag path (`nh start --workers 64`).
    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 2}},
        override=64, cpu_count=12)
    assert workers == 4
    assert warning and "ceiling" in warning

    # The config path (`concurrency.max_workers: 64` on disk).
    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 64}}, cpu_count=12)
    assert workers == 4
    assert warning and "ceiling" in warning


def test_the_isolation_downgrade_still_wins_over_the_ceiling():
    """Order matters: an over-wide request with isolation off must still be
    told about ISOLATION — that is the switch it has to fix, and the operator
    only gets one warning per run."""
    from no_human.core.scheduler import resolve_max_workers

    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 64},
         "isolation": {"enabled": False}}, cpu_count=12)
    assert workers == 1
    assert warning and "isolation.enabled is false" in warning
    assert "ceiling" not in warning
    # ...and it quotes the width the operator ASKED for. This is what pins the
    # order: clamping first and then reporting the isolation refusal leaves
    # every assertion above true while the message reads "not 4" — a number the
    # operator never typed, about a limit that is not their problem here.
    assert "not 64" in warning, warning


def test_resolve_serve_pool_clamps_the_flag_and_the_config():
    from no_human.core.scheduler import resolve_serve_pool

    assert resolve_serve_pool(
        {}, cli_workers=64, cpu_count=12) == (4, True, None)
    assert resolve_serve_pool(
        {"concurrency": {"enabled": True, "max_workers": 64}},
        cli_workers=None, cpu_count=12) == (4, True, None)
    # Below the ceiling nothing moves — the flag still buys the pool it asked
    # for, and serve's historical no-flag default of 2 is untouched.
    assert resolve_serve_pool(
        {}, cli_workers=3, cpu_count=12) == (3, True, None)
    assert resolve_serve_pool(
        {"concurrency": {"enabled": True}}, cli_workers=None,
        cpu_count=12) == (2, True, None)


def test_bounded_xdist_workers():
    """The CPU-oversubscription guard (2026-07-11): 3 tasks × pytest -n auto
    on 12 cores must not spawn 36 workers."""
    from no_human.core.scheduler import bounded_xdist_workers
    assert bounded_xdist_workers(3, 12, None) == "4"      # 12//3
    assert bounded_xdist_workers(5, 12, None) == "2"      # 12//5
    assert bounded_xdist_workers(20, 12, None) == "1"     # floor at 1
    assert bounded_xdist_workers(1, 12, None) is None     # serial: untouched
    assert bounded_xdist_workers(3, 12, "8") is None      # explicit choice kept


@pytest.mark.asyncio
async def test_resumed_work_claims_before_fresh_pending(store):
    """WIP-first: a task resumed to IMPLEMENTING (sunk cost, operator waiting)
    must claim a free slot before a newer PENDING task. Live starvation
    (2026-07-24): every newly imported ticket jumped the single slot ahead of
    three budget-raised resumes, which then false-stalled on a 40-min cycle."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    resumed = Task.new("resumed WIP", repo_path="/tmp/x")
    await store.create_task(resumed)
    await store.set_status(resumed, TaskStatus.IMPLEMENTING, validate=False)
    fresh = Task.new("fresh pending", repo_path="/tmp/x")
    await store.create_task(fresh)

    started = await sched.tick()
    assert started == [resumed.id], (
        f"expected the resumed task to claim the slot, got {started}")


@pytest.mark.asyncio
async def test_wip_first_beats_an_older_pending(store):
    """WIP-first must hold even when the resumed task is the NEWER row —
    the naive fix (one merged `ORDER BY created_at ASC` across statuses)
    would hand the slot to the older PENDING task instead. The existing
    `test_resumed_work_claims_before_fresh_pending` can't catch that: there
    the resumed task already happens to be older, so a global ASC sort would
    pass it by accident too."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    older_pending = Task.new("older pending", repo_path="/tmp/x")
    older_pending.created_at = "2026-08-01T08:00:00+00:00"
    await store.create_task(older_pending)

    resumed = Task.new("resumed WIP, newer", repo_path="/tmp/x")
    resumed.created_at = "2026-08-11T08:00:00+00:00"
    await store.create_task(resumed)
    await store.set_status(resumed, TaskStatus.IMPLEMENTING, validate=False)

    started = await sched.tick()
    assert started == [resumed.id], (
        f"expected WIP-first to beat the older pending task, got {started}")


@pytest.mark.asyncio
async def test_oldest_pending_claims_before_newer_pending(store):
    """Bug (2026-08-12): the claim path consumed `list_tasks`, which is
    `created_at DESC` for the board, so the NEWEST pending ticket dispatched
    first and day-old tickets starved behind every fresh filing. Queue order
    within a status must be FIFO (oldest first).

    `older` is created SECOND (higher rowid) so neither insertion order nor
    rowid can accidentally produce the pass — only `created_at ASC` can."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    newer = Task.new("filed overnight", repo_path="/tmp/x")
    newer.created_at = "2026-08-12T08:00:00+00:00"
    await store.create_task(newer)

    older = Task.new("filed a day ago", repo_path="/tmp/x")
    older.created_at = "2026-08-11T08:00:00+00:00"
    await store.create_task(older)

    started = await sched.tick()
    assert started == [older.id], (
        f"expected the older pending task to claim the slot first, got {started}")


@pytest.mark.asyncio
async def test_claim_order_is_fifo_across_a_full_slice(store):
    """Guards against a fix that only sorts the head of the claim list: five
    PENDING tasks created in shuffled `created_at` order must be claimed in
    ascending `created_at` order across the whole slice, not just the first
    pick."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=3)

    stamps = [
        "2026-08-05T08:00:00+00:00",
        "2026-08-09T08:00:00+00:00",
        "2026-08-03T08:00:00+00:00",
        "2026-08-11T08:00:00+00:00",
        "2026-08-07T08:00:00+00:00",
    ]
    tasks = []
    for i, stamp in enumerate(stamps):
        t = Task.new(f"shuffled {i}", repo_path="/tmp/x")
        t.created_at = stamp
        await store.create_task(t)
        tasks.append(t)

    expected = [t.id for t in sorted(tasks, key=lambda t: t.created_at)[:3]]
    started = await sched.tick()
    assert started == expected, (
        f"expected the three oldest ids in ascending order, got {started}")


@pytest.mark.asyncio
async def test_startup_recovers_orphaned_midrun_tasks(tmp_path):
    """Review 2026-07-25: scoping the stuck sweep to claimed tasks left a
    task orphaned in a mid-run status (process killed during review/testing)
    invisible forever — not claimable, not swept. Startup must requeue it."""
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus

    store = await Store(tmp_path / "t.db").connect()
    try:
        orphan = Task.new("killed mid-review", repo_path="/r")
        await store.create_task(orphan)
        await store.set_status(orphan, TaskStatus.REVIEWING, validate=False)
        healthy = Task.new("parked", repo_path="/r")
        await store.create_task(healthy)
        await store.set_status(healthy, TaskStatus.ESCALATED, validate=False)

        events = []
        sched = Scheduler(store, lambda task=None: None,
                          on_event=lambda k, t: events.append((k, t)))
        await sched._recover_orphans()

        fresh = await store.get_task(orphan.id)
        assert fresh.status is TaskStatus.IMPLEMENTING  # claimable again
        recorded = await store.list_events(orphan.id)
        assert any(e["kind"] == "orphan_recovered" for e in recorded)
        assert any(k == "orphan_recovered" for k, _ in events)
        # Parked/terminal tasks are NOT touched.
        assert (await store.get_task(healthy.id)).status is TaskStatus.ESCALATED
    finally:
        await store.close()


def _branch_point(ctx: dict) -> str:
    """What the requeued run will actually branch from, asked of the code that
    decides it. A requeue re-enters `run_task` as a FRESH bounded loop, so
    attempt_n is 1 — the number at which `handoff.wip_sha` is ignored."""
    from no_human.core.orchestrator import Orchestrator
    return Orchestrator._resume_branch_point(None, None, ctx, 1)


class _SubjectRepo:
    """Stand-in for GitRepo where only the commit SUBJECT matters."""

    def __init__(self, subject: str):
        self.subject = subject

    def _run(self, *args, **kw):
        return self.subject


def _gate_armed(ctx: dict, sha: str, subject: str) -> bool:
    """Is the zero-diff honesty gate armed for this branch point?

    Asked of `_is_own_partial` itself, not of a proxy. True means the work
    already ahead of base is the LOOP's and must NOT be credited to an attempt
    that edited nothing.
    """
    from no_human.core.orchestrator import Orchestrator
    return Orchestrator._is_own_partial(
        Orchestrator.__new__(Orchestrator), _SubjectRepo(subject), ctx, sha)


@pytest.mark.asyncio
async def test_orphan_recovery_resumes_from_the_dead_attempts_commit(store):
    """A restart must resume the in-flight attempt, not burn it.

    Measured 2026-08-10: three server restarts produced 11 attempts closed as
    'interrupted: superseded by a newer attempt' across 5 live tasks, and every
    successor restarted the coder from base even though the dead attempt's work
    was committed and its sha recorded on the attempt row. Nothing was reported
    lost (`resume_checkpoint_lost` NULL on all of them) because no checkpoint
    was ever considered.
    """
    orphan = Task.new("killed mid-review", repo_path="/r")
    await store.create_task(orphan)
    await store.set_status(orphan, TaskStatus.REVIEWING, validate=False)
    attempt_id = await store.create_attempt(orphan.id, 1)
    sha = "a" * 40
    await store.update_attempt(attempt_id, commit_sha=sha)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    fresh = await store.get_task(orphan.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    resume_from = (fresh.context or {}).get("resume_from") or {}
    assert resume_from.get("sha") == sha, (
        "the requeued row carries no checkpoint — the successor attempt will "
        f"redo the dead attempt's work from base; got {resume_from!r}")
    # A machine requeue is NOT a human gate. Asked of the GATE, not of a proxy:
    # `by not in (None, "human")` was the assertion here and it proved nothing —
    # `_is_own_partial` short-circuited on the commit's SHAPE before it ever
    # read `by`, and `commit_sha` on an attempt row is the attempt's ORDINARY
    # work commit (`_run_attempt`: update_attempt(commit_sha=commit.sha)), never
    # a [WIP-PARTIAL]. So the label was right and the gate was DOWN: the
    # successor attempt could edit nothing, inherit this diff and be recorded
    # `succeeded` with `unproductive_streak` never incrementing.
    assert _gate_armed(fresh.context, sha, "fix: the dead attempt's work"), (
        "the zero-diff honesty gate is DOWN on a machine requeue whose "
        "checkpoint is an ordinary work commit")
    assert _branch_point(fresh.context) == sha


def test_a_human_gated_checkpoint_still_disarms_the_gate():
    """Control for the assertion above, in the direction that has regressed
    twice (D15 / task-84251cb2): a human who gated the branch point IS credited
    with the work already on it, whatever the commit's subject looks like.
    Arming there fails a correct "nothing to add" as fabrication."""
    for subject in ("fix: work a human gated", "[WIP-PARTIAL] half a feature"):
        ctx = {"resume_from": {"sha": "a" * 40, "by": "human"}}
        assert _gate_armed(ctx, "a" * 40, subject) is False


def test_the_gate_still_reads_the_subject_when_no_provenance_names_the_sha():
    """Control: on the `handoff.wip_sha` / reused-`pr_branch` paths nothing
    records who produced the branch point, so the SUBJECT remains the signal —
    provenance only outranks shape for the sha it actually names."""
    ctx = {"resume_from": {"sha": "b" * 40, "by": "human"}}
    assert _gate_armed(ctx, "a" * 40, "[WIP-PARTIAL] half a feature") is True
    assert _gate_armed(ctx, "a" * 40, "fix: a real commit") is False


@pytest.mark.asyncio
async def test_orphan_recovery_without_a_checkpoint_still_starts_cold(store):
    """Control: an attempt that died before committing anything has nothing to
    resume onto, and must requeue exactly as it always has — branching from
    base, with no fabricated provenance."""
    orphan = Task.new("killed before its first commit", repo_path="/r")
    await store.create_task(orphan)
    await store.set_status(orphan, TaskStatus.CONTEXT, validate=False)
    await store.create_attempt(orphan.id, 1)          # no commit_sha

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    fresh = await store.get_task(orphan.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert not ((fresh.context or {}).get("resume_from") or {}).get("sha")
    assert _branch_point(fresh.context or {}) == ""


@pytest.mark.asyncio
async def test_orphan_recovery_never_overwrites_an_existing_provenance(store):
    """The requeue must still INHERIT a decision another actor already made:
    stamping over a human's `resume_from` relabels their gated sha as the
    machine's and fails their resume as fabrication (orchestrator
    `_is_own_partial`)."""
    from no_human.blockers import resume_provenance

    orphan = Task.new("resumed by a human, then killed", repo_path="/r")
    await store.create_task(orphan)
    gated = "b" * 40
    await store.merge_context(
        orphan.id, {"resume_from": resume_provenance({"sha": gated}, "human")})
    await store.set_status(orphan, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(orphan.id, 1)
    await store.update_attempt(attempt_id, commit_sha="c" * 40)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    resume_from = ((await store.get_task(orphan.id)).context or {})["resume_from"]
    # `branch: None` DELETES under RFC 7396, so it is absent, not stored.
    assert resume_from == {"sha": gated, "by": "human"}


@pytest.mark.asyncio
async def test_orphan_recovery_does_not_resurrect_a_deliberately_cleared_checkpoint(
        store):
    """A CLEARS path removed the checkpoint; the sweep must not put it back.

    `POST /api/tasks/{id}/retry`, `LeadAgent`'s sub-task retry and
    `_unblock_ready` all write `resume_from: None` — "retry means from base; a
    fresh run must not silently branch from a checkpoint some EARLIER actor
    chose". The candidate sha must therefore come from the attempt that
    actually DIED (the one still `in_progress`, db.py `create_attempt` closes
    every older one), never from "the newest attempt this task ever had": the
    latter reaches straight back past the clear, into a run that already
    failed, and resurrects exactly what the human deleted.
    """
    t = Task.new("failed, then retried from base", repo_path="/r")
    await store.create_task(t)
    dead = await store.create_attempt(t.id, 1)
    await store.update_attempt(dead, commit_sha="a" * 40)

    t.context = await store.merge_context(t.id, {"resume_from": None})
    # run 2 starts (closing run 1's row) and dies before committing anything
    await store.create_attempt(t.id, 2)
    await store.set_status(t, TaskStatus.PLANNING, validate=False)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    fresh = await store.get_task(t.id)
    assert not ((fresh.context or {}).get("resume_from") or {}).get("sha"), (
        "the retry's cleared checkpoint came back from a run that already "
        f"failed; got {(fresh.context or {}).get('resume_from')!r}")


@pytest.mark.asyncio
async def test_a_second_restart_restamps_instead_of_reusing_its_own_stale_sha(store):
    """The measured incident was THREE restarts, not one.

    Bailing on ANY existing `resume_from` cannot tell a human's gate from the
    machine's own earlier stamp, so restart 2 resumed onto restart 1's sha and
    silently discarded everything the run in between committed — the same class
    of loss this branch exists to stop, one restart later. Only `by == "human"`
    is inherited; a machine stamp is re-stamped with the dead attempt's own sha.
    """
    t = Task.new("orphaned twice", repo_path="/r")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(a1, commit_sha="a" * 40)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)

    await Scheduler(store, lambda task=None: None)._recover_orphans()
    assert ((await store.get_task(t.id)).context or {})["resume_from"] == {
        "sha": "a" * 40, "by": "orphan_recovery"}

    # the requeued run gets further, commits MORE work, and dies again
    a2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(a2, commit_sha="b" * 40)
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.TESTING, validate=False)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    resume_from = ((await store.get_task(t.id)).context or {})["resume_from"]
    assert resume_from == {"sha": "b" * 40, "by": "orphan_recovery"}, (
        "restart 2 resumed onto restart 1's stale checkpoint and discarded "
        f"the interim run's commit; got {resume_from!r}")


@pytest.mark.asyncio
async def test_a_machine_stamp_survives_a_restart_that_finds_nothing_newer(store):
    """Control for the re-stamp above: re-stamping is not CLEARING. A requeued
    run that dies before committing leaves the previous machine checkpoint in
    place — dropping it would burn the very work the stamp was protecting."""
    t = Task.new("orphaned twice, second run committed nothing", repo_path="/r")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(a1, commit_sha="a" * 40)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)

    await Scheduler(store, lambda task=None: None)._recover_orphans()
    await store.create_attempt(t.id, 2)               # no commit_sha
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.TESTING, validate=False)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    assert ((await store.get_task(t.id)).context or {})["resume_from"] == {
        "sha": "a" * 40, "by": "orphan_recovery"}


async def test_a_pool_crash_records_a_durable_reason(store):
    """A task killed by a pool-level exception must say WHY, somewhere durable.

    The handler logged to a file the board never reads and emitted `task_error`
    on the LIVE pool stream — gone the moment nobody is watching — then set
    FAILED. So the task ended with no recorded cause anywhere a human looks.
    That matters more since the drawer's "Why it failed" reads the last
    attempt's failure_reason: a crash here can happen with no attempt row at
    all, so the task explains itself nowhere.
    """
    class CrashingOrch:
        async def run_task(self, task):
            raise RuntimeError("boom inside the pool")

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await asyncio.sleep(0.05)

    t = await store.find_task(ids[0])
    assert t.status is TaskStatus.FAILED, "the crash must still terminate the task"

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert crashed, (
        "a pool crash left no durable record — only a log line and a transient "
        f"live event; got kinds: {[e.get('kind') for e in events]}")
    assert "boom inside the pool" in crashed[0]["text"], (
        "the record must carry the actual exception, not just that one happened")
    assert "RuntimeError" in crashed[0]["text"], "and its type"

    # The pool must SURVIVE the crash — the whole reason that except exists.
    assert sched.inflight == set(), "the crashed task was not released"


# --------------------------------------------------------------------------- #
# `nh serve --until-empty`: drain-and-exit (KI-3 / ADOPT-17)                   #
# --------------------------------------------------------------------------- #


class TerminalOrch:
    """Ends each task in a status chosen per id — used to prove the exit
    condition tells a FAILED task apart from an honest park."""

    def __init__(self, store, statuses: dict, default=TaskStatus.AWAITING_APPROVAL):
        self.store = store
        self.statuses = statuses
        self.default = default
        self.started: list[str] = []

    async def run_task(self, task):
        self.started.append(task.id)
        status = self.statuses.get(task.id, self.default)
        await self.store.set_status(task, status, validate=False)
        return SimpleNamespace(status=status, task=task)


async def test_until_empty_exits_immediately_on_an_empty_queue(store):
    """Nothing queued → the drain is already done. The poll interval is 30s
    and the whole call must return well inside it: an empty queue must not
    cost a caller one tick of waiting."""
    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=30.0, until_empty=True), 2.0)

    assert stop.is_set(), "the drain must set the SAME stop event a signal sets"
    assert await sched.queue_is_drained()
    assert await sched.failed_dispatched() == []


async def test_until_empty_drains_the_queue_then_exits(store):
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    ids = await _mk_tasks(store, 5)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert sorted(fake.started) == sorted(ids), "every queued task must run"
    assert sched.inflight == set()
    assert await sched.queue_is_drained()
    assert await sched.failed_dispatched() == []


async def test_until_empty_reports_a_failed_task_but_not_a_park(store):
    """The exit condition is keyed on `task.status == TaskStatus.FAILED`.
    A parked task (here ESCALATED — an honest off-ramp the loop is designed
    to produce) must NOT make the batch look failed, or every real run of a
    hard task exits non-zero and the code says nothing."""
    ids = await _mk_tasks(store, 2)
    orch = TerminalOrch(store, {ids[0]: TaskStatus.FAILED,
                                ids[1]: TaskStatus.ESCALATED})
    sched = Scheduler(store, lambda task=None: orch, max_workers=2)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert await sched.failed_dispatched() == [ids[0]]


async def test_until_empty_ignores_failed_rows_this_process_never_dispatched(store):
    """A real install's DB routinely holds OLD failed rows. The exit code is
    scoped to `self._dispatched` — tasks THIS process started — or every cron
    run of `--until-empty` exits 1 forever and non-zero means nothing. This is
    the test an independent review's mutation (count ALL failed rows) proved
    missing: with that mutation, the stale row below flips the answer."""
    stale = Task.new("failed long ago", repo_path="/tmp/x")
    await store.create_task(stale)
    await store.set_status(stale, TaskStatus.FAILED, validate=False)

    ids = await _mk_tasks(store, 1)
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)
    stop = asyncio.Event()
    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert fake.started == ids, "the fresh task ran"
    assert await sched.failed_dispatched() == [], (
        "a FAILED row this process never dispatched leaked into the exit code")


async def test_until_empty_does_not_stop_while_a_task_is_still_running(store):
    """`_claimable` is empty the moment the only task is dispatched — if the
    exit condition ignored `_inflight` the drain would exit mid-task."""
    hold = asyncio.Event()
    sched = Scheduler(store, lambda task=None: FakeOrch(store, hold=hold),
                      max_workers=1)
    await _mk_tasks(store, 1)
    stop = asyncio.Event()
    run = asyncio.ensure_future(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True))

    await asyncio.sleep(0.2)
    assert not run.done(), "exited while a task was still in flight"
    assert not await sched.queue_is_drained()

    hold.set()
    await asyncio.wait_for(run, 10.0)
    assert await sched.queue_is_drained()


async def test_bare_run_forever_still_runs_forever_on_an_empty_queue(store):
    """The default is untouched: no flag, no exit, empty queue or not."""
    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=1)
    stop = asyncio.Event()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            sched.run_forever(stop=stop, poll_interval=0.01), 0.4)
    assert not stop.is_set()
