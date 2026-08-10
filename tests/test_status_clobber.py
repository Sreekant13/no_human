"""The 2026-08-09 demo incident (R15): a tracker poller's write-back held a
stale Task snapshot across slow network calls, then wrote the whole row back —
reverting a live task's status from awaiting_approval to reviewing. The row
then had a mid-run status with no worker attached, which nothing swept until
the next server restart: invisible for 66 minutes, and recovery re-ran the
whole task and opened a duplicate PR.

Three invariants close the class:
  1. update_task / update_task_columns never move the status column —
     set_status is the ONLY status writer.
  2. Poller write-back persists its markers via merge_context, so a stale
     snapshot cannot erase context keys written concurrently (pr_watch).
  3. The scheduler's orphan sweep also runs per-tick: a task in a mid-run
     status with no worker attached and a stale updated_at is requeued at
     runtime, not just at startup.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from no_human.core.db import Store
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.intake.jira_poll import JiraPoller

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


# --------------------------------------------------------------------------- #
# 1. Store: only set_status moves status                                       #
# --------------------------------------------------------------------------- #


async def test_update_task_never_moves_status(store):
    """A stale handle's status must not overwrite a live row's status; the
    handle is refreshed to the row's truth instead."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    stale = await store.get_task(t.id)          # snapshot at PENDING

    await store.set_status(t, TaskStatus.REVIEWING, validate=False)

    stale.title = "renamed"                     # a legitimate column edit
    result = await store.update_task(stale)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.REVIEWING     # not clobbered to PENDING
    assert fresh.title == "renamed"                 # the edit still landed
    assert result.status is TaskStatus.REVIEWING    # handle refreshed
    assert stale.status is TaskStatus.REVIEWING


