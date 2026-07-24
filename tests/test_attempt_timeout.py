"""B20: a coder turn has NO wall-clock bound (only max_turns), so a hung SDK
subprocess (auth/quota/network stall at 0% CPU) would wedge the whole attempt
forever — the exact wedge that killed a dogfood shadow run. The advisory/judge
calls were already guarded (_bounded_run); this proves the CODER turn now is
too: a hang becomes an honest FAILED attempt, never a forever-wedge.
"""
from __future__ import annotations

import asyncio
import time

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo, store  # noqa: F401


class _HangBackend:
    """A coder turn that never returns in time (a hung SDK subprocess)."""

    async def run(self, *a, **k):
        await asyncio.sleep(30)  # far longer than the patched attempt ceiling


async def test_hung_coder_turn_fails_fast_instead_of_wedging(
    bare_repo, tmp_path, store  # noqa: F811
):
    cfg = _config(tmp_path)
    # Tiny ceiling so the hang trips it immediately; default is 3600s.
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    events: list = []
    orch = Orchestrator(store, cfg.data, _HangBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    t0 = time.monotonic()
    outcome = await orch.run_task(t)
    elapsed = time.monotonic() - t0

    # Proves the timeout FIRED: the backend hangs 30s, but the bounded loop of
    # short-ceiling attempts completes in well under that — never the 30s wedge.
    assert elapsed < 15, f"attempt did not time out fast (took {elapsed:.1f}s)"
    # A terminal, honest non-hang state — never AWAITING_APPROVAL (nothing
    # ran). BLOCKED joined the set with the timeout-streak escalation
    # (SCRUM-4): two consecutive timeouts now park as TRANSIENT_INFRA.
    assert outcome.status in (
        TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.BLOCKED), outcome
    assert outcome.pr_url is None
    # The attempt row must SAY it timed out (observability, not a silent fail).
    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt recorded"
    assert attempts[-1]["status"] == "failed"
    assert "timed out" in (attempts[-1]["failure_reason"] or "").lower(), attempts[-1]
    # And an honest timeout event was emitted.
    assert any(e.get("error_class") == "timeout" for e in events), \
        [e for e in events if e.get("kind") == "agent_error"]


async def test_normal_run_is_not_falsely_timed_out(
    bare_repo, tmp_path, store  # noqa: F811
):
    """The wall-clock bound must NOT fire for a normal fast attempt."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 30  # ample

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)
    # A healthy run reaches an approvable PR — no false timeout, no failure.
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome
    attempts = await store.list_attempts(t.id)
    assert not any(
        "timed out" in (a["failure_reason"] or "").lower() for a in attempts
    ), attempts


class _AlwaysHangBackend:
    """Every coder turn hangs — a persistently wedged backend."""

    calls = 0

    async def run(self, *a, **k):
        type(self).calls += 1
        await asyncio.sleep(30)


async def test_repeated_timeouts_escalate_after_two_not_max_attempts(
    bare_repo, tmp_path, store  # noqa: F811
):
    """SCRUM-4 / B20 follow-up: a backend that hangs on EVERY attempt must trip
    the unproductive streak after 2 consecutive timeouts and escalate with a
    TIMEOUT-specific blocker — not burn all max_attempts, and never surface the
    misleading zero-diff 'is this already implemented?' question."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 3
    _AlwaysHangBackend.calls = 0
    orch = Orchestrator(store, cfg.data, _AlwaysHangBackend(), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # TRANSIENT_INFRA parks (BLOCKED, auto-retry) — a transient backend wedge
    # self-heals; a low-confidence triage may escalate instead. Either way the
    # loop must STOP after two timeouts, never burn the third attempt.
    assert outcome.status in (TaskStatus.BLOCKED, TaskStatus.ESCALATED), outcome
    assert _AlwaysHangBackend.calls == 2, (
        f"expected escalation after 2 timeouts, backend ran {_AlwaysHangBackend.calls}x")
    fresh = await store.get_task(t.id)
    b = fresh.blocker or {}
    blob = ((b.get("question") or "") + (b.get("root_cause_hypothesis") or "")).lower()
    assert "hung" in blob or "timed out" in blob, b
    assert "already implemented" not in blob, (
        "timeout streak must not surface the zero-diff escalation")
    assert b.get("category") == "TRANSIENT_INFRA", b


async def test_timeout_then_productive_attempt_resets_the_streak(
    bare_repo, tmp_path, store  # noqa: F811
):
    """One timeout followed by a productive attempt must reset the streak —
    no false timeout escalation, exactly like the other unproductive reasons."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 1.5

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    productive = FakeBackend(mutate)

    class _HangOnce:
        calls = 0

        async def run(self, *a, **k):
            type(self).calls += 1
            if type(self).calls == 1:
                await asyncio.sleep(30)
            return await productive.run(*a, **k)

    _HangOnce.calls = 0
    orch = Orchestrator(store, cfg.data, _HangOnce(), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # The healthy second attempt proceeds — never a timeout-streak escalation.
    fresh = await store.get_task(t.id)
    assert (fresh.blocker or {}).get("category") != "TRANSIENT_INFRA", fresh.blocker
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome
