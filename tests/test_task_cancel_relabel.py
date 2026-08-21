"""`nh task cancel` on an already-FAILED task refused outright — a hand-landed
pending task with no honest terminal (855f1263's sibling defect): a real
failure that a human wants to re-label "cancelled" (e.g. because it was
actually a deliberate stop) had no route to record that assertion. The
existing DONE guard's message and behaviour stay untouched.

Fix (in `no_human.cli.commands.task_cancel`): a FAILED task with no existing
`context["cancel_reason"]` is re-labelled — `cancel_reason` is set, and a
`human_cancel` event is recorded via the shared `human_event` shape, exactly
the way `nh approve --landed` records an assertion. A FAILED task that
ALREADY carries a `cancel_reason` is a no-op: it reports the existing reason
and writes nothing new (idempotent "first call sets, later inspection
reports").

Idiom copied from `tests/test_approve.py`: sync tests, each owning its own
`asyncio.run`, `_bootstrap` patched via `mock.patch.object`, real `CliRunner`
invocations — `task_cancel` is a subcommand of the `task` click group
(`@task.command("cancel")`), so invocation goes through the `task` group
object itself: `CliRunner().invoke(task, ["cancel", tid, "--reason", ...])`.
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock

from click.testing import CliRunner

from no_human.cli.commands import task
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

# --------------------------------------------------------------------------- #
# CLI plumbing (copied from tests/test_approve.py)                           #
# --------------------------------------------------------------------------- #

class _Cfg:
    db_path = None
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _cfg(db_path):
    c = _Cfg()
    c.db_path = db_path
    return c


def _invoke(cmd, db, args):
    import no_human.cli.commands as cmd_mod
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_cfg(db), None)):
        return CliRunner().invoke(cmd, args)


def _task_state(db, task_id):
    async def _go():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            events = await store.list_events(task_id)
            return t, events
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Task fixtures                                                               #
# --------------------------------------------------------------------------- #

async def _seed(db, *, status, cancel_reason=None):
    async with Store(db) as store:
        t = Task.new("a task", repo_path="/tmp/does-not-matter")
        t.status = status
        t.context = {}
        if cancel_reason is not None:
            t.context["cancel_reason"] = cancel_reason
        # create_task inserts the row exactly as given (via Task.to_row()),
        # so setting status/context on the Task object before creating it
        # is sufficient — no separate transition call needed.
        await store.create_task(t)
        return t.id


# --------------------------------------------------------------------------- #
# AC2 — the FAILED-task relabel / no-op behaviour                            #
# --------------------------------------------------------------------------- #

def test_cancel_labels_a_failed_task_with_no_reason(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.FAILED))

    result = _invoke(task, db,
                      ["cancel", tid, "--reason",
                       "hand-landed by the supervisor"])

    assert result.exit_code == 0, result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.FAILED
    assert (t.context or {}).get("cancel_reason") == \
        "hand-landed by the supervisor"

    cancel_events = [e for e in events if e.get("kind") == "human_cancel"]
    assert len(cancel_events) == 1, events
    ev = cancel_events[0]
    assert ev["reason"] == "hand-landed by the supervisor"
    assert ev["prior_status"] == "failed"
    assert ev["prior_cancel_reason"] is None


def test_cancel_is_a_no_op_when_a_reason_already_exists(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(
        _seed(db, status=TaskStatus.FAILED, cancel_reason="first"))

    result = _invoke(task, db, ["cancel", tid, "--reason", "second"])

    assert result.exit_code == 0, result.output
    assert "already cancelled" in result.output
    assert "first" in result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.FAILED
    assert (t.context or {}).get("cancel_reason") == "first"
    assert not any(e.get("kind") == "human_cancel" for e in events), events


def test_cancel_on_a_done_task_is_still_refused_without_writes(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.DONE))

    result = _invoke(task, db, ["cancel", tid, "--reason", "irrelevant"])

    assert result.exit_code == 0, result.output
    assert "already done" in result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.DONE
    assert (t.context or {}).get("cancel_reason") is None
    assert not any(e.get("kind") == "human_cancel" for e in events), events


def test_cancel_of_a_pending_task_is_unchanged(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_seed(db, status=TaskStatus.PENDING))

    result = _invoke(task, db, ["cancel", tid, "--reason", "no longer needed"])

    assert result.exit_code == 0, result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.FAILED
    assert (t.context or {}).get("cancel_reason") == "no longer needed"

    cancel_events = [e for e in events if e.get("kind") == "human_cancel"]
    assert len(cancel_events) == 1, events
    assert cancel_events[0]["prior_status"] == "pending"
