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
    # A terminal, honest non-hang state — never AWAITING_APPROVAL (nothing ran).
    assert outcome.status in (TaskStatus.FAILED, TaskStatus.ESCALATED), outcome
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
