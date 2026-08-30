"""Nothing closed the codex subprocess TRANSPORT, so a bounded teardown
leaked it and `BaseSubprocessTransport.__del__` ran against a closed loop.

MEASURED 2026-08-26, twice, in full-suite gates on branches that touched NO
codex code:

    FAILED tests/test_codex_oversized_jsonl_line.py::...
    PytestUnraisableExceptionWarning: Exception ignored in:
      <function BaseSubprocessTransport.__del__>
    RuntimeError: Event loop is closed

`asyncio.create_subprocess_exec` creates the child; the transport is only
torn down by asyncio itself when `proc.wait()` completes
(`BaseSubprocessTransport._call_connection_lost`, reached from the child
watcher callback `wait()` is keyed to). `_kill_and_reap`'s teardown bounds
that wait with `asyncio.wait_for(proc.wait(), _TEARDOWN_WAIT)`
(`src/no_human/agent/codex_backend.py`); on the TIMEOUT arm `wait()` by
definition never completes, so the transport is guaranteed to survive past
the function returning, alive until GC finalises it — potentially long
after the event loop that owned it has been closed by whatever test ran
next. `__del__` then calls `close()` -> `loop.call_soon()` on a closed loop,
raising `RuntimeError: Event loop is closed` from inside a finalizer, which
Python routes to `sys.unraisablehook` — the hook pytest wraps into
`PytestUnraisableExceptionWarning`, attributed to whatever test happened to
be running when GC fired rather than to the codex branch that caused it.
Reported load: 6 concurrent pytest processes at load 11.18 on 18 CPUs (more
concurrency -> more GC pressure -> more visible).

NOT A REGRESSION FROM PR #785. #785 replaced an unbounded `await
proc.wait()` with the bounded `wait_for` above — a strict improvement, since
the unbounded version wedged a pool worker for up to `attempt_timeout_s` on
exactly the same paused-transport condition described in
`tests/test_codex_teardown_does_not_hang.py`. On main (pre-#785) the
unbounded wait never returns either, so main leaks the transport too, AND
hangs the worker. #785 turned "hang forever + leak" into "return in 5s +
leak"; this file fixes the remaining half — the leak — without touching the
bound, the `-9` fallback, or any of #785's own behaviour.

NOT A DUPLICATE of ticket b7090c45 (the HANG, fixed by #785) or ec24f443
(making the timeout path *observable* downstream, an explicit non-goal
here — see `codex_backend._close_subprocess_transport`'s docstring).

THE FIX (`codex_backend._close_subprocess_transport`): explicitly close
`proc._transport` at the tail of `_kill_and_reap`, after the bounded wait,
on both its timeout and success arms (a single call site). This is NOT a
no-op on the success arm either: asyncio's own auto-close
(`SubprocessStreamProtocol._maybe_close_transport`) only fires once BOTH
`process_exited` has run AND every piped fd has reported
`pipe_connection_lost` (i.e. been read to EOF); `stream()` returns as soon
as it sees `turn.completed` without draining stdout/stderr to EOF, so that
second condition is never met and asyncio never auto-closes the transport
on the normal path either — MEASURED by deleting the call and observing
`test_a_teardown_that_completes_in_the_bound_is_unchanged` below go red
alongside the timeout-arm tests, not just AC1/AC2 as first assumed.
`close()` remains idempotent, so one call site is still easier to keep
correct than two.

THE PRIVATE ATTRIBUTE IS THE ONLY ROUTE. `asyncio.subprocess.Process`
exposes no public `close()`. The transport lives at `proc._transport` —
verified live against this repo's interpreter, CPython 3.12.13
(`Lib/asyncio/subprocess.py`, `Process.__init__`: `self._transport =
transport`), and documented in CPython 3.12/3.13 as unchanged. That this
attribute keeps existing is pinned here, not left to a silent `getattr`
fallback in the fix itself — see
`test_process_still_exposes_the_private_transport_attribute`, which fails
loudly (not silently) if a future CPython renames or removes it.

Every fake "codex" CLI below is a REAL subprocess, sharing
`_write_fake_cli`/`_stub_cli`/`FAKE_ENV` with
`tests/test_codex_oversized_jsonl_line.py` (imported, never edited) — the
same real `create_subprocess_exec` + real pipes call site the incident
happened on. `asyncio.wait_for` itself is never mocked; only `proc.wait` is
replaced with a coroutine that never resolves, exactly as
`tests/test_codex_teardown_does_not_hang.py` already does, because a
SIGKILLed child otherwise gets reaped by the loop's own child watcher
before the timeout branch can be forced deterministically.
"""

from __future__ import annotations

import asyncio
import gc
import sys

import pytest

import no_human.agent.codex_backend as cx

from .test_codex_oversized_jsonl_line import (  # noqa: F401
    FAKE_ENV, _stub_cli, _write_fake_cli,
)

# Small enough that a modest write pauses the reader, mirroring
# tests/test_codex_teardown_does_not_hang.py's `_TINY_STDOUT_LIMIT` idiom. A
# test-local literal, not derived from `cx._STDOUT_LIMIT`, so the test stays
# meaningful if that constant changes.
_TINY_STDOUT_LIMIT = 4096