async def test_update_task_columns_never_moves_status(store):
    """update_task_columns had the same landmine with no guard at all."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    stale = await store.get_task(t.id)

    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    stale.blocker = {"category": "AMBIGUITY", "question": "?"}
    await store.update_task_columns(stale)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert fresh.blocker["category"] == "AMBIGUITY"


# --------------------------------------------------------------------------- #
# 2. Poller write-back: stale snapshot cannot revert status or erase context   #
# --------------------------------------------------------------------------- #


def _jira_cfg():
    return {"integrations": {"jira": {
        "enabled": True, "site": "https://acme.atlassian.net",
        "project_key": "PROJ", "email": "me@x.com", "jql": "",
        "write_back": True,
    }}}


def _adapter():
    return SimpleNamespace(transition=lambda key, cat: True,
                           comment=lambda key, body: True)


async def _stale_snapshot_poller(store, advance_to: TaskStatus):
    """Build the incident's exact race deterministically: the poller's
    list_tasks snapshot predates a status advance + context merge that land
    before its write-back."""
    task = Task.new("T", source="jira", external_id="PROJ-1",
                    repo_path="/tmp/r")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.REVIEWING, validate=False)
    stale = await store.get_task(task.id)       # the poller's snapshot

    # ...meanwhile the orchestrator advances the task and records its PR.
    live = await store.get_task(task.id)
    await store.set_status(live, advance_to, validate=False)
    await store.merge_context(task.id, {"pr_watch": "https://gh/pr/5"})

    poller = JiraPoller(_adapter(), store, config=_jira_cfg())
    return task, poller, stale


async def test_jira_sync_does_not_revert_a_live_status_advance(store, monkeypatch):
    task, poller, stale = await _stale_snapshot_poller(
        store, TaskStatus.AWAITING_APPROVAL)

    async def _stale_list(status=None):
        return [stale]

    monkeypatch.setattr(store, "list_tasks", _stale_list)
    await poller.sync_statuses()

    monkeypatch.undo()
    fresh = await store.get_task(task.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL, (
        "poller write-back reverted a live status advance (the 2026-08-09 "
        "demo incident: task stranded in reviewing with no worker)")


async def test_jira_sync_does_not_erase_concurrent_context_keys(store, monkeypatch):
    task, poller, stale = await _stale_snapshot_poller(
        store, TaskStatus.AWAITING_APPROVAL)

    async def _stale_list(status=None):
        return [stale]

    monkeypatch.setattr(store, "list_tasks", _stale_list)
    await poller.sync_statuses()

    monkeypatch.undo()
    fresh = await store.get_task(task.id)
    assert (fresh.context or {}).get("pr_watch") == "https://gh/pr/5", (
        "poller write-back erased a context key merged after its snapshot "
        "(pr_watch loss silently kills PR comment-watching)")
    assert "jira" in (fresh.context or {}), "write-back markers must still land"


# --------------------------------------------------------------------------- #
# 3. Scheduler: stranded mid-run rows are swept at runtime, not just startup   #
# --------------------------------------------------------------------------- #


class _NeverRunOrch:
    async def run_task(self, task):  # pragma: no cover - sweep tests never run it
        raise AssertionError("dispatch must not run in these tests")


async def _stranded_task(store, *, age_seconds: float) -> Task:
    t = Task.new("stranded", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=age_seconds)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, t.id))
    await store.db.commit()
    return t


async def test_runtime_sweep_requeues_a_stranded_reviewing_task(store):
    t = await _stranded_task(store, age_seconds=3600)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_recovered" for e in events)


async def test_runtime_sweep_skips_inflight_tasks(store):
    t = await _stranded_task(store, age_seconds=3600)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    sched._inflight.add(t.id)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_runtime_sweep_skips_recently_updated_tasks(store):
    """A task inside the grace window is never requeued — protects any claim
    window where a worker set a mid-run status moments ago."""
    t = await _stranded_task(store, age_seconds=300)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_startup_sweep_ignores_grace_window(store):
    """At startup nothing is legitimately in flight: even a fresh mid-run
    status is an orphan (the pre-existing behavior, preserved)."""
    t = await _stranded_task(store, age_seconds=0)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans()

    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# 4. The cure for review finding B1: the sweep's liveness signal is durable    #
#    (persisted events), and `nh watch` never drives a second orchestrator    #
#    beside a running server.                                                  #
# --------------------------------------------------------------------------- #


async def test_sweep_trusts_recent_events_as_liveness(store):
    """A row whose updated_at is stale but whose EVENTS are fresh is a live
    out-of-process run (nh watch, another driver flushing to the same DB) —
    never requeued. _inflight is per-process; events are not."""
    import time as _time
    t = await _stranded_task(store, age_seconds=3600)
    await store.save_events(t.id, [{
        "source": "agent", "kind": "tool_use", "text": "", "ts": _time.time(),
    }])
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_sweep_requeues_when_events_are_stale_too(store):
    """Old events do not confer liveness — only recent activity does."""
    import time as _time
    t = await _stranded_task(store, age_seconds=3600)
    await store.save_events(t.id, [{
        "source": "agent", "kind": "tool_use", "text": "",
        "ts": _time.time() - 3600,
    }])
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


async def test_tick_invokes_the_runtime_sweep(store):
    """The sweep must actually run from tick() — not only when called
    directly by tests."""
    t = await _stranded_task(store, age_seconds=3600)

    class _Orch:
        async def run_task(self, task):
            await store.set_status(task, TaskStatus.AWAITING_APPROVAL,
                                   validate=False)
            return SimpleNamespace(status=TaskStatus.AWAITING_APPROVAL,
                                   task=task)

    sched = Scheduler(store, lambda task=None: _Orch(), max_workers=1)
    await sched.tick()

    fresh = await store.get_task(t.id)
    assert fresh.status is not TaskStatus.REVIEWING, (
        "tick() never swept the stranded row")
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_recovered" for e in events)


async def test_runtime_sweep_skips_a_plan_correction_wait(store):
    """A PLANNING row holding a human's plan correction is WAITING, not
    stranded — _claimable picks it up; the sweep must not."""
    from no_human.core import plan_gate
    t = Task.new("corrected", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.PLANNING, validate=False)
    t.context = await store.merge_context(t.id, {
        plan_gate.CONTEXT_KEY: {"state": plan_gate.STATE_CORRECTING},
    })
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", t.id))
    await store.db.commit()
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.PLANNING


async def test_unparseable_updated_at_reads_young_never_requeued(store):
    """A corrupt timestamp must fail closed (no requeue), not fail open."""
    assert Scheduler._row_age_s("garbage") == 0.0
    assert Scheduler._row_age_s(None) == 0.0
    t = await _stranded_task(store, age_seconds=0)
    await store.db.execute(
        "UPDATE tasks SET updated_at = 'not-a-stamp' WHERE id = ?", (t.id,))
    await store.db.commit()
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_follow_task_streams_events_and_returns_on_parked(store):
    """_follow_task echoes persisted events and exits when the task leaves
    the active statuses — the read-only half of `nh watch`."""
    from no_human.cli.commands import _follow_task
    t = Task.new("followed", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await store.save_events(t.id, [
        {"source": "orchestrator", "kind": "pr_open", "text": "url", "ts": 1.0},
        {"source": "orchestrator", "kind": "state", "text": "done", "ts": 2.0},
    ])
    echoed = []
    result = await _follow_task(store, t.id[:8], echo=echoed.append)
    assert result is not None and result.status is TaskStatus.AWAITING_APPROVAL
    assert [e["kind"] for e in echoed] == ["pr_open", "state"]


async def test_watch_follows_instead_of_running_when_server_owns(monkeypatch, tmp_path):
    """With a live server owning the pool, `nh watch` must never build its
    own orchestrator (B1: two orchestrators on one checkout; the runtime
    sweep would requeue whichever copy goes silent)."""
    from unittest import mock
    from click.testing import CliRunner
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        db_path = tmp_path / "nh.db"

    followed = []

    async def _fake_follow(store, task_id, **kw):
        followed.append(task_id)
        t = Task.new("x", repo_path="/tmp/r")
        t.status = TaskStatus.AWAITING_APPROVAL
        return t

    ran_tui = []
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=True: (_Cfg(), None)), \
         mock.patch.object(cmd_mod, "_server_owns_worker", lambda cfg: True), \
         mock.patch.object(cmd_mod, "_follow_task", _fake_follow), \
         mock.patch.dict("sys.modules"):
        import no_human.cli.tui as tui_mod
        monkeypatch.setattr(tui_mod, "run_watch",
                            lambda *a, **k: ran_tui.append(a))
        import asyncio as _aio
        result = await _aio.to_thread(
            CliRunner().invoke, cmd_mod.watch, ["deadbeef"])

    assert result.exit_code == 0, result.output
    assert followed == ["deadbeef"]
    assert ran_tui == []


async def test_follow_task_stops_when_the_row_is_deleted(store):
    """A deleted row must end the follow, not spin forever (review N2)."""
    from no_human.cli.commands import _follow_task
    t = Task.new("doomed", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)

    import asyncio

    async def _delete_soon():
        await asyncio.sleep(0.05)
        await store.db.execute("DELETE FROM tasks WHERE id = ?", (t.id,))
        await store.db.commit()

    deleter = asyncio.ensure_future(_delete_soon())
    result = await asyncio.wait_for(
        _follow_task(store, t.id, poll_s=0.02, echo=lambda e: None), timeout=5)
    await deleter
    assert result is None


async def test_tui_run_persists_its_events():
    """The `nh watch` TUI must persist events like its two sibling runners —
    the board reads task_events, and the stranded sweep reads them as
    LIVENESS: a silent TUI run is the double-orchestrator hole (review B2).
    Structural pin on the one production construction site."""
    import inspect
    import no_human.cli.tui as tui
    src = inspect.getsource(tui.WatchApp._run)
    assert "EventPersister" in src and "_persisting(" in src, (
        "WatchApp._run no longer persists its events — the stranded sweep "
        "will read a live TUI run as silence and requeue it")
