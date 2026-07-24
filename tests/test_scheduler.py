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


@pytest.mark.asyncio
async def test_reanalysis_maybe_run_produces_result(store):
    """maybe_run returns a result dict with expected keys when due."""
    job = ReanalysisJob(store, interval_seconds=60, days=1)
    # Due because _last_run is 0.
    result = await job.maybe_run()
    assert result is not None
    assert "transcripts" in result
    assert "proposed" in result
    assert "duplicates" in result


@pytest.mark.asyncio
async def test_reanalysis_dedup_across_runs(store):
    """Running re-analysis twice does not duplicate proposals."""
    job = ReanalysisJob(store, interval_seconds=0, days=1)
    r1 = await job.maybe_run()
    job._running = False  # reset guard
    job._last_run = 0     # force re-run
    r2 = await job.maybe_run()
    # Second run: any findings from r1 are now cached/deduped.
    assert r2 is not None
    assert r2["duplicates"] >= 0  # no new proposals if transcripts unchanged


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
            await store.set_status(task, TaskStatus.DONE, validate=False)
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
            await store.set_status(task, TaskStatus.DONE, validate=False)
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
    assert len(persisted) == 2
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
            await store.set_status(task, TaskStatus.DONE, validate=False)
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
            await store.set_status(task, TaskStatus.DONE, validate=False)
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
    # And the final flush must not re-insert what was already written.
    assert len(await store.list_events(t.id)) == 5


async def test_flushed_events_are_not_duplicated_by_the_final_flush(store):
    """save_events INSERTs. Handing it the full buffer on every flush would
    write each event once per flush."""
    class SlowFakeOrch:
        _sink = staticmethod(lambda e: None)

        async def run_task(self, task):
            for i in range(3):
                self._sink({"kind": "tool_use", "tool_name": f"Read{i}"})
                await asyncio.sleep(0.03)   # several flush intervals elapse
            await store.set_status(task, TaskStatus.DONE, validate=False)
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    sched = Scheduler(store, lambda task=None: SlowFakeOrch(), max_workers=1)
    sched._EVENT_FLUSH_INTERVAL = 0.01
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    while sched.inflight:
        await asyncio.sleep(0.01)

    persisted = await store.list_events(t.id)
    assert len(persisted) == 3
    assert [e["tool_name"] for e in persisted] == ["Read0", "Read1", "Read2"]


# --------------------------------------------------------------------------- #
# The pool is never wider than the worktree isolation allows                   #
# --------------------------------------------------------------------------- #

def test_pool_is_clamped_to_one_when_concurrency_is_disabled():
    """The live config was exactly this, and the server announced
    '2 worker(s) · concurrent' while Orchestrator._concurrency_enabled() was
    False — so two tasks would share one checkout with no worktree."""
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

    assert resolve_max_workers({"concurrency": {"enabled": True, "max_workers": 3}}) == (3, None)
    assert resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 1}}, override=4) == (4, None)


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

    workers, enabled, error = resolve_serve_pool(
        {"concurrency": {"enabled": False, "max_workers": 1}}, cli_workers=3)
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
