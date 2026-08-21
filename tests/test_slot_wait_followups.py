"""Follow-ups from the slot-wait review round: three defects, one test file.

1. `nh task show` printed `slot_wait.waiting_text()`'s own "waiting for a
   worker slot" prefix a SECOND time, hardcoded ahead of it — the phrase
   appeared twice for one wait.
2. `Scheduler._claimable()` claims a PLANNING row carrying
   `plan_gate.correcting` (a plan-approval correction resumes into PLANNING,
   not IMPLEMENTING) — behind a full pool that row gets a `waiting_for_slot`
   event, but `slot_wait.CLAIMABLE_STATUSES` omitted "planning", so `nh
   status` counted it `working` and `nh task show` printed nothing. A
   genuinely DISPATCHED planning run must still not read as waiting — that is
   the negative control below.
3. `tick()` returns before `_note_slot_waits` while the pool is paused for a
   quota/infra cooldown, and nothing closes an already-open wait when the
   pool pauses — so a task kept reading "waiting for a worker slot ... since
   <stale-time>" for as long as the pause lasted. While paused, a recorded
   wait must not read as a LIVE slot wait in either `nh status` or `nh task
   show`.

Review round 2 on the fix for the three defects above found two more,
BLOCKING, defects in the fix itself (both in `nh status`, both added below):

4. F1 — `nh status` and `nh status --json` could disagree about `waiting` for
   the identical DB state at the identical moment: the `--json` branch used
   to compute and return its own bucket counts BEFORE the pause/reachability
   check the human-readable branch applies, so a paused pool read `waiting 0`
   in one and `"waiting": 1` in the other.
5. F2 — `nh status` failed OPEN when the pool was unreachable (server not
   running): `waits_are_live(pause)` returned `True` whenever `pause is
   None`, which is what an unreachable pool ALSO looks like, so a stale wait
   recorded before the server went away was printed as a confident `waiting
   1` instead of being hedged like `nh task show` already does.

Each `test_...` named in `.no_human/repro_tests.json` fails on unmodified
`core/slot_wait.py` / `core/scheduler.py` / `cli/commands.py` and passes once
the fix lands. Uses the existing `_make_runner` / `_seed_task` / `_stub_health`
/ `_status_runner_with_config_width` helpers — no new test infrastructure.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.lanes import status_buckets
from no_human.core.task import TaskStatus
from no_human.core import slot_wait

from tests.test_cli_commands import (
    _make_runner,
    _seed_task,
    _status_runner_with_config_width,
    _stub_health,
)


# --------------------------------------------------------------------------- #
# Helpers — each opens its own fresh Store connection, matching the pattern   #
# `tests/test_cli_commands.py`'s own helpers use.                             #
# --------------------------------------------------------------------------- #

def _seed_event(db_path: Path, task_id: str, event: dict) -> None:
    async def _go():
        async with Store(db_path) as s:
            await s.save_events(task_id, [event])
    asyncio.run(_go())


def _merge_context(db_path: Path, task_id: str, patch: dict) -> None:
    async def _go():
        async with Store(db_path) as s:
            await s.merge_context(task_id, patch)
    asyncio.run(_go())


def _waiting_ids_and_buckets(db_path: Path):
    async def _go():
        async with Store(db_path) as s:
            tasks = await s.list_tasks()
            waiting_ids = await s.tasks_waiting_for_slot()
            return waiting_ids, status_buckets(tasks, waiting_ids)
    return asyncio.run(_go())


def _correcting_patch() -> dict:
    """A realistic `plan_gate.reply_patch(..., approve=False, ...)` shape —
    keyed on the STATE, same as `plan_gate.correcting` reads."""
    return {"plan_approval": {
        "state": "correcting",
        "correction": "fix the config path",
        "corrected_at": "2026-08-20T00:00:00+00:00",
        "approved_at": None,
    }}


# --------------------------------------------------------------------------- #
# AC1 — the doubled label                                                     #
# --------------------------------------------------------------------------- #

def test_show_prints_the_waiting_phrase_exactly_once(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, task_id="a" * 32)
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": slot_wait.KIND,
        "text": slot_wait.waiting_text(1, 1, "2026-08-20T12:00:00+00:00"),
        "ts": time.time(),
    })

    runner = _make_runner(db, monkeypatch)
    out = runner.invoke(cli, ["task", "show", task_id]).output
    assert out.count("waiting for a worker slot") == 1, out
    assert "1/1 busy" in out, out
    assert "since 2026-08-20T12:00:00+00:00" in out, out


# --------------------------------------------------------------------------- #
# AC2 — a correcting PLANNING task behind a full pool                         #
# --------------------------------------------------------------------------- #

def test_a_correcting_planning_task_behind_a_full_pool_is_counted_and_shown(
        tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.PLANNING, task_id="b" * 32)
    _merge_context(db, task_id, _correcting_patch())
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": slot_wait.KIND,
        "text": slot_wait.waiting_text(1, 1, "2026-08-20T12:00:00+00:00"),
        "ts": time.time(),
    })

    waiting_ids, buckets = _waiting_ids_and_buckets(db)
    assert waiting_ids == {task_id}, waiting_ids
    assert buckets["waiting"] == 1, buckets
    assert buckets["working"] == 0, buckets

    runner = _make_runner(db, monkeypatch)
    out = runner.invoke(cli, ["task", "show", task_id]).output
    assert out.count("waiting for a worker slot") == 1, out


def test_a_dispatched_planning_run_is_not_waiting(tmp_path, monkeypatch):
    """Negative control: once a run-sourced event lands after the wait (a
    worker actually picked the row up), it must stop reading as waiting —
    widening `CLAIMABLE_STATUSES` to include "planning" must not make a live
    planning run look stuck."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.PLANNING, task_id="c" * 32)
    _merge_context(db, task_id, _correcting_patch())
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": slot_wait.KIND,
        "text": slot_wait.waiting_text(1, 1, "2026-08-20T12:00:00+00:00"),
        "ts": 1000.0,
    })
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": "repo_config",
        "text": "applying the repo's .no_human.yml", "ts": 1001.0,
    })

    waiting_ids, buckets = _waiting_ids_and_buckets(db)
    assert task_id not in waiting_ids, waiting_ids
    assert buckets["working"] == 1, buckets
    assert buckets["waiting"] == 0, buckets

    runner = _make_runner(db, monkeypatch)
    out = runner.invoke(cli, ["task", "show", task_id]).output
    assert "waiting for a worker slot" not in out, out