def _cli_that_lingers_after_thread_started(tmp_path):
    """A real child that emits one small event, then floods far past the
    tiny reader limit and never exits on its own — `stream()` must kill it,
    and the reap must be forced to time out by stubbing `proc.wait`."""
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'sys.stdout.write("x" * 2_000_000)\n'
        'sys.stdout.flush()\n'
        'import time; time.sleep(30)\n'
    )
    return _write_fake_cli(tmp_path, body, name="fake-codex-lingers")


def _cli_that_completes_cleanly(tmp_path):
    """A real child that finishes a normal turn and exits 0 on its own —
    the teardown's success arm, no kill/timeout involved."""
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'emit({"type": "item.completed", "item": {"id": "i0", '
        '"type": "agent_message", "text": "all done"}})\n'
        'emit({"type": "turn.completed", "usage": {"input_tokens": 100, '
        '"cached_input_tokens": 0, "output_tokens": 10}})\n'
    )
    return _write_fake_cli(tmp_path, body, name="fake-codex-clean-exit")


def _patch_create_subprocess_with_a_hanging_wait(monkeypatch, captured):
    """Shared technique with `tests/test_codex_teardown_does_not_hang.py`'s
    `_create_with_a_hanging_wait`: spawn the REAL child via the REAL
    `create_subprocess_exec`, only replace `proc.wait` with a coroutine that
    never resolves, so the `_TEARDOWN_WAIT` timeout branch in
    `_kill_and_reap` is reached deterministically rather than racing the
    loop's own child watcher. `captured` collects every `proc` created so
    the test can inspect its transport after the run completes."""
    real_create = asyncio.create_subprocess_exec

    async def _create_with_a_hanging_wait(*a, **k):
        proc = await real_create(*a, **k)
        captured.append(proc)

        async def _never():
            await asyncio.Event().wait()

        proc.wait = _never
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _create_with_a_hanging_wait)
    monkeypatch.setattr(cx.asyncio, "create_subprocess_exec",
                        _create_with_a_hanging_wait, raising=False)


class _UnraisableCapture:
    """Installs a `sys.unraisablehook` that records instead of printing, so
    a test can assert on exactly what a CPython finalizer raised — the same
    channel pytest's own unraisable-exception plugin listens on to produce
    `PytestUnraisableExceptionWarning`."""

    def __init__(self):
        self.caught: list[object] = []
        self._old_hook = None

    def __enter__(self):
        self._old_hook = sys.unraisablehook
        sys.unraisablehook = self.caught.append
        return self

    def __exit__(self, *exc_info):
        sys.unraisablehook = self._old_hook


# ---------------------------------------------------------------------------
# AC1 — the transport is explicitly closed on the timeout path.
# ---------------------------------------------------------------------------


async def test_a_timed_out_teardown_leaves_no_open_transport(tmp_path, monkeypatch):
    """RED if the `_close_subprocess_transport(proc)` call is deleted from
    `_kill_and_reap`: with only the bound (no explicit close), `proc.wait`
    never resolving means asyncio never tears the transport down, so
    `transport.is_closing()` stays False after the whole run() returns."""
    monkeypatch.setattr(cx, "_STDOUT_LIMIT", _TINY_STDOUT_LIMIT)
    monkeypatch.setattr(cx, "_LINE_ACCUM_CAP", 65536)
    _stub_cli(monkeypatch, cli=_cli_that_lingers_after_thread_started(tmp_path))

    captured: list = []
    _patch_create_subprocess_with_a_hanging_wait(monkeypatch, captured)

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9), 90)

    assert result.is_error is True
    assert captured, "create_subprocess_exec was never called"
    transport = captured[0]._transport
    assert transport is not None, "no transport was ever attached to proc"
    assert transport.is_closing() is True, (
        "the subprocess transport was left open after a timed-out teardown "
        "— _close_subprocess_transport must be called from _kill_and_reap "
        "on the timeout arm")


# ---------------------------------------------------------------------------
# AC2 — no unraisable exception once the loop that owned the transport
# closes. This is the direct reproduction of the measured gate failure.
# ---------------------------------------------------------------------------


