"""C1: a hung SDK subprocess (Stream-closed transport) once wedged the whole
bench ~12min at 0% CPU because `await be.run()` never returns. These tests prove
every grill/intake/judge LLM call now has a hard wall-clock ceiling: a hang
becomes a TimeoutError the callers treat as advisory / fail-closed, never a
forever-wedge."""
import asyncio
import time
from types import SimpleNamespace

from no_human.intake import evaluator
from no_human.eval import judge


class _HangBackend:
    async def run(self, *a, **k):
        await asyncio.sleep(30)                    # longer than the patched ceiling
        return SimpleNamespace(final_text="UNREACHABLE", is_error=False)


def test_evaluator_bounded_run_times_out_to_sentinel(monkeypatch):
    monkeypatch.setattr(evaluator, "_LLM_TIMEOUT_S", 0.05)
    r = asyncio.run(evaluator._bounded_run(_HangBackend(), "prompt"))
    # sentinel, not a hang and not the backend's UNREACHABLE result
    assert r.final_text == "" and r.is_error is True


def test_judge_times_out_fail_closed(monkeypatch):
    monkeypatch.setattr(judge, "_JUDGE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(judge, "_RETRY_BACKOFF_S", 0.0)
    gj = judge.GoalJudge(backend=_HangBackend())
    t0 = time.monotonic()
    v = asyncio.run(gj.judge(request="do x", criteria=[], agent_diff="",
                             outcome_status="done"))
    elapsed = time.monotonic() - t0
    assert v.satisfied is False                    # fail-closed on timeout, no hang
    # proves the TIMEOUT fired: 2 attempts x 0.05s ceiling (backoff patched to 0)
    # completes in well under a second, NOT the backend's 30s hang.
    assert elapsed < 5.0, f"judge did not time out fast (took {elapsed:.1f}s)"
