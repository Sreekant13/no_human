"""A hung spec must not stop the whole bench run.

The product already had this watchdog — `blockers.stuck_active_minutes`, added
for the 2026-07-11 reviewer hang — but it lives in `blockers/wake.py`, which
only the SERVER runs. `nh bench run` drives the orchestrator directly, so
nothing was watching. Observed live 2026-07-23: a spec sat nine minutes at 0%
CPU with no children, no sockets and no file activity, blocked in `__wait4`,
and would have sat there forever. The whole run was dead and said nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from no_human.eval import northstar as ns
from no_human.eval.northstar import NorthStarRunner


def _runner(stuck_minutes):
    r = NorthStarRunner.__new__(NorthStarRunner)   # no collaborators needed
    r.config = {"blockers": {"stuck_active_minutes": stuck_minutes}}
    return r


class _Orch:
    """Stands in for the Orchestrator. `beats` events, then hangs forever."""

    def __init__(self, beats, gap, sink):
        self.beats, self.gap, self.sink = beats, gap, sink

    async def run_task(self, task):
        for _ in range(self.beats):
            await asyncio.sleep(self.gap)
            self.sink()                    # an event: proof of life
        # Bounded on purpose: 30s dwarfs the 0.3s threshold under test, but
        # a MISSING watchdog then FAILS the suite instead of hanging it.
        await asyncio.sleep(30)            # then hang, like the observed wedge


@pytest.fixture(autouse=True)
def _fast_watchdog(monkeypatch):
    # The real cadence is 30s; nothing about the logic depends on the value.
    monkeypatch.setattr(ns, "_WATCHDOG_POLL_S", 0.02)
    monkeypatch.setattr(ns, "_WATCHDOG_UNWIND_S", 0.5)


@pytest.mark.asyncio
async def test_a_silent_spec_is_stopped_instead_of_hanging_the_run():
    """THE defect. Silence past the threshold must raise, so the CLI records a
    crashed spec and the run CONTINUES to the next one."""
    import time

    last = [time.monotonic()]
    r = _runner(0.005)                     # 0.3s of silence is "stuck"
    orch = _Orch(beats=0, gap=0, sink=lambda: None)

    # asyncio.wait_for so a MISSING watchdog fails fast instead of hanging.
    # Without it this test hung for the fake orchestrator's full 3600s sleep
    # when I mutated the guard away — a test that hangs instead of failing is
    # useless in CI, and it wedged my own mutation run.
    with pytest.raises(NorthStarRunner.SpecStalled) as exc:
        await asyncio.wait_for(
            r._run_with_watchdog(orch, object(), last), timeout=10)

    msg = str(exc.value)
    assert "no event" in msg and "hung" in msg, msg
    assert "limit" in msg, "must name the threshold it applied"


@pytest.mark.asyncio
async def test_a_SLOW_but_ALIVE_spec_is_never_killed():
    """The dangerous direction, and the reason this mirrors wake.py's semantic
    (no EVENT for N minutes) rather than total wall clock.

    A spec that runs far longer than the threshold while still emitting must
    finish untouched — killing it would turn a slow success into a recorded
    capability failure, which is exactly the class #207 existed to remove.
    """
    import time

    last = [time.monotonic()]
    r = _runner(0.005)                     # 0.3s of SILENCE is stuck...

    done = []

    class _Alive:
        async def run_task(self, task):
            # Runs ~10x the threshold, but keeps emitting throughout.
            for _ in range(20):
                await asyncio.sleep(0.15)
                last[0] = time.monotonic()  # an event
            done.append(True)
            return "finished"

    out = await r._run_with_watchdog(_Alive(), object(), last)

    assert out == "finished"
    assert done, "the slow-but-alive spec must run to completion"


@pytest.mark.asyncio
async def test_zero_disables_the_watchdog_exactly_as_in_wake_py():
    """wake.py treats <= 0 as "disabled"; the bench must not invent a
    different convention for the same key."""
    import time

    r = _runner(0)

    class _SlowAndSilent:
        """Silent for far longer than any threshold. With the disable branch
        removed, limit_s becomes 0 and the FIRST poll would stall it — so this
        returning proves the branch is doing the work. The previous fixture
        returned instantly and passed either way."""
        async def run_task(self, task):
            await asyncio.sleep(0.6)
            return "ok"

    out = await asyncio.wait_for(
        r._run_with_watchdog(_SlowAndSilent(), object(),
                             [time.monotonic()]),
        timeout=10)
    assert out == "ok"


@pytest.mark.asyncio
async def test_the_threshold_comes_from_the_SHARED_config_key():
    """Reusing `blockers.stuck_active_minutes` is the point — a second knob
    would drift from the one the server enforces."""
    import time

    r = _runner(0.004)
    r.config["blockers"]["stuck_active_minutes"] = 0.004
    with pytest.raises(NorthStarRunner.SpecStalled) as exc:
        await asyncio.wait_for(
            r._run_with_watchdog(_Orch(beats=0, gap=0, sink=lambda: None),
                                 object(), [time.monotonic()]),
            timeout=10)
    # 0.004 min rounds to 0 in the message, but the limit must be reported.
    assert "limit" in str(exc.value)


def test_the_SINK_is_what_refreshes_the_watchdog_clock():
    """The wiring, on the production path.

    Every other test in this file sets the clock itself, so the one line in
    `_make_sink` that refreshes it could be deleted with all of them green.
    Deleting it makes the watchdog fire on a spec that is long but ALIVE —
    a slow success recorded as a capability failure, the false-kill direction.
    """
    import time

    recorded, forwarded = [], []

    class _Persister:
        def record(self, e): recorded.append(e)

    sink, clock = ns._make_sink(_Persister(), forwarded.append, "task-1")
    before = clock[0]
    time.sleep(0.02)

    sink({"kind": "tool_use"})

    assert clock[0] > before, "an event must refresh the watchdog clock"
    # and it still does its original jobs
    assert recorded and recorded[0]["task_id"] == "task-1"
    assert forwarded and forwarded[0] is recorded[0]
    assert "ts" in recorded[0]