# --------------------------------------------------------------------------- #
# AC3 — a paused pool must not read a recorded wait as live                   #
# --------------------------------------------------------------------------- #

def test_a_wait_recorded_before_a_cooldown_is_not_a_live_slot_wait_while_paused(
        tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, task_id="d" * 32)
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": slot_wait.KIND,
        "text": slot_wait.waiting_text(1, 1, "2026-08-20T12:00:00+00:00"),
        "ts": time.time(),
    })

    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {
        "max_workers": 4, "workers_busy": 0, "queue_depth": 1,
        "paused": True, "paused_reason": "quota",
        "paused_until": "2026-08-20T17:20:00+00:00",
        "paused_profile": "personal2",
    })

    status_out = " ".join(runner.invoke(cli, ["status"]).output.split())
    assert "waiting 0" in status_out, status_out
    assert "quota cooldown" in status_out, status_out

    show_out = runner.invoke(cli, ["task", "show", task_id]).output
    assert "pool paused" in show_out, show_out
    assert "quota cooldown" in show_out, show_out
    assert "waiting for a worker slot" not in show_out, show_out


def test_an_unpaused_pool_still_counts_and_shows_the_same_wait(
        tmp_path, monkeypatch):
    """Negative control for AC3: with no pause reported, the very same
    recorded wait is still counted `waiting` and still printed once — pausing
    is what changes the read, not merely having a live server to ask."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, task_id="e" * 32)
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": slot_wait.KIND,
        "text": slot_wait.waiting_text(1, 1, "2026-08-20T12:00:00+00:00"),
        "ts": time.time(),
    })

    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {
        "max_workers": 4, "workers_busy": 4, "queue_depth": 1,
    })

    status_out = " ".join(runner.invoke(cli, ["status"]).output.split())
    assert "waiting 1" in status_out, status_out

    show_out = runner.invoke(cli, ["task", "show", task_id]).output
    assert show_out.count("waiting for a worker slot") == 1, show_out


# --------------------------------------------------------------------------- #
# F1 — `nh status` and `nh status --json` must agree, at the same moment      #
# --------------------------------------------------------------------------- #

def test_status_json_and_human_readable_agree_on_waiting_while_paused(
        tmp_path, monkeypatch):
    """Review finding F1: the `--json` branch used to return its own bucket
    counts computed BEFORE the pause check the human-readable branch applies,
    so the two could disagree about `waiting` for the identical DB state.
    Both must now say the recorded wait is not live while the pool is paused."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, task_id="f" * 32)
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": slot_wait.KIND,
        "text": slot_wait.waiting_text(1, 1, "2026-08-20T12:00:00+00:00"),
        "ts": time.time(),
    })

    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {
        "max_workers": 4, "workers_busy": 0, "queue_depth": 1,
        "paused": True, "paused_reason": "quota",
        "paused_until": "2026-08-20T17:20:00+00:00",
        "paused_profile": "personal2",
    })

    json_out = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert json_out["waiting"] == 0, json_out

    text_out = " ".join(runner.invoke(cli, ["status"]).output.split())
    assert "waiting 0" in text_out, text_out


# --------------------------------------------------------------------------- #
# F2 — an unreachable pool must not confidently assert a live wait           #
# --------------------------------------------------------------------------- #

def test_status_does_not_confidently_count_waiting_when_pool_is_unreachable(
        tmp_path, monkeypatch):
    """Review finding F2: `waits_are_live(pause)` returned True whenever
    `pause is None`, which is indistinguishable from "genuinely not paused" —
    so a stale wait recorded before the server went away (or was never
    started) was printed as a confident `waiting 1`, unlike `nh task show`,
    which already hedges an unreachable pool with `STALE_POOL_NOTE`. Both
    `--json` and the human-readable line must not count it while the pool
    cannot be asked at all."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, task_id="g" * 32)
    _seed_event(db, task_id, {
        "source": "orchestrator", "kind": slot_wait.KIND,
        "text": slot_wait.waiting_text(1, 1, "2026-08-20T12:00:00+00:00"),
        "ts": time.time(),
    })

    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, None)  # simulates "connection refused"

    json_out = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert json_out["waiting"] == 0, json_out

    text_out = " ".join(runner.invoke(cli, ["status"]).output.split())
    assert "waiting 0" in text_out, text_out