def test_no_unraisable_after_the_loop_closes_on_the_timeout_path(tmp_path, monkeypatch):
    """Sync test (asyncio_mode=auto still runs plain `def` tests as-is). The
    whole scenario runs inside `asyncio.run(...)`, which creates a FRESH
    loop and CLOSES it when the coroutine returns — the exact condition the
    incident needs: a transport whose owning loop is already gone by the
    time GC finalises it. RED if the close is removed: `del`-ing every
    reference and forcing GC after the loop is closed reliably reproduces
    `RuntimeError: Event loop is closed` from
    `BaseSubprocessTransport.__del__` on this repo's CPython."""
    monkeypatch.setattr(cx, "_STDOUT_LIMIT", _TINY_STDOUT_LIMIT)
    monkeypatch.setattr(cx, "_LINE_ACCUM_CAP", 65536)
    _stub_cli(monkeypatch, cli=_cli_that_lingers_after_thread_started(tmp_path))

    captured: list = []
    _patch_create_subprocess_with_a_hanging_wait(monkeypatch, captured)

    async def _drive():
        return await asyncio.wait_for(
            cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9), 90)

    with _UnraisableCapture() as capture:
        result = asyncio.run(_drive())
        assert result.is_error is True

        # Drop every strong reference this test holds so the transport
        # becomes collectible, then force finalization NOW — after
        # asyncio.run() has already closed the loop above.
        del result
        captured.clear()
        gc.collect()
        gc.collect()

    assert capture.caught == [], (
        "BaseSubprocessTransport.__del__ raised after the loop closed "
        f"(exactly the measured gate failure): "
        f"{[repr(getattr(u, 'exc_value', u)) for u in capture.caught]}")


# ---------------------------------------------------------------------------
# AC3 — positive control: the normal (in-bound) path is unaffected.
# ---------------------------------------------------------------------------


async def test_a_teardown_that_completes_in_the_bound_is_unchanged(tmp_path, monkeypatch):
    """A clean, fast exit never reaches `_TEARDOWN_WAIT` at all — `proc.wait`
    is NOT stubbed here, so this exercises the success arm the timeout-path
    tests above never touch. The result event's shape is asserted against
    the same baseline `tests/test_codex_oversized_jsonl_line.py` already
    pins (`is_error`, `final_text`, `session_id`, `stop_reason`), plus usage
    totals, plus the same no-unraisable-exception assertion as AC2 — proving
    the unconditional `_close_subprocess_transport(proc)` call added to
    `_kill_and_reap` (it runs on BOTH arms, not just the timeout one) leaves
    the RESULT event and error semantics unchanged on this arm. It is NOT a
    behavioural no-op at the transport level — `stream()` returns as soon as
    it sees `turn.completed` without draining stdout/stderr to EOF, so
    asyncio's own auto-close never fires here either, and this test's own
    `transport.is_closing() is True` assertion below is what pins that the
    explicit close is doing real, necessary work on this arm too (removing
    the call turns this assertion red, not just the timeout-arm tests)."""
    _stub_cli(monkeypatch, cli=_cli_that_completes_cleanly(tmp_path))

    captured: list = []
    real_create = asyncio.create_subprocess_exec

    async def _create_capturing(*a, **k):
        proc = await real_create(*a, **k)
        captured.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_capturing)
    monkeypatch.setattr(cx.asyncio, "create_subprocess_exec",
                        _create_capturing, raising=False)

    with _UnraisableCapture() as capture:
        result = await asyncio.wait_for(
            cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9), 30)

        assert result.is_error is False, (result.final_text or "")[:300]
        assert result.final_text == "all done"
        assert result.session_id == "th_1"
        assert result.stop_reason == "end_turn"
        assert result.tokens_used == 110  # (100 - 0 cached) + 10 output
        assert result.output_tokens == 10
        assert result.cache_read_tokens == 0

        assert captured, "create_subprocess_exec was never called"
        transport = captured[0]._transport
        assert transport is not None
        assert transport.is_closing() is True, (
            "a cleanly-exited child's transport should already be closed "
            "by asyncio's own machinery once wait() resolves")

        del result
        captured.clear()
        gc.collect()
        gc.collect()

    assert capture.caught == [], (
        "the normal-completion path must not produce an unraisable "
        f"exception either: {[repr(getattr(u, 'exc_value', u)) for u in capture.caught]}")


# ---------------------------------------------------------------------------
# AC4 — the private attribute is pinned, not silently degraded.
# ---------------------------------------------------------------------------


async def test_process_still_exposes_the_private_transport_attribute():
    """`_close_subprocess_transport` has no public route and relies on
    `proc._transport` — verified live against CPython 3.12.13
    (`sys.version`), where `asyncio.subprocess.Process.__init__` sets
    `self._transport = transport` (`Lib/asyncio/subprocess.py`). If a future
    CPython renames or removes this attribute, `_close_subprocess_transport`
    would silently stop closing anything (its own `getattr(..., None)` guard
    exists only to keep a teardown from raising, per its docstring) — THIS
    test is what must fail loudly instead, here rather than in production."""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    try:
        assert hasattr(proc, "_transport"), (
            "asyncio.subprocess.Process no longer exposes `_transport` on "
            f"CPython {sys.version.split()[0]} — "
            "codex_backend._close_subprocess_transport's only route to "
            "closing the child's subprocess transport is gone; it must be "
            "re-verified and re-pinned against the new stdlib internals")
        transport = proc._transport
        assert callable(getattr(transport, "close", None)), (
            "proc._transport no longer has a callable close() on CPython "
            f"{sys.version.split()[0]} — "
            "codex_backend._close_subprocess_transport calls it directly")
        assert callable(getattr(transport, "is_closing", None)), (
            "proc._transport no longer has a callable is_closing() on "
            f"CPython {sys.version.split()[0]}")
    finally:
        await proc.wait()
