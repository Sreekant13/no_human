"""Async SQLite store (WAL). Single-user, single-host — no Postgres (§3.6)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any, AsyncIterator, Awaitable, Callable, NamedTuple, TypeVar,
)

import aiosqlite

from .task import Task, TaskStatus, assert_transition

log = logging.getLogger("no_human.db")


# --------------------------------------------------------------------------- #
# The role registry: WHICH NAMED ROLE each family of attempt token columns
# bills to. THE one list — every cost surface derives its column set from here
# rather than re-typing it, because this repo has already shipped the drift
# this prevents twice (the coder-only sum that rigged the north-star ratio,
# then the four-tier sums that quietly excluded planning).
#
# Keyed by COLUMN PREFIX, valued by the role's human name. The coder's prefix
# is the empty string: its columns are the unprefixed originals
# (`tokens_used`, `cache_read_tokens`, …) and renaming them would rewrite 50+
# call sites for no gain.
#
# Adding a role means: one entry here, three `*_tokens`/`*_cache_*` columns
# plus one `*_output_tokens` column in `_migrate`, and a sink that fills them.
# `test_role_token_accounting.py` fails on the first without the second, which
# is the only reason this is a registry and not six literals.
#
# Ordered coder-first because that is the order every human-facing breakdown
# prints in; nothing else depends on it.
USAGE_ROLES: dict[str, str] = {
    "": "coder",
    "review_": "reviewer",
    "plan_": "planner",
    "utility_": "utility",
    "supervisor_": "supervisor",
    "distill_": "distill",
}

#: The `pr_outcomes.outcome` values that are FINAL (migration 0010). See the
#: "PR outcomes" section of `Store` for why one constant and not two literals.
#: Mirrors `vcs.pr_outcome.MERGED` / `CLOSED_UNMERGED`; `core` may not import
#: `vcs`, so `tests/test_pr_outcome.py` pins the two spellings equal instead.
SETTLED_PR_OUTCOMES: tuple[str, ...] = ("merged", "closed_unmerged")

#: The same tuple as a SQL `IN (...)` list. Built from the tuple rather than
#: written out again, so a value added above cannot be missed below. Safe to
#: interpolate: the members are module-level literals, never caller input.
_SETTLED_OUTCOMES_SQL = "({})".format(
    ", ".join(f"'{o}'" for o in SETTLED_PR_OUTCOMES))

# The roles that are NOT the coder's own session — i.e. the ones accumulated
# out-of-band during a task and drained onto the attempt row at exit
# (`Orchestrator._pop_aux_usage`) or to the unattributed ledger when no
# attempt ever claims them (`_flush_orphaned_aux_usage`). The reviewer is
# absent on purpose: its burn is written by the review path directly onto the
# attempt row it just judged, never through the aux accumulator.
#
# DERIVED from `USAGE_ROLES` by naming the two EXCLUSIONS rather than by
# re-typing the four members. A hand-written list here would have to be
# widened by hand every time a role is registered, and the failure mode is
# silent: an undrained accumulator loses that role's burn without raising.
# Stating the exclusions instead means a new role is aux BY DEFAULT — the
# safe direction, since a drained role that had nothing to drain is a no-op
# while an undrained one is missing spend.
AUX_USAGE_TIERS: tuple[str, ...] = tuple(
    t for t in USAGE_ROLES if t not in ("", "review_"))


def usage_columns_for(tier: str) -> tuple[str, ...]:
    """The three ADDEND token columns for one role prefix.

    Deliberately excludes ``{tier}output_tokens``: that is a SLICE of
    ``{tier}tokens_used``, already inside it, and summing it as a fourth
    addend double-counts every output token. See
    ``Store._output_columns_by_class``.
    """
    return (
        "tokens_used" if tier == "" else f"{tier}tokens_used",
        f"{tier}cache_read_tokens",
        f"{tier}cache_creation_tokens",
    )


def _resolve_migrations_dir() -> Path:
    """Locate the schema migrations across the ways this code ships.

    Mirrors `api/app.py::_resolve_web_dist`, for the same reason and with the
    same two layouts:

    1. **Repo checkout / frozen desktop bundle** — ``parents[3]/migrations``.
       In a checkout ``__file__`` is ``<repo>/src/no_human/core/db.py``, so
       parents[3] is the repo root. Under a PyInstaller onedir freeze it is
       ``<bundle>/_internal/no_human/core/db.py``, so parents[3] is the bundle
       root, which is where ``packaging/build-installer.sh`` copies them.
    2. **Wheel install** — ``<site-packages>/no_human/migrations``. parents[3]
       is meaningless there (it points at ``lib/python3.X``, outside the
       package), so the migrations are shipped INSIDE the package instead;
       ``pyproject.toml`` force-includes ``migrations`` to that name.

    Layout 2 did not exist until 2026-08-01, and layout 1 silently resolved to
    ``<venv>/lib/python3.X/migrations`` — a directory that is simply absent.
    `Path.glob` on a missing directory does not raise, it yields nothing, so
    `_migrate` ran zero migrations, created no schema, and every wheel install
    of no_human was unusable from its very first command. See `_migrate` for
    the fail-closed check that now backs this up.

    The first candidate is returned as the fallback when neither exists, so the
    error names the path a developer expects to see.
    """
    candidates = (
        Path(__file__).resolve().parents[3] / "migrations",   # checkout / frozen
        Path(__file__).resolve().parent.parent / "migrations",  # installed wheel
    )
    for candidate in candidates:
        if any(candidate.glob("*.sql")):
            return candidate
    return candidates[0]


MIGRATIONS_DIR = _resolve_migrations_dir()

_T = TypeVar("_T")

# The `(Store, owning asyncio task)` pairs whose critical section the CURRENT
# context is inside. A SET, not one Store: two Stores per process is the normal
# shape here (`nh start` runs the pool's Store and the Jira poller's), and one
# slot made an A -> B -> A call chain self-deadlock, because entering B erased
# the record that A was already held and the return into A then waited on a lock
# this very task owns.
#
# The pair carries the owning task because the context is NOT private to the
# task that set it. `asyncio` COPIES the context into each new Task, so a Task
# created INSIDE a critical section inherits this set verbatim and would take
# the reentrant fast path — running unguarded on the connection while its parent
# still holds the section. Measured on 3.12: `asyncio.create_task`,
# `asyncio.ensure_future` and `TaskGroup.create_task` all inherit;
# `asyncio.wait_for(coro, <positive timeout>)` does NOT, because since 3.12 it
# awaits the coroutine in the caller's own task under `asyncio.timeouts.timeout`
# rather than wrapping it (it did wrap on <=3.11, and still does when the
# timeout is <= 0 — but `requires-python` is >=3.12, so the live vector is
# task creation).
#
# Comparing the recorded owner against `asyncio.current_task()` is what tells
# genuine reentrancy (same task, nested call) apart from that inheritance; see
# `Store._critical`, which raises on the second. Cross-task exclusion for tasks
# created OUTSIDE a section never depended on this — those start from a context
# in which the set is empty.
_in_critical: ContextVar["frozenset[tuple[Store, Any]]"] = ContextVar(
    "_in_critical", default=frozenset())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# THE QUEUE-VISIBILITY CONTRACT, as a symbol rather than a literal repeated in
# five files. `LearningQueue.pending()`, `nh learnings`, `GET /api/learnings`
# and the transcript ingester all select `source = "proposed"`, so an
# unconfirmed memory written under ANY other `source` is invisible to the human
# gate it exists for — it is not queued, it is lost.
#
# Two docstrings already stated this invariant (the `origin` column comment
# above `add_memory`, and `learning/queue.py`'s ORIGIN_* block) and NOTHING
# enforced it. Three separate call sites then broke it — `nh history --analyze`
# (`source="history"`), `nh reply`'s mined learnings (`source="reply"`, which
# lost two real rows from the operator's own review replies) and the curator's
# consolidation pass (`source="curator"`, which archives the proposals it
# consolidates, so a broken write there DESTROYS queue entries). A comment is
# not a constraint; `add_memory` refuses the shape now. Provenance belongs in
# `origin`, which is a second column for exactly this reason.
SOURCE_PROPOSED = "proposed"


def read_file_marker(path: Path) -> "SnapshotMarker":
    """Read the database file's true head through a brand-new connection.

    MODULE-LEVEL, and not a `Store` method, because it is deliberately not an
    operation on the Store's connection — it is the second opinion the Store's
    connection is checked against, and `tests/test_db_concurrency.py::
    test_no_store_read_keeps_a_raw_cursor` is right to flag a raw `fetchone()`
    inside the class. Putting it here says what it is.

    Stdlib `sqlite3` rather than another `Store`: `Store.connect()` runs the
    migrations, and migration 0009 WRITES (it drops and recreates an FTS trigger
    on every connect). A health probe that writes to the database it is auditing
    is not a health probe. `query_only` makes that structural rather than a
    promise, which matters because this runs against the operator's live file.

    Blocking on purpose — callers hand it to `asyncio.to_thread`.
    """
    import sqlite3
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT count(*), max(updated_at) FROM tasks").fetchone()
        return SnapshotMarker(int(row[0]), row[1])
    finally:
        conn.close()


def serialized_write(
    fn: Callable[..., Awaitable[_T]],
) -> Callable[..., Awaitable[_T]]:
    """Run one Store write — every statement of it plus its COMMIT — as a
    single critical section on the shared connection.

    ONE `aiosqlite.Connection` is shared by every coroutine (the pool runs
    `concurrency.max_workers` tasks against one Store). aiosqlite serialises
    *individual* operations on its worker thread, but never a *sequence* of
    them: each `await` is a scheduling point where another coroutine's write —
    and, fatally, its `commit()` — runs in the middle of ours. Two consequences,
    both of which this decorator fixes:

    1. `commit()` ends the connection's implicit transaction, so a foreign
       commit lands halfway through any multi-statement write here
       (`create_attempt`'s UPDATE+INSERT, `_migrate`, `update_attempt`'s
       read-modify-write, `add_memory`'s dedupe-then-insert). The atomicity
       those writes assume was never real once two tasks ran at once.
    2. If the statement the foreign commit interrupts is a *writer that has
       produced a row* — `UPDATE … RETURNING`, whose VDBE stays live between
       `execute()` and the fetch — SQLite refuses the COMMIT outright:
       ``OperationalError: cannot commit transaction - SQL statements in
       progress``. That crash killed real attempts (see
       `tests/test_db_concurrency.py`).

    Reads once ran outside this lock, on the reasoning that they never COMMIT,
    so they cannot split someone else's transaction, and that a live SELECT
    cursor does not block COMMIT (SQLite only refuses on `db->nVdbeWrite > 0`).
    Every clause of that is still true, and the conclusion drawn from it was
    still wrong: a SELECT does not block a COMMIT, but while it is UNRESET it
    holds this connection's read transaction open, and a write attempted from
    inside an open read transaction fails immediately. So reads now take this
    lock too — see `Store._critical` and the read helpers below, where the
    window is described exactly.

    The remaining sentence of the original reasoning was that the lock is
    per-Store, i.e. per-connection, which is the right scope — cross-connection
    and cross-process serialisation being SQLite's own job, and unchanged.

    **That is true and was still not enough, so read it narrowly.**
    Delegating to SQLite is correct for LOCK contention between connections
    with nothing else open: the loser waits, and `busy_timeout` (5000 ms, from
    `sqlite3.connect`'s default via aiosqlite — measured, not assumed) decides
    how long. It is NOT correct once THIS connection holds a read transaction,
    because SQLite will not run its busy handler for a read-to-write upgrade —
    waiting there can only deadlock — so it returns at once. Measured on the
    parent commit under the two-Store storm below: time to failure 0.6–1.8 ms
    against a 5000 ms timeout, three orders of magnitude short of ever
    consulting it. There is normally a peer to lose to: `nh start` opens a
    single shared Store that `nh start` hands to both its intake pollers and
    the app lifespan (`cli/commands.py::start._go`, `app.state._external_store`
    — one connection since the 2026-08-03 rescue), and every `nh` CLI
    invocation opens one more in another process. This lock, being per-Store, spans none of them.

    So the boundary above is real, and crossing it safely takes two things, both
    of which this class now does. First, no read may outlive its own fetch —
    see `_fetchone`/`_fetchall`, where that is argued in full; that is what stops
    a read transaction being held INDEFINITELY. Second, reads take this same
    critical section, because closing promptly still leaves the `await` gap
    between `execute()` and the fetch, and a concurrent coroutine's write inside
    that gap fails just the same. `tests/test_db_concurrency.py` covers both.

    **Serialising reads is not free, and the earlier claim that it was is
    withdrawn.** The reasoning behind it — aiosqlite runs every operation for a
    connection on one worker thread, so there is no read/write parallelism to
    give up — is true about the THREAD and says nothing about the LOCK, which
    also makes concurrent readers wait for each other. Measured, six interleaved
    paired rounds, four concurrent readers over a copy of a real 74 MB database
    (`get_task` + `list_attempts`, 480 reads per round):

        parent commit    5,656 – 7,740 reads/s   (median 6,114)
        this commit      2,284 – 4,897 reads/s   (median 2,986)

    — a 2.07x drop at the median, spread 1.16x–2.94x across those six pairs, on
    a loaded machine.

    **DO NOT QUOTE A SINGLE FACTOR FROM THIS.** Three independent measurements
    of the same thing now exist and they do not agree: ~1.6x (12 paired rounds),
    ~2.07x (the six above), ~3x (11,722 / 9,444 vs 2,731 / 3,608). The spread
    WITHIN one six-round session, 1.16x–2.94x, is about as wide as the spread
    BETWEEN sessions — which is what says the differences are machine load, not
    the lock. An earlier draft of this paragraph shipped "2–3x"; that band
    excluded its own disclosed minimum, and only 1 of an independent reviewer's
    12 rounds fell inside it. Withdrawn.

    The honest statement, and the only one this paragraph now makes: reads are
    SLOWER under concurrent readers — roughly 1.2x–3x, load-dependent and not
    reliably characterised. Re-measure before acting on any figure here.

    What that costs in practice, measured the same way: nothing visible on the
    board. One websocket tick (`_board_tasks`) is 68.5 ms vs 63.5 ms at one
    socket and 257.5 ms vs 268.0 ms at four (medians, same six rounds) — the
    first pair moves the WRONG way for a regression, so both are noise,
    because a tick is dominated by two large queries rather than by read count.
    The regression is real, known and bounded; it is not being optimised away
    here, and a future reader should re-measure before assuming it still holds.
    """

    @functools.wraps(fn)
    async def wrapper(self: "Store", *args: Any, **kwargs: Any) -> _T:
        async with self._critical():
            # ROLLBACK ON THE ERROR PATH. Without this, one exception between
            # `execute()` and `commit()` pinned the connection FOREVER, and
            # `db.py` contained no `rollback` anywhere at all (grep it; use
            # `journal_mode` as the known positive that the grep works).
            #
            # `aiosqlite.connect()` passes no `isolation_level`, so Python's
            # legacy implicit-BEGIN applies: the first INSERT/UPDATE/DELETE
            # opens a transaction that only COMMIT or ROLLBACK can end. A write
            # that raised after that point left the transaction open, and every
            # later statement on the shared connection ran inside it — reads
            # served from a snapshot frozen at the moment of the failure, writes
            # failing `database is locked` (SQLITE_BUSY / _BUSY_SNAPSHOT).
            # Restarting the server was the only known cure.
            #
            # WHAT THIS DOES NOT FIX, measured rather than reasoned. An earlier
            # draft of this comment claimed `rollback()` also resets every
            # statement on the connection and therefore ends a READ pin left by
            # an unreset cursor. That is FALSE on this stack. Measured on
            # CPython 3.12 + aiosqlite: pin a connection with `execute("SELECT *
            # FROM tasks")` + one `fetchone()` on a 9-row table, let a peer
            # commit, call `rollback()` — the connection still sees 8 rows while
            # the file holds 9.
            #
            # So this rollback ends a WRITE transaction and nothing else, which
            # is exactly one of the ways a connection gets pinned. That is the
            # concrete reason `probe_snapshot_staleness` is the load-bearing
            # guard and this is the cheap one: detection covers the read pin,
            # the recovered-stale-WAL case, and whatever else there turns out to
            # be. `tests/test_frozen_snapshot_guard.py` holds the measurement so
            # the claim cannot quietly come back.
            #
            # It is a no-op when no transaction is open, the common case.
            #
            # `BaseException`, not `Exception`: a `CancelledError` between
            # `execute()` and `commit()` pins the connection exactly as hard as
            # a `sqlite3.Error` does, and cancellation is routine here (the pool
            # cancels workers on shutdown).
            try:
                result = await fn(self, *args, **kwargs)
            except BaseException:
                await self._rollback_quietly()
                raise
            self.last_successful_write_at = _now()
            return result

    wrapper.__nh_serialized_write__ = True  # type: ignore[attr-defined]
    return wrapper


class SnapshotMarker(NamedTuple):
    """How far through the write history one connection can see.

    `count` alone is not enough: an UPDATE-only wedge (a task escalating) moves
    `max_updated_at` without changing the row count, and that is precisely the
    shape the 2026-08-01 incident took for its first two symptoms.
    """

    count: int
    max_updated_at: str | None

    def behind(self, other: "SnapshotMarker") -> bool:
        return (self.count < other.count
                or (self.max_updated_at or "") < (other.max_updated_at or ""))


class StalenessProbe(NamedTuple):
    """One verdict from `Store.probe_snapshot_staleness`."""

    stale: bool
    shared: SnapshotMarker
    fresh: SnapshotMarker
    recheck: SnapshotMarker | None   # the confirming re-read; None if not needed
    reason: str

    def __repr__(self) -> str:  # keeps log lines and repro output readable
        return (f"StalenessProbe(stale={self.stale}, shared={tuple(self.shared)}, "
                f"fresh={tuple(self.fresh)}, reason={self.reason!r})")


class ImportedTaskRow(NamedTuple):
    """One row of the backlog picker's imported-chip projection (SCRUM-54) —
    only the four columns the chip lookup needs, never a full Task hydration."""

    external_id: str
    id: str
    status: str
    created_at: str


class Store:
    """Thin async wrapper over the tasks/attempts tables."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._db: aiosqlite.Connection | None = None
        # Guards every write critical section on this connection — see
        # `serialized_write` for why one connection + N coroutines needs it.
        self._write_lock = asyncio.Lock()
        # Liveness counters. Read by `/api/worker/status` so that "idle" and
        # "wedged" stop reading identically (they were the same JSON for six
        # hours on 2026-08-01).
        self.last_successful_write_at: str | None = None
        self.stale_detections = 0
        self.last_stale_at: str | None = None
        self.reconnects = 0
        self.last_reconnect_at: str | None = None

    async def connect(self) -> "Store":
        # no_human.db sits beside the credential store; the directory must be
        # private even when the DB is what creates it.
        from ..config import ensure_private_dir
        ensure_private_dir(self.path.parent)
        self._db = await aiosqlite.connect(self.path)
        # connect() is ATOMIC: it either returns a usable Store or leaves no
        # trace of itself. Anything less hangs the process forever.
        #
        # `aiosqlite.connect()` starts a worker thread, and that thread is NOT
        # a daemon (`aiosqlite/core.py`: `Thread(target=_connection_worker_thread,
        # args=(self._tx,))`, no `daemon=True`). Its loop is a blocking
        # `tx.get()` that only ever ends when `close()` enqueues the stop
        # sentinel. So if any step below raises, the exception propagates to
        # the caller perfectly well — and then the interpreter reaches
        # `threading._shutdown`, joins that live non-daemon thread, and blocks
        # there for the rest of time. The user sees a traceback, if anything,
        # and a command that never returns; ^C is the only way out.
        #
        # That converted "no such table: tasks" (the wheel shipped no
        # migrations) into an unbounded silent hang on `nh status`, `nh doctor`
        # and `nh task list` for every new user. Closing here is what makes the
        # failure a normal, fast, reported error. It is not specific to that
        # bug: EVERY failure path in connect() had it, and every future one
        # would too.
        try:
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode = WAL")
            await self._db.execute("PRAGMA foreign_keys = ON")
            await self._migrate()
            # Warm the loaded-code snapshot HERE, off the event loop, because
            # this is the one place EVERY entrypoint passes through — the
            # server's lifespan, but equally `nh` commands and the eval
            # harnesses, none of which have a lifespan to pre-warm them. Left
            # cold, the first `create_attempt` pays three blocking git
            # subprocesses while holding the sqlite write transaction its own
            # UPDATE just opened. This repo has already lost days to lock
            # storms; a telemetry stamp must not be able to start another one.
            #
            # Three is `ls-files`, `rev-parse`, `status` — 220ms measured on
            # this checkout, and a 30s worst case under the 10s per-call
            # timeout. It was TWO until the tracking check closed the
            # borrowed-sha hole: this count is a measured claim, and it moves
            # when the calls do.
            from .build_info import loaded_code
            await asyncio.to_thread(loaded_code)
        except BaseException:
            db, self._db = self._db, None
            try:
                await db.close()  # stops the worker thread (its `finally` does)
            except BaseException:  # pragma: no cover - never mask the real error
                log.debug("closing the sqlite connection after a failed "
                          "connect() also failed", exc_info=True)
            raise
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _rollback_quietly(self) -> None:
        """End whatever transaction a failed write left open. Never raises.

        The caller is already handling an exception; a failure here must not
        replace it with a less informative one. It is logged rather than
        swallowed, because a rollback that itself fails means the connection is
        in the state `probe_snapshot_staleness` exists to catch.
        """
        db = self._db
        if db is None:
            return
        try:
            await db.rollback()
        except asyncio.CancelledError:
            # NOT ours to swallow. `await db.rollback()` is a suspension point,
            # so a cancellation can arrive DURING it — and catching that here
            # let the task finish with the ORIGINAL exception and
            # `task.cancelled() == False`, i.e. a cancelled task that does not
            # report as cancelled. `serialized_write` reasons carefully about
            # `CancelledError` in the OUTER handler and the first version of
            # this inner one quietly undid it.
            #
            # Re-raising replaces the original exception with the
            # `CancelledError`, which is correct: cancellation outranks it, and
            # the caller that cancelled is entitled to see cancellation. The
            # transaction is left to the connection's teardown, which is the
            # same position every other cancelled write is in.
            raise
        except BaseException:  # noqa: BLE001 — never mask the original error
            log.warning("rollback after a failed write also failed; this "
                        "connection may be serving a frozen snapshot",
                        exc_info=True)

    # --- staleness: never trust the shared connection's own answer --------- #
    #
    # WHY THIS EXISTS AND WHY IT IS THE LOAD-BEARING GUARD. On 2026-08-01 the
    # server's shared connection served a read snapshot pinned three hours in
    # the past. Rows written after the pin were invisible to it, so the
    # scheduler re-dispatched two long-finished tasks ~12x/minute and never saw
    # the one real task waiting. Every surface reported health, because every
    # surface asked THE POISONED CONNECTION.
    #
    # The rollback above removes ONE way to get pinned, and the incident's own
    # timeline argues it was not the way that happened: the process started at
    # 23:28:37 and was pinned to before 20:34:30, three hours before its own
    # first read, with the first crash at log line 220 of 46,000 — i.e. the
    # connection was stale FROM BIRTH, which no transaction this process left
    # open can explain (a stale WAL index recovered at startup can). The first
    # cause is therefore still INFERRED.
    #
    # So this check deliberately does not care what caused the pin. It asks a
    # second, independent connection what the FILE says and compares. Any
    # mechanism that freezes the shared connection — an un-rolled-back write, an
    # unreset cursor, a recovered stale `-shm`, or something not yet imagined —
    # produces the same divergence and is caught here.

    async def _shared_marker(self) -> SnapshotMarker:
        row = await self._fetchone(
            "SELECT count(*) AS n, max(updated_at) AS m FROM tasks")
        return SnapshotMarker(int(row["n"]), row["m"])

    async def probe_snapshot_staleness(self) -> StalenessProbe:
        """Is this connection's read view behind the file? Cheap; per-tick.

        THE FALSE-POSITIVE THAT WOULD MAKE THIS UNSAFE TO ACT ON. The shared
        read and the fresh read cannot be simultaneous, so a peer committing
        between them leaves the fresh marker legitimately ahead. Reconnecting on
        that would churn the connection under ordinary concurrent load — and
        peers are guaranteed here (`nh start` opens Stores for the Jira and
        Linear pollers, and every `nh` CLI command opens one in another
        process).

        The discriminator is a CONFIRMING RE-READ, and it works because the two
        states differ in exactly one observable way: a healthy connection starts
        a new read transaction per statement and so sees the peer's commit
        immediately, whereas a pinned one can never catch up by definition. Only
        a connection still behind on the SECOND read is reported stale.

        Being behind is also the only direction that matters. The shared marker
        reading AHEAD of the fresh one is the same benign race viewed from the
        other side, never a pin.
        """
        shared = await self._shared_marker()
        fresh = await asyncio.to_thread(read_file_marker, self.path)
        if not shared.behind(fresh):
            return StalenessProbe(False, shared, fresh, None, "up-to-date")
        recheck = await self._shared_marker()
        if not recheck.behind(fresh):
            # It caught up, so it was never pinned: a peer simply committed
            # between the two reads.
            return StalenessProbe(False, recheck, fresh, recheck,
                                  "concurrent-write-race")
        self.stale_detections += 1
        self.last_stale_at = _now()
        return StalenessProbe(True, recheck, fresh, recheck, "frozen-snapshot")

    async def reconnect(self) -> None:
        """Drop the connection and open a new one. Recovery for a frozen view.

        Held under the critical section so no coroutine is mid-statement on the
        connection being replaced. `connect()` re-enters that section through
        `_migrate`; `_critical` is reentrant for the same task and Store, so
        that is safe rather than a deadlock.

        The reference is dropped BEFORE the close is attempted: if closing a
        wedged connection fails, the wedged object must not survive as
        `self._db`. If the subsequent connect also fails, `self.db` raises a
        clear "not connected" error, which is a loud failure — the state this
        method exists to escape is the silent one.
        """
        async with self._critical():
            old, self._db = self._db, None
            if old is not None:
                try:
                    await old.close()
                except BaseException:  # noqa: BLE001
                    log.warning("closing the stale connection failed; "
                                "replacing it anyway", exc_info=True)
            await self.connect()
            self.reconnects += 1
            self.last_reconnect_at = _now()

    def liveness(self) -> dict[str, Any]:
        """Connection-health counters for `/api/worker/status`."""
        return {
            "last_successful_write_at": self.last_successful_write_at,
            "stale_detections": self.stale_detections,
            "last_stale_at": self.last_stale_at,
            "reconnects": self.reconnects,
            "last_reconnect_at": self.last_reconnect_at,
        }

    async def __aenter__(self) -> "Store":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store not connected; call connect() first")
        return self._db

    # --- reads: the read transaction must not outlive the read ------------- #
    #
    # EVERY read goes through these two helpers, and the reason is not tidiness.
    #
    # THE WINDOW. A SELECT opens this connection's read transaction when it is
    # first stepped, and holds it until the STATEMENT IS RESET. Reset happens on
    # exactly three events: the statement steps to SQLITE_DONE, the cursor is
    # closed, or the cursor is garbage-collected. So the dangerous span is not
    # "for as long as the cursor is referenced" — that overstates it, and the
    # overstatement matters, because it points at the wrong call sites. The two
    # real spans are:
    #
    #   1. THE `await` GAP BETWEEN `execute()` AND THE FETCH. This one is in
    #      EVERY read, no exceptions. aiosqlite dispatches `execute` and `fetch*`
    #      to its worker thread as two separate awaitables, so there is always a
    #      scheduling point between them at which the read transaction is open
    #      and another coroutine can run. This is the window the critical
    #      section closes, and it is why closing the cursor promptly is not on
    #      its own enough.
    #   2. EVERYTHING AFTER THE FETCH, for a read that does not consume its
    #      whole result set. `fetchmany`, `async for` over more rows than
    #      aiosqlite's 64-row `iter_chunk_size`, or `fetchone` against a query
    #      that matched more than one row all leave the statement live until
    #      close. This is the span that can be held indefinitely, and it is the
    #      one `finally: await cur.close()` below closes.
    #
    # A read that DOES consume its whole result set resets itself and holds
    # nothing past the fetch — CPython's `sqlite3` steps once inside `execute()`
    # and once more inside the fetch, so `SELECT … WHERE id = ?` on a unique key
    # followed by `fetchone()` is already reset. Measured against raw aiosqlite
    # (5-row table, peer commits between the read and the write):
    #
    #     execute() only, no fetch ................. WRITE FAILS
    #     SELECT … WHERE id = ? + fetchone ......... write OK
    #     SELECT * + ONE fetchone .................. WRITE FAILS
    #     SELECT * + fetchall ...................... write OK
    #     SELECT * + fetchmany(2) .................. WRITE FAILS
    #     `async for` + break, 200-row table ....... WRITE FAILS
    #     `async for` + break, 5-row table ......... write OK  (one chunk = done)
    #
    # That table is what `test_a_pinned_snapshot_wedges_the_connection_
    # permanently` characterises, and why it has to seed MORE rows than it fetches.
    #
    # WHY THE WINDOW IS FATAL HERE. A write attempted while this connection
    # holds a read transaction has to upgrade it, and SQLite will not run the
    # busy handler for an upgrade — waiting on one can only deadlock — so it
    # fails AT ONCE instead of after `busy_timeout`. Peers are guaranteed: `nh
    # start` shares ONE Store across its pollers and the app lifespan
    # (`app.state._external_store` — the second/third connections were the 2026-08
    # lock-flood defect and are gone),
    # every `nh` CLI command opens one in another process, and `Store.connect()`
    # itself WRITES — migration 0009
    # drops and recreates the FTS trigger on every connect — so a bare
    # `connect()` + `close()` and nothing else is enough to break a pinned
    # write (measured).
    #
    # WHICH ERROR CODE, HONESTLY. Two, and the traceback does not distinguish
    # them: same file, same line, same message `database is locked`.
    #
    #   * SQLITE_BUSY_SNAPSHOT (517) once a peer has COMMITTED past the snapshot
    #     this connection is pinned to. Deterministic, and permanent until the
    #     statement is reset — every later write fails, which is why restarting
    #     the server used to be the only known cure.
    #   * SQLITE_BUSY (5) when the upgrade merely loses to a peer that holds the
    #     write lock right now.
    #
    # Both appeared, mixed, in single runs of the realistic two-Store storm on
    # the parent commit (four errors per run; 1–3 of each, varying run to run).
    # So the production code was NOT inferable from the traceback, and nothing
    # here asserts which one it was: the honest statement is that an open read
    # transaction makes the next write on that connection fail instantly with
    # SQLITE_BUSY or SQLITE_BUSY_SNAPSHOT. The fix removes both, because it
    # removes the open read transaction.
    #
    # What makes either much worse than it sounds is the message. `database is
    # locked` is a lie about the cause: the file stays writable by every other
    # connection throughout, so the usual external-writer probe (`BEGIN
    # IMMEDIATE` from the `sqlite3` CLI) returns in milliseconds and reports a
    # healthy database while the server cannot write at all.
    #
    # `tests/test_db_concurrency.py` covers this over the read surface, so a new
    # read that reintroduces a bare `self.db.execute` + a fetch fails there.

    # `await self.db.execute(...)` + an explicit close, rather than the tidier
    # `async with self.db.execute(...)`: the connection's `execute` is
    # monkeypatched by tests (`_park_after`) to park a write mid-sequence, and a
    # plain coroutine substitute supports `await` but not `async with`. The
    # guarantee is identical — `finally` runs on the exception path too, which
    # is the path that matters, since that is exactly when a pinned snapshot
    # would otherwise be left behind.

    @asynccontextmanager
    async def _critical(self) -> "AsyncIterator[None]":
        """The connection's critical section — held by reads AND writes.

        REENTRANT, per asyncio task, and it has to be: the read-modify-write
        methods (`update_attempt`, `add_memory`, `set_status`) call `_fetchone`
        from inside `serialized_write`, and `asyncio.Lock` is not reentrant, so
        a plain acquire there would deadlock the pool instead of unlocking it.

        The exemption is keyed on `(Store, owning task)`, and both halves earn
        their place:

        * **the Store**, because the set holds every section this context is
          inside, not just the last one. One slot self-deadlocked an
          ``A -> B -> A`` chain across two Stores — and two Stores in one
          process is this product's normal shape, not a corner case;
        * **the owning task**, because a `ContextVar` is NOT private to the task
          that set it. asyncio copies the context into every new Task, so a Task
          created INSIDE this section starts life holding the exemption.
          Nothing spawns a task in here today, so this is a trap laid for a
          future call site rather than a live bug — which is exactly the kind
          that ships silently. It is made loud below instead: taking the fast
          path on an INHERITED exemption would run unguarded on the connection
          while the parent still holds the lock, so it raises.

        What was always true, and still is: a task created OUTSIDE a section
        cannot see the exemption, because it copied a context in which the set
        was empty. That is the exclusion the lock is actually for.

        WHAT THE SET DOES NOT SOLVE, said here because holding a set of Stores
        invites the assumption that it does. Two DIFFERENT tasks nesting two
        Stores in opposite orders — task 1 takes A then B, task 2 takes B then A
        — deadlock classically, and this exemption cannot see it: the owners
        differ, so each task correctly reads the other's lock as foreign and
        waits for it. Serialising reads widened that surface, because reads now
        take a lock they did not before.

        Not reachable today, and the reason is structural rather than a survey:
        `_critical` and `serialized_write` are private and are entered ONLY from
        inside `Store`, and no `Store` holds a reference to another `Store`
        (`class Store` contains no `Store(` call and no `Store` attribute), so
        no call chain can hold two sections at once. If one ever needs to, the
        rule is a fixed global lock ORDER, not a cleverer exemption — an
        exemption keyed on the holder can never distinguish a cycle from
        ordinary contention.
        """
        held = _in_critical.get()
        me = asyncio.current_task()
        for store, owner in held:
            if store is not self:
                continue
            if owner is me:
                yield                      # genuine reentrancy: our own nesting
                return
            raise RuntimeError(
                "Store._critical: this asyncio task inherited another task's "
                "critical-section exemption for this Store, which means a Task "
                "was created (or asyncio.wait_for was called) inside the "
                "section. Continuing would run this statement on the shared "
                "connection with no lock held while the parent still holds it "
                "— the race the lock exists to stop. Move the Store call out "
                "of the critical section, or give the child task its own Store."
            )
        async with self._write_lock:
            token = _in_critical.set(held | {(self, me)})
            try:
                yield
            finally:
                _in_critical.reset(token)

    async def _fetchone(self, sql: str, params: Any = ()) -> Any:
        """Read one row and release the cursor before returning."""
        async with self._critical():
            cur = await self.db.execute(sql, params)
            try:
                return await cur.fetchone()
            finally:
                await cur.close()

    async def _fetchall(self, sql: str, params: Any = ()) -> list[Any]:
        """Read all rows and release the cursor before returning."""
        async with self._critical():
            cur = await self.db.execute(sql, params)
            try:
                return await cur.fetchall()
            finally:
                await cur.close()

    # The same two, for readers OUTSIDE this module. `doctor.py`,
    # `context/sessions.py` and the board's event search all query the shared
    # connection directly, and a raw `store.db.execute(...)` there is exactly as
    # dangerous as one in here — more so for `context/sessions.py`, which runs on
    # the task path while the pool is writing. Route them through the same
    # critical section and the same guaranteed close.

    async def query(self, sql: str, params: Any = ()) -> list[Any]:
        """Run a read and return every row, cursor released."""
        return await self._fetchall(sql, params)

    async def query_one(self, sql: str, params: Any = ()) -> Any:
        """Run a read and return the first row (or None), cursor released."""
        return await self._fetchone(sql, params)

    @serialized_write
    async def _migrate(self) -> None:
        # Fail CLOSED. `Path.glob` on a directory that does not exist does not
        # raise — it yields nothing — so the natural spelling of this loop is a
        # fail-open in the one place that must not have one: zero migrations
        # runs cleanly, creates no schema, and hands the caller a connection to
        # an empty database. The first symptom then surfaces two frames later
        # in `_ensure_task_columns` as `no such table: tasks`, which names
        # neither the real cause nor the path that was searched.
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not sql_files:
            raise RuntimeError(
                f"no_human cannot create its database schema: no *.sql "
                f"migrations found in {MIGRATIONS_DIR}. This installation is "
                f"incomplete — the migrations are part of the package and "
                f"should have been installed alongside it. Reinstall no_human "
                f"(or, in a source checkout, verify that the repo's "
                f"migrations/ directory is present)."
            )
        for sql_file in sql_files:
            await self.db.executescript(sql_file.read_text())
        await self._ensure_task_columns()
        await self.db.commit()

    async def _ensure_task_columns(self) -> None:
        """Add columns that SQLite cannot create idempotently in a .sql file
        (no ADD COLUMN IF NOT EXISTS). Safe to run on every connect."""
        existing = {row["name"]
                    for row in await self._fetchall("PRAGMA table_info(tasks)")}
        wanted = {
            "kind": "TEXT DEFAULT 'feature'",
            "linked_repos": "TEXT",  # JSON list of additional repo paths
            "parent_id": "TEXT",  # LeadAgent: compound task sub-task linkage
            # Cooperative cancellation. A dedicated column, NOT task.context:
            # the CLI and the running orchestrator both hold a Task copy, and
            # `update_task` rewrites the whole mutable surface from it — so a
            # flag in `context` is clobbered by whichever writer flushes last.
            # `update_task`'s column list deliberately omits this one, leaving
            # the CLI its sole writer and the orchestrator its sole consumer.
            "cancel_requested": "TEXT",  # reason, or NULL for "keep running"
        }
        for col, decl in wanted.items():
            if col not in existing:
                await self.db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)"
        )
        # Phase 7d: cache metric columns on attempts (validates Phase 2a caching).
        att_existing = {row["name"]
                        for row in await self._fetchall(
                            "PRAGMA table_info(attempts)")}
        att_wanted = {
            "cache_read_tokens": "INTEGER DEFAULT 0",
            "cache_creation_tokens": "INTEGER DEFAULT 0",
            # The REVIEWER's burn, in its own columns. It was thrown away after the verdict,
            # so the DB held only the coder's tokens and no cost surface could price the gate
            # (59 Opus-4-8 runs over full diffs, costing nothing on the record). Separate from
            # the coder's so by_tier/by_profile keep attributing coder spend to the coder.
            "review_tokens_used": "INTEGER DEFAULT 0",
            "review_cache_read_tokens": "INTEGER DEFAULT 0",
            "review_cache_creation_tokens": "INTEGER DEFAULT 0",
            # PLANNING burn (single planner, MoA proposers, aggregator): ran on
            # separate readonly backends and was persisted NOWHERE — the docs
            # even claimed it lived "inside the coder's session" (ARCH_REVIEW
            # #5; ~917k cache-read priced at $0 on one measured task). Written
            # once, onto the attempt row of the attempt the plan fed.
            "plan_tokens_used": "INTEGER DEFAULT 0",
            "plan_cache_read_tokens": "INTEGER DEFAULT 0",
            "plan_cache_creation_tokens": "INTEGER DEFAULT 0",
            # UTILITY-tier burn — discarded entirely before B2 #6. It used to
            # mean "everything that is not coder/reviewer/planner", which is a
            # residual and not a role: the supervisor and the context
            # distiller billed into it alongside the stuck hypothesis, the
            # spec evaluator, the assumption pass, both grill halves and the
            # split drafter. Since A5 those first two have columns of their
            # own (below) and this column means the INTAKE/advisory utility
            # tier only. Historical rows keep whatever they were written with;
            # nothing is moved, and the grand total is unchanged either way.
            "utility_tokens_used": "INTEGER DEFAULT 0",
            "utility_cache_read_tokens": "INTEGER DEFAULT 0",
            "utility_cache_creation_tokens": "INTEGER DEFAULT 0",
            # SUPERVISOR burn: the every-`check_every`-tool-calls course
            # corrector (`agent/supervisor.py`, `llm.supervisor_model`). It
            # runs once per N tool calls for the whole length of an
            # implementation session, so it is the one aux role whose cost
            # scales with attempt LENGTH rather than with intake — exactly the
            # thing a cost optimiser needs to see on its own before it starts
            # tuning `check_every`. Folded into `utility_` it was
            # indistinguishable from a one-shot spec evaluator.
            "supervisor_tokens_used": "INTEGER DEFAULT 0",
            "supervisor_cache_read_tokens": "INTEGER DEFAULT 0",
            "supervisor_cache_creation_tokens": "INTEGER DEFAULT 0",
            # CONTEXT-DISTILLATION burn: one utility-model session per
            # oversized gathered chunk (`_distill_large_chunks`). Unbounded in
            # the number of chunks and paid BEFORE the coder writes a line, so
            # it is the other half of the old `utility_` residual that has to
            # be separable — "distillation pays for itself" is a claim about
            # this column against the coder's, and it could not be stated,
            # let alone tested, while the two aux roles shared one bucket.
            "distill_tokens_used": "INTEGER DEFAULT 0",
            "distill_cache_read_tokens": "INTEGER DEFAULT 0",
            "distill_cache_creation_tokens": "INTEGER DEFAULT 0",
            # The OUTPUT share of the `*tokens_used` column beside each one.
            # `_usage_quad` in the backend always had this number and the
            # backend summed it away (`input_tokens + output_tokens`) before
            # anything downstream could see it, so output — which bills ~5x
            # input — was priced at the input rate everywhere: the stats
            # dollars, the cost tiles, and the lifetime brake.
            #
            # A SUBSET, not a fourth addend: `tokens_used` keeps meaning
            # input+output, exactly as all 52 files that read it already
            # assume, and this says how much of that total was output. Input
            # is `tokens_used - output_tokens`. One source of truth for the
            # total, so the two can never drift into disagreeing about it.
            #
            # NO `DEFAULT 0`, unlike every column above — and that is the
            # whole point of them being separate lines. ADD COLUMN backfills
            # the declared default, so `DEFAULT 0` would stamp "this attempt
            # emitted no output tokens" onto every row in the ledger. The
            # split was discarded AT CAPTURE; there is nothing to backfill
            # from and there never will be. NULL reads "unknown" and prices at
            # the old rate; 0 reads "free" and is a lie. A 0 written for an
            # unreported field is how a per-attempt brake went inert on 27 of
            # 27 tasks once already.
            "output_tokens": "INTEGER",
            "review_output_tokens": "INTEGER",
            "plan_output_tokens": "INTEGER",
            "utility_output_tokens": "INTEGER",
            "supervisor_output_tokens": "INTEGER",
            "distill_output_tokens": "INTEGER",
            # Which model actually ran which role on this attempt. Nothing
            # recorded it, which is how a frozen config.yaml silently inverted
            # coder and reviewer for a week.
            "models": "TEXT DEFAULT '{}'",
            # Which subscription paid for this attempt (profile name, never a
            # token). NULL on attempts that predate auth profiles.
            "auth_profile": "TEXT",
            # Which team-brain version this attempt read remote rules AS OF,
            # pinned once at attempt start. NULL whenever the feature is off,
            # which is the default and every attempt before it existed.
            # Deliberately a SECOND column rather than folded into
            # auth_profile: they answer different questions — who paid, and
            # what the agent knew — and one column cannot answer both.
            "brain_watermark": "INTEGER",
            # The checkpoint this attempt was supposed to resume from and could
            # not, plus what it did instead. NULL on every attempt that resumed
            # normally or never had a checkpoint, which is almost all of them.
            # Its own column rather than `failure_reason`: the attempt is not
            # failed — it branched from base and may well open a PR — and
            # writing "why did this fail" on a succeeding attempt would put a
            # red line under it on every surface that prints that column.
            "resume_checkpoint_lost": "TEXT",
            # Which CODE produced this attempt's verdict — the sha of what the
            # server actually has IN MEMORY, not HEAD at query time. The server
            # loads the backend once; merging a fix to main does not reload it,
            # so a verdict from superseded code was indistinguishable from a
            # verdict from the fix (task ecfe1789 escalated on a tamper-guard
            # false positive 3h18m after the commit that fixed that exact false
            # positive had merged). With this stamped on the row, such an
            # escalation can be RE-JUDGED afterwards instead of being charged
            # to the ticket — which is also what was corrupting the dogfood
            # success measurement. See core/build_info.py for the format and
            # for why this records rather than blocks.
            "loaded_code_version": "TEXT",
        }
        for col, decl in att_wanted.items():
            if col not in att_existing:
                await self.db.execute(f"ALTER TABLE attempts ADD COLUMN {col} {decl}")
        # D2 #3 curator: memories gain a recoverable archive flag — the
        # curator NEVER deletes (broker invariant); archived rows leave the
        # pending queue but stay queryable.
        mem_existing = {row["name"]
                        for row in await self._fetchall(
                            "PRAGMA table_info(memories)")}
        if "archived" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN archived INTEGER DEFAULT 0")
        # B2: WHICH SIGNAL produced a proposal — "review" (a reviewer FAIL
        # round's findings, B1) or "supervisor" (a recurring supervisor
        # `correct` decision). A SECOND column rather than reusing `source`,
        # for the same reason `brain_watermark` is not folded into
        # `auth_profile`: they answer different questions. `source` is the
        # queue-VISIBILITY contract — `pending()`, `nh learnings`, the API and
        # the ingester all select `source="proposed"` — so a proposal that
        # named its provenance there would be invisible to the human gate it
        # exists for.
        #
        # NO DEFAULT, deliberately. ADD COLUMN backfills the declared default,
        # and stamping "review" (or any other value) onto every pre-existing
        # row would be inventing provenance for rows that genuinely do not
        # record it. NULL reads "unknown", which is the truth.
        if "origin" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN origin TEXT")
        # B3: STRUCTURED EVIDENCE — what happened, in which task, citing the
        # correction/review event — as JSON, beside the human-prose `content`
        # that already narrates it. NO DEFAULT, same reasoning as `origin`:
        # rows written before the column genuinely did not record structured
        # evidence, and NULL says so honestly.
        if "evidence" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN evidence TEXT")
        # B4: the PROJECT SCOPE — "prj:" + sha256 of the normalized git remote
        # URL (learning/scope.py) — so the same repository cloned at two paths
        # is one project. `project` keeps the checkout path (the human-readable
        # blast-radius line in `nh learnings`); this column is the identity
        # recall matches on. NULL = legacy row or a repo with no remote; those
        # keep matching by path, and `stamp_project_scope` upgrades them the
        # next time their repo is actually seen — the only moment the
        # path→remote mapping is knowable.
        if "project_scope" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN project_scope TEXT")
        # S2: WHEN this memory was last INJECTED into a prompt — stamped by
        # `Orchestrator._load_active_memories`, the one place a task turns into
        # an active rule set. It answers the question the confirm queue cannot:
        # of the rules a human already confirmed, which have ever done anything?
        #
        # NO DEFAULT, same reasoning as `origin` and `evidence`: a row written
        # before the column genuinely has no usage history, and backfilling
        # `datetime('now')` would stamp every legacy rule as freshly used —
        # inventing the exact fact `nh learnings --stale` exists to report.
        # NULL reads "never seen used", which is the truth for a legacy row and
        # for a rule that has genuinely never triggered; `--stale` says which
        # of the two it cannot tell apart rather than guessing.
        if "last_used_at" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN last_used_at TEXT")

        # Phase 6a: test_layers column on projects (JSON-encoded TestPlan layers).
        proj_existing = {row["name"]
                         for row in await self._fetchall(
                             "PRAGMA table_info(projects)")}
        if "test_layers" not in proj_existing:
            await self.db.execute(
                "ALTER TABLE projects ADD COLUMN test_layers TEXT DEFAULT '[]'"
            )
        # Phase 7e: history cache table — content-signature keyed so onboarding
        # doesn't re-extract every request. "Re-scan" forces refresh.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS history_cache (
                content_sig TEXT PRIMARY KEY,
                cascade_id TEXT NOT NULL,
                title TEXT,
                findings_json TEXT,
                ingested_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Real spend that NO ATTEMPT ROW can own. Two sources, both intake:
        #
        #  * The interactive grill (`nh task add --grill`, the board's
        #    /api/grill endpoints) runs BEFORE a task exists, so
        #    `attempts.utility_*` is not merely the wrong column — there is no
        #    row, and often no task ever (the operator can walk away mid-
        #    wizard). Those rows carry task_id NULL.
        #  * Pre-attempt intake on a task that never reached an attempt (parked
        #    at the plan gate, escalated on an unavailable input, decomposed).
        #    The task id IS known, so those rows carry it — but no attempt
        #    spent it, and inventing an attribution is how a cost surface
        #    starts lying.
        #
        # `site` says which, per row, so the residual stays diagnosable instead
        # of being one anonymous number.
        #
        # DELIBERATELY NOT summed into per-task cost (`lifetime_usage`,
        # `eval/northstar`): those answer "what did THIS task cost", and this
        # table is by construction the spend no attempt owns. It is the
        # whole-cost residual — read it for the true total, not the per-task
        # one. `nh status` prints it whenever it is non-zero.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS unattributed_usage (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                site TEXT NOT NULL,
                model TEXT,
                task_id TEXT,
                tokens_used INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0
            )
        """)
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_unattributed_usage_task "
            "ON unattributed_usage(task_id)"
        )

    # ----------------------------- tasks ---------------------------------- #

    @serialized_write
    async def create_task(self, task: Task) -> Task:
        row = task.to_row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        await self.db.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", row
        )
        await self.db.commit()
        return task

    async def get_task(self, task_id: str) -> Task | None:
        row = await self._fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return Task.from_row(dict(row)) if row else None

    async def find_task(self, prefix: str) -> Task | None:
        """Resolve a task by full id or a unique id prefix (CLI convenience)."""
        rows = await self._fetchall(
            "SELECT * FROM tasks WHERE id = ? OR id LIKE ? LIMIT 2",
            (prefix, prefix + "%"),
        )
        if len(rows) == 1:
            return Task.from_row(dict(rows[0]))
        return None

    async def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        if status is not None:
            rows = await self._fetchall(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            )
        else:
            rows = await self._fetchall(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            )
        return [Task.from_row(dict(r)) for r in rows]

    async def get_task_by_source_external_id(
        self, source: str, external_id: str
    ) -> Task | None:
        """Filtered dedupe lookup for any external-source intake (Slack, and
        usable by Jira too) — one indexed-shape query instead of hydrating
        every task via `list_tasks()` just to scan for a match."""
        row = await self._fetchone(
            "SELECT * FROM tasks WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        )
        return Task.from_row(dict(row)) if row else None

    async def list_imported_tasks(self, source: str) -> list[ImportedTaskRow]:
        """Narrow projection for the backlog picker's imported-chip lookup
        (SCRUM-54): only (external_id, id, status, created_at) for tasks from
        ONE tracker with a linked external_id, via one filtered SQL query —
        never a full `list_tasks()` hydration of every task's every column just
        to read four fields.

        `source` is a parameter, not a literal, because the picker now lists
        two trackers: dedupe keys on (source, external_id), so a Jira NO-1 must
        not make a Linear NO-1 look already-imported. Bound as a SQL parameter
        like every other value here — never interpolated."""
        rows = await self._fetchall(
            "SELECT external_id, id, status, created_at FROM tasks "
            "WHERE source = ? AND external_id IS NOT NULL",
            (source,),
        )
        return [
            ImportedTaskRow(
                external_id=r["external_id"], id=r["id"],
                status=r["status"], created_at=r["created_at"],
            )
            for r in rows
        ]

    @serialized_write
    async def set_status(
        self,
        task: Task,
        new_status: TaskStatus,
        *,
        validate: bool = True,
        human_override: bool = False,
    ) -> Task | None:
        """Transition a task, enforcing the legal-transition map by default.

        CAS guard (SCRUM-73): the WHERE clause is checked against the live DB
        row inside this one statement, not the possibly-stale `task.status`
        this caller is holding — a worker coroutine can hold IMPLEMENTING
        while a human's `shipped` verb already wrote DONE, and
        IMPLEMENTING->REVIEWING passes `assert_transition` on the stale
        value. Terminal here means the row reads DONE, or reads FAILED with a
        `cancel_reason` recorded in context (an explicit human cancel, not a
        plain failure) — a plain FAILED row stays writable so `nh task retry`
        / `POST /api/tasks/{id}/retry` keep working. Once a row is terminal,
        only a write that keeps its status unchanged may land; every other
        write (including validate=False ones) is a no-op that returns None.

        `human_override=True` bypasses the guard entirely — reserved for the
        human verbs that are allowed to move a row OUT of a terminal state
        (retry, cancel, shipped). Every other call site (watcher,
        orchestrator, scheduler, pipeline) must leave it at the default so a
        stale in-process handle can never clobber a human's terminal write.
        """
        if validate:
            assert_transition(task.status, new_status)
        now = _now()
        if human_override:
            cur = await self.db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_status.value, now, task.id),
            )
        else:
            cur = await self.db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? "
                "WHERE id = ? AND ("
                "  status = ?"
                "  OR NOT ("
                "    status = ?"
                "    OR (status = ? AND json_extract(context, '$.cancel_reason') IS NOT NULL)"
                "  )"
                ")",
                (
                    new_status.value, now, task.id,
                    new_status.value,
                    TaskStatus.DONE.value, TaskStatus.FAILED.value,
                ),
            )
        await self.db.commit()
        if cur.rowcount == 0:
            row = await self._fetchone(
                "SELECT status FROM tasks WHERE id = ?", (task.id,)
            )
            if row is not None:
                log.warning(
                    "set_status: blocked %s -> %s on terminal row %s",
                    row["status"], new_status.value, task.id,
                )
                task.status = TaskStatus(row["status"])
            return None
        task.status = new_status
        task.updated_at = now
        return task

    @serialized_write
    async def update_task(self, task: Task) -> Task:
        """Persist the full mutable surface of a task row.

        CAS guard (SCRUM-73): mirrors set_status's terminal definition (done,
        or failed with a `cancel_reason` in context) — a terminal row's
        status column is protected from being resurrected by a stale
        in-memory `task.status`, via a CASE keyed on the row's own
        pre-update status/context (evaluated atomically inside this one
        statement, before this call's own :context write applies). Every
        other column still writes normally, so e.g. the Jira poller can keep
        updating context write-back markers on an already-DONE row. No
        override parameter here — callers that must move a row OUT of a
        terminal state go through set_status(..., human_override=True)
        instead, since update_task never carries that intent.
        """
        task.updated_at = _now()
        row = task.to_row()
        await self.db.execute(
            """UPDATE tasks SET
                 external_id=:external_id, source=:source, title=:title,
                 description=:description, requirements=:requirements,
                 acceptance_criteria=:acceptance_criteria, repo_path=:repo_path,
                 kind=:kind, parent_id=:parent_id,
                 status = CASE
                            WHEN (
                              status = 'done'
                              OR (status = 'failed'
                                  AND json_extract(context, '$.cancel_reason') IS NOT NULL)
                            ) AND status != :status
                            THEN status ELSE :status END,
                 blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority, context=:context, plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        # Deliberately NOT `UPDATE … RETURNING status`. A writer that has
        # produced a row leaves its VDBE live between `execute()` and the fetch,
        # and every `await` in that gap is a scheduling point: SQLite refuses any
        # COMMIT while a write statement is in progress ("cannot commit
        # transaction - SQL statements in progress"). That was this method's
        # half of KI-1. The write lock alone would close it, but only while the
        # ONLY thing that can reach this connection is a lock-taking Store
        # method, and only because CPython's refcounting happens to finalize the
        # abandoned cursor if this frame unwinds (a cancellation, an exception) —
        # measured, not assumed, but an implementation detail no invariant should
        # rest on. A plain UPDATE parks nothing. The read-back below is inside
        # the same uncommitted transaction and the same critical section, so it
        # observes exactly what RETURNING did.
        result = await self._fetchone(
            "SELECT status FROM tasks WHERE id = ?", (task.id,)
        )
        await self.db.commit()
        if result is not None and result["status"] != row["status"]:
            log.warning(
                "update_task: blocked status %s -> %s on terminal row %s",
                result["status"], row["status"], task.id,
            )
            task.status = TaskStatus(result["status"])
        return task

    @serialized_write
    async def merge_context(self, task_id: str, patch: dict) -> dict:
        """Atomically merge *patch* into the task's context (RFC 7396).

        The lost-update fix for concurrent context writers: `update_task`
        rewrites the whole context blob from a Task copy, so the watcher, the
        CLI and the orchestrator (different coroutines AND different
        processes) clobber each other — whichever flushes last wins (the
        cancel_requested column above documents the same failure). A single
        `json_patch` UPDATE is atomic under SQLite's write serialization, so
        concurrent merges of different keys both survive, across processes.

        Semantics (RFC 7396): nested dicts merge recursively; lists/scalars
        replace; a ``None`` value DELETES the key. Returns the merged context.
        """
        await self.db.execute(
            """UPDATE tasks SET
                 context = json_patch(COALESCE(context, '{}'), ?),
                 updated_at = ?
               WHERE id = ?""",
            (json.dumps(patch), _now(), task_id),
        )
        await self.db.commit()
        row = await self._fetchone(
            "SELECT context FROM tasks WHERE id = ?", (task_id,))
        return json.loads(row[0]) if row and row[0] else {}

    @serialized_write
    async def append_context_list(self, task_id: str, key: str, item: dict) -> None:
        """Atomically append *item* to the context list at *key* (created if
        absent). List appends cannot be expressed as a merge patch (RFC 7396
        replaces arrays wholesale), so this uses json_set's '[#]' append —
        one UPDATE, no read-modify-write."""
        assert "." not in key and "[" not in key, "flat keys only"
        await self.db.execute(
            f"""UPDATE tasks SET
                 context = json_set(
                   json_patch(COALESCE(context, '{{}}'),
                              CASE WHEN json_extract(COALESCE(context,'{{}}'),
                                        '$.{key}') IS NULL
                                   THEN json_object('{key}', json_array())
                                   ELSE '{{}}' END),
                   '$.{key}[#]', json(?)),
                 updated_at = ?
               WHERE id = ?""",
            (json.dumps(item), _now(), task_id),
        )
        await self.db.commit()

    @serialized_write
    async def update_task_columns(self, task: Task) -> Task:
        """Persist the task's mutable columns EXCEPT context. Multi-writer
        zones (watcher, CLI, gate) must write context only via merge_context/
        append_context_list — this companion writes the rest without
        clobbering concurrent context merges with a stale blob."""
        task.updated_at = _now()
        row = task.to_row()
        await self.db.execute(
            """UPDATE tasks SET
                 external_id=:external_id, source=:source, title=:title,
                 description=:description, requirements=:requirements,
                 acceptance_criteria=:acceptance_criteria, repo_path=:repo_path,
                 kind=:kind, parent_id=:parent_id,
                 status=:status, blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority, plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        await self.db.commit()
        return task

    @serialized_write
    async def request_cancel(self, task_id: str, reason: str) -> None:
        """Ask a running task to stop at its next cooperative checkpoint.

        A targeted UPDATE of one column: it must not read-modify-write the task
        row, or it would race the orchestrator that owns every other column.
        """
        await self.db.execute(
            "UPDATE tasks SET cancel_requested = ? WHERE id = ?", (reason, task_id)
        )
        await self.db.commit()

    async def get_cancel_request(self, task_id: str) -> str | None:
        """The pending cancellation reason for *task_id*, or None."""
        row = await self._fetchone(
            "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
        )
        return row["cancel_requested"] if row else None

    @serialized_write
    async def clear_cancel_request(self, task_id: str) -> None:
        """Drop a pending cancellation, once honoured or withdrawn."""
        await self.db.execute(
            "UPDATE tasks SET cancel_requested = NULL WHERE id = ?", (task_id,)
        )
        await self.db.commit()

    async def list_subtasks(self, parent_id: str) -> list[Task]:
        """Return all sub-tasks of a compound parent task."""
        rows = await self._fetchall(
            "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at",
            (parent_id,),
        )
        return [Task.from_row(dict(r)) for r in rows]

    async def count_subtasks(self, parent_id: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_id = ?", (parent_id,)
        )
        return int(row["n"]) if row else 0

    # ---------------------------- attempts --------------------------------- #

    @serialized_write
    async def create_attempt(self, task_id: str, attempt_number: int) -> str:
        # Read BEFORE the UPDATE below opens a write transaction. `connect()`
        # has already warmed this, so it is a cached attribute read — but
        # ordering it here means even a cold cache cannot shell out to git
        # while the write lock is held.
        from .build_info import loaded_code
        code_version = loaded_code().descriptor
        # An earlier attempt of this task still 'in_progress' cannot be running:
        # attempts are serial, so a new one starting means the old process died
        # (kill -9, crash) without ever closing its row. Left alone, those rows
        # make `attempts.status` untrustworthy as a completion signal — the
        # baseline had three of them. Close them for what they are.
        await self.db.execute(
            "UPDATE attempts SET status = 'interrupted', "
            "failure_reason = COALESCE(NULLIF(TRIM(failure_reason), ''), "
            "'interrupted: superseded by a newer attempt — the prior worker "
            "process died without closing its row') "
            "WHERE task_id = ? AND status = 'in_progress' AND attempt_number < ?",
            (task_id, attempt_number),
        )
        attempt_id = uuid.uuid4().hex
        # Stamped HERE, at the single chokepoint every attempt passes through,
        # rather than at each of the orchestrator's three creation sites — a
        # site added later would otherwise silently record nothing, and an
        # attempt with no provenance is exactly the row this exists to prevent.
        # `code_version` was resolved above, outside the write transaction.
        await self.db.execute(
            "INSERT INTO attempts (id, task_id, attempt_number, "
            "loaded_code_version) VALUES (?, ?, ?, ?)",
            (attempt_id, task_id, attempt_number, code_version),
        )
        await self.db.commit()
        return attempt_id

    @serialized_write
    async def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        if not fields:
            return
        # Observability backstop (C2): a failed attempt with no stated reason
        # is undiagnosable — task 6cfdb936 burned attempts on exactly that.
        # When the caller marks failed without a reason AND the row has none,
        # stamp a loud sentinel instead of leaving silence. Never clobbers a
        # reason set by an earlier update.
        if fields.get("status") == "failed" and not fields.get("failure_reason"):
            fields.pop("failure_reason", None)
            row = await self._fetchone(
                "SELECT COALESCE(failure_reason, '') FROM attempts WHERE id = ?",
                (attempt_id,))
            if row is not None and not row[0].strip():
                fields["failure_reason"] = (
                    "(no failure reason recorded — observability gap; "
                    "report which stage failed silently)")
        # JSON-encode dict/list values transparently.
        clean = {
            k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in fields.items()
        }
        assignments = ", ".join(f"{k} = :{k}" for k in clean)
        clean["id"] = attempt_id
        await self.db.execute(
            f"UPDATE attempts SET {assignments} WHERE id = :id", clean
        )
        await self.db.commit()

    async def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number",
            (task_id,),
        )
        return [dict(r) for r in rows]

    async def attempts_by_task(self) -> dict[str, list[dict[str, Any]]]:
        """All attempts, grouped by task — ONE query.

        B2 #16: the board issued list_attempts PER TASK, every 2 seconds, per
        connected socket (an N+1 over the whole task history on every tick).
        """
        all_rows = await self._fetchall(
            "SELECT * FROM attempts ORDER BY task_id, attempt_number")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in all_rows:
            row = dict(r)
            grouped.setdefault(row["task_id"], []).append(row)
        return grouped

    async def count_attempts(self, task_id: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM attempts WHERE task_id = ?", (task_id,)
        )
        return int(row["n"]) if row else 0

    # The named roles the attempts table meters, and the three token columns
    # each one carries. `eval/northstar.py` already sums exactly this set to
    # report cost; the budget gate below matches it, so the two can no longer
    # disagree about what a task spent.
    #
    # DERIVED from the module-level `USAGE_ROLES` registry, never re-typed:
    # this list was four literals in six different files, and the last time a
    # role was added (planning) four of the six kept summing three. Adding a
    # role to the registry now widens every one of them at once.
    _USAGE_TIERS = tuple(USAGE_ROLES)

    @classmethod
    def _usage_columns_by_class(cls) -> dict[str, tuple[str, ...]]:
        """The same addend columns, grouped by PRICE CLASS rather than by role.

        Every role bills at the same three rates, so the classes — not the
        roles — are what a cost-weighted budget has to keep apart
        (``core.pricing``). Keyed by the coder-tier column name so a caller can
        splat the result straight into ``pricing.weighted_tokens``.

        Three classes x ``len(USAGE_ROLES)`` roles; the count moves when a
        role is registered, which is why nothing here or downstream states it
        as a literal any more.
        """
        return {
            "tokens_used": tuple(
                "tokens_used" if tier == "" else f"{tier}tokens_used"
                for tier in cls._USAGE_TIERS
            ),
            "cache_read_tokens": tuple(
                f"{tier}cache_read_tokens" for tier in cls._USAGE_TIERS),
            "cache_creation_tokens": tuple(
                f"{tier}cache_creation_tokens" for tier in cls._USAGE_TIERS),
        }

    @classmethod
    def _usage_columns(cls) -> tuple[str, ...]:
        # Derived, never re-listed: a column added to one of these and not the
        # other would make the raw total and the weighted total disagree about
        # what the task spent.
        return tuple(
            col for cols in cls._usage_columns_by_class().values() for col in cols
        )

    @classmethod
    def _output_columns_by_class(cls) -> dict[str, tuple[str, ...]]:
        """The output SHARE of the four ``*tokens_used`` columns.

        Kept OUT of ``_usage_columns_by_class`` on purpose, and this is the
        one thing to understand before editing either method. Those three
        classes are ADDENDS — ``lifetime_usage`` sums them to get the raw
        token total. ``output_tokens`` is not an addend; it is a slice of the
        ``tokens_used`` addend, already inside it. Folding it in would count
        every output token twice and silently inflate the raw figure that
        ``nh``, the web surfaces and ``eval/northstar.py`` all print.

        It rides along in ``lifetime_usage_by_class`` anyway, because the
        WEIGHTED path does need it: ``pricing.weighted_tokens`` charges it
        ``OUTPUT_EXTRA_WEIGHT`` — the premium over the 1.0 that
        ``tokens_used`` already applied — so the splat keeps working and the
        total is priced once.
        """
        return {
            "output_tokens": tuple(
                "output_tokens" if tier == "" else f"{tier}output_tokens"
                for tier in cls._USAGE_TIERS
            ),
        }

    async def lifetime_usage_by_class(
        self, task_id: str
    ) -> tuple[int, dict[str, int]]:
        """(attempts, {tokens_used, cache_read_tokens, cache_creation_tokens,
        output_tokens}).

        The same rows and the same addend columns ``lifetime_usage`` sums, kept
        in their three price classes so the budget gate can weight them
        (``core.pricing.weighted_tokens``), plus a FOURTH key that is not a
        fourth class: ``output_tokens`` is the output slice of ``tokens_used``,
        carried here so the splat into ``weighted_tokens`` can charge it the
        output premium. It is deliberately absent from ``lifetime_usage``'s
        raw total, which would otherwise count it twice. The classes are
        summed across all
        registered roles (``USAGE_ROLES``: coder, reviewer, planner,
        utility, supervisor, distill) because they all bill at the same three
        rates. For the same numbers cut by ROLE instead, see
        ``lifetime_usage_by_role`` — it partitions the identical column set,
        so the two always agree on the total.
        """
        # The three raw classes PLUS the output share, which is a slice of the
        # first of them rather than a fourth class — see
        # `_output_columns_by_class`. The inner `COALESCE(col, 0)` is what
        # makes a NULL split cost nothing extra instead of poisoning the SUM:
        # an attempt whose split was never recorded prices exactly as it did
        # before the column existed, which is the honest treatment of
        # "unknown" and the only one available (there is no backfill).
        wanted = {**self._usage_columns_by_class(), **self._output_columns_by_class()}
        selects = ", ".join(
            "COALESCE(SUM({}), 0) AS {}".format(
                " + ".join(f"COALESCE({c}, 0)" for c in cols), name)
            for name, cols in wanted.items()
        )
        row = await self._fetchone(
            f"SELECT COUNT(*) AS n, {selects} FROM attempts WHERE task_id = ?",
            (task_id,),
        )
        if not row:
            return (0, {name: 0 for name in wanted})
        return (int(row["n"]), {name: int(row[name]) for name in wanted})

    async def lifetime_usage(self, task_id: str) -> tuple[int, int]:
        """(attempts, tokens) spent over the task's WHOLE life, resumes included.

        Tokens = everything the attempt metered: in/out, cache reads AND cache
        creation, across every registered role (``USAGE_ROLES``: coder,
        reviewer, planner, utility, supervisor, distill). Cache reads are where the bulk of the burn lives (~83%), but
        this used to sum ONLY the coder's ``tokens_used + cache_read_tokens``
        — 2 columns out of the whole grid. The gate was therefore blind to every
        reviewer, planner and utility token, and to cache creation everywhere. Measured
        over 574 real attempt rows that blind spot is 16.2% of true spend, and
        a task whose burn was mostly reviewer or utility could never trip the
        cap at all. Cache creation is billed, so a spend gate must count it.

        Interrupted/killed rows count: they spent the attempt even if their
        token columns under-report (pre-1638427 rows recorded zero).

        RAW, and deliberately still raw: this is the burn figure `nh`, the web
        surfaces and `eval/northstar.py` all report, and it must keep matching
        them token for token. The BUDGET gate no longer compares against it —
        it uses ``lifetime_usage_by_class`` and weights the classes by price
        (``core.pricing``), because a raw sum bounds conversation length, not
        spend. Computed from the same one query so the two cannot drift.

        Sums the three ADDEND classes only. ``lifetime_usage_by_class`` also
        returns ``output_tokens``, which is a slice of ``tokens_used`` and not
        a bucket beside it; `sum(by_class.values())` would double-count it and
        move a number this docstring promises will keep matching every surface
        token for token.
        """
        attempts, by_class = await self.lifetime_usage_by_class(task_id)
        return attempts, sum(
            by_class[name] for name in self._usage_columns_by_class()
        )

    async def lifetime_usage_by_role(
        self, task_id: str
    ) -> dict[str, dict[str, int]]:
        """``{role: {tokens_used, cache_read_tokens, cache_creation_tokens,
        output_tokens, total}}`` over the task's whole life.

        The SAME rows and the SAME columns ``lifetime_usage`` sums, cut by
        NAMED ROLE instead of by price class. That is the whole point and the
        one invariant to preserve when editing either: both partition
        ``_usage_columns()``, so

            sum(r["total"] for r in by_role.values()) == lifetime_usage()[1]

        exactly, for every task, with no residual — a role's spend can move
        between buckets but can never leave the total.
        ``test_role_token_accounting.py`` asserts both halves (structurally,
        over the column sets, and on real rows).

        What that identity does NOT catch, stated so nobody reads more safety
        into it than is there: both sides of it derive from ``USAGE_ROLES``,
        so they narrow TOGETHER. A metered column added to the `attempts`
        schema under a prefix no role registers is unclaimed by this method
        AND absent from ``_usage_columns()``, and the sum still reconciles
        while the spend is silently uncounted. The only guard that sees that
        is one anchored to the SCHEMA rather than to the registry —
        ``test_no_metered_column_in_the_schema_is_unclaimed``, which reads
        `PRAGMA table_info(attempts)`.

        ``total`` is the three ADDENDS only. ``output_tokens`` rides along as
        a fifth key because callers pricing a role need it, but it is a SLICE
        of ``tokens_used``, not a fourth addend — adding it in would
        double-count output and break the identity above.

        Roles are reported even at zero, so a caller rendering a breakdown
        gets a stable shape and an operator can see that the supervisor cost
        nothing rather than wondering whether it was measured.
        """
        cols: dict[str, tuple[str, ...]] = {}
        for tier in USAGE_ROLES:
            cols[tier] = usage_columns_for(tier) + (
                "output_tokens" if tier == "" else f"{tier}output_tokens",)
        selects = ", ".join(
            f"COALESCE(SUM(COALESCE({col}, 0)), 0) AS {col}"
            for tier_cols in cols.values() for col in tier_cols
        )
        row = await self._fetchone(
            f"SELECT {selects} FROM attempts WHERE task_id = ?", (task_id,))
        out: dict[str, dict[str, int]] = {}
        for tier, role in USAGE_ROLES.items():
            used, read, creation, output = (
                int(row[c]) if row else 0 for c in cols[tier])
            out[role] = {
                "tokens_used": used, "cache_read_tokens": read,
                "cache_creation_tokens": creation, "output_tokens": output,
                "total": used + read + creation,
            }
        return out

    # ---------------------- unattributed usage ledger ----------------------- #

    @serialized_write
    async def record_unattributed_usage(
        self, *, site: str, tokens_used: int = 0, cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0, model: str | None = None,
        task_id: str | None = None,
    ) -> str | None:
        """Book utility-tier spend that no attempt row can own.

        ``site`` names WHERE it was spent — the live values are ``"api.grill"``,
        ``"api.grill_stream"``, ``"api.grill_stream.evaluate_spec"``,
        ``"cli.task_add.grill"``, and one ``"orphaned_<tier>usage"`` per
        registered aux role (``orphaned_plan_usage``,
        ``orphaned_utility_usage``, ``orphaned_supervisor_usage``,
        ``orphaned_distill_usage``; the set is generated from
        ``AUX_USAGE_TIERS`` by ``_flush_orphaned_aux_usage``, so it widens
        with the registry) — so the residual stays diagnosable rather
        than being one anonymous number.
        Returns the row id, or None when there was nothing to record — a call
        that reports zero across all three figures writes no row, so the table
        holds spend and never padding.

        NOT YET BOOKED ANYWHERE, and this ledger is their natural home — five
        further LLM sites still record nothing, verified present as of this
        commit: the GUI transcript analyzer (`api/app.py:2925`, review tier),
        the WikiGenerator (`api/app.py:3008` + `docs_gen.py:118`,
        ``max_turns=12``), and three CLI backends (`cli/commands.py:1776`,
        `:2310`, `:3138`). Deliberately left out of this change, which is
        scoped to the six intake sites.
        """
        tokens_used = int(tokens_used or 0)
        cache_read_tokens = int(cache_read_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        if not (tokens_used or cache_read_tokens or cache_creation_tokens):
            return None
        row_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO unattributed_usage (id, ts, site, model, task_id, "
            "tokens_used, cache_read_tokens, cache_creation_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, _now(), site, model, task_id, tokens_used,
             cache_read_tokens, cache_creation_tokens),
        )
        await self.db.commit()
        return row_id

    async def unattributed_usage_totals(
        self, task_id: str | None = None
    ) -> dict[str, int]:
        """Totals over the unattributed ledger: ``{calls, tokens_used,
        cache_read_tokens, cache_creation_tokens, total}``.

        ``task_id=None`` totals the WHOLE ledger (the default question — "how
        much intake spend does no task own"); pass an id to scope it.
        """
        sql = ("SELECT COUNT(*) AS calls, "
               "COALESCE(SUM(tokens_used), 0) AS tokens_used, "
               "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
               "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens "
               "FROM unattributed_usage")
        args: tuple[Any, ...] = ()
        if task_id is not None:
            sql += " WHERE task_id = ?"
            args = (task_id,)
        row = await self._fetchone(sql, args)
        out = {k: int(row[k] if row else 0) for k in (
            "calls", "tokens_used", "cache_read_tokens", "cache_creation_tokens")}
        out["total"] = (out["tokens_used"] + out["cache_read_tokens"]
                        + out["cache_creation_tokens"])
        return out

    # --------------------------- memories ---------------------------------- #
    # The human-confirmed learning queue (PLAN.md 4.5): proposals land here
    # with confirmed=0 and never enter the active rule set until a human
    # confirms them (avoids leniency-biased lessons accumulating silently).

    @serialized_write
    async def add_memory(
        self, *, mem_type: str, title: str, content: str,
        tags: list[str] | None = None, project: str | None = None,
        source: str = "proposed", confirmed: bool = False,
        dedupe_key: str | None = None, origin: str | None = None,
        evidence: dict[str, Any] | None = None,
        project_scope: str | None = None,
    ) -> str | None:
        """Insert a memory. If ``dedupe_key`` matches an existing memory's
        signature (stored in file_path), skip and return None.

        ``origin`` records WHICH SIGNAL produced the proposal (``learning.queue``'s
        ``ORIGIN_REVIEW`` / ``ORIGIN_SUPERVISOR``); it is not ``source``, which is
        the queue-visibility contract. NULL where unrecorded.

        ``evidence`` is the B3 structured record (what happened, in which
        task, citing the correction/review event) — stored as JSON, NULL where
        unrecorded. ``project_scope`` is the B4 project identity
        (``learning/scope.py``); NULL keeps the row on legacy path matching.

        RAISES ``ValueError`` for an unconfirmed memory whose ``source`` is not
        ``SOURCE_PROPOSED`` — see that constant for the three call sites that
        wrote one anyway. Deliberately loud rather than silently normalised: a
        guard that quietly repairs its input is a guard nobody ever notices is
        being hit, and every `source` in this repo is a literal at the call
        site, so no runtime data can reach this branch. It is a programming
        error, and it fires before any write, so a caller that trips it leaves
        the database untouched.

        THE ROWS THAT ARE ALREADY LIKE THAT. This guard closes the door; it
        does not go back for what walked through it. See the block below.
        """
        # ── STRANDED ROWS, and what this change does NOT do to them ────────
        #
        # Measured 2026-08-07 against a `cp` of the operator's live database —
        # never the live file, which a running server holds open — so the
        # numbers below are a snapshot of one install, not a property of the
        # schema. 20 rows have `confirmed = 0` and a `source` that `pending()`
        # does not select, in two shapes:
        #
        #   18 rows  source='confirmed', confirmed=0
        #            created_at  2026-07-01 13:40:03 (all 18, identical)
        #            updated_at  2026-07-01T13:40:39.458782+00:00
        #                     …  2026-07-01T13:40:39.467533+00:00
        #            origin NULL, archived 0
        #    2 rows  source='reply', confirmed=0
        #            created 2026-07-26 23:53:41 and 2026-07-27 00:14:28
        #
        # The 2 reply rows have a known producer: `nh reply`'s mined learning
        # passed source="reply", which is the bug this guard exists for.
        #
        # The 18 do not, and the honest claim is narrower than it is tempting to
        # make. `confirm_memory` DOES write source='confirmed' — it is the only
        # writer of that literal anywhere in this repo's history (`git log --all
        # -S"source = 'confirmed'" -- src` returns exactly one commit, the one
        # that introduced the method) — and it has always set `confirmed = 1` in
        # the SAME UPDATE. So it is the COMBINATION that has no producer either
        # this session or the review before it could find. Not "no code path can
        # produce it": nothing here rules out a hand-run UPDATE, an older tree,
        # or a path we did not think to look at. One more clue, recorded rather
        # than interpreted: `created_at` on all 18 is in the column DEFAULT's
        # format (`datetime('now')` — space separator, no offset) while
        # `updated_at` is Python `_now()`'s ISO-8601 with microseconds, so the
        # rows were inserted with the default and updated 36 seconds later by
        # something in Python. Which thing, we could not establish.
        #
        # WHAT "STRANDED" MEANS HERE, precisely — the first draft of this said
        # "no code path will ever surface them again", and that is false:
        #   · NOT reachable: `LearningQueue.pending()` (source='proposed'),
        #     `active()` and `GET /api/learnings` (confirmed=1), prompt
        #     injection via `list_memories(confirmed=True, …)`, and
        #     `context/sessions.py`'s recall (`WHERE confirmed = 1`). So they
        #     can never become an active rule, and can never reach the human
        #     confirm gate — the two paths that decide anything.
        #   · STILL reachable: `learning/curator.py`'s `curate()` reads
        #     `list_memories(confirmed=False)` with no source filter, so its
        #     dedupe pass can archive one as a duplicate and its LLM pass can
        #     propose archiving or consolidating it; and `nh recall <q>
        #     --include-pending` lists them as "memory (pending)".
        #
        # THIS BRANCH RUNS NOTHING AGAINST THEM. There is no migration here and
        # no write to the operator's database. The options are the operator's:
        #   1. Leave them. Nothing injects them into any prompt. The only cost
        #      is that an ad-hoc count of "pending learnings" disagrees with the
        #      queue by 20.
        #   2. Re-queue them — `UPDATE memories SET source='proposed' WHERE
        #      confirmed=0 AND source<>'proposed'` — which is the only option
        #      that hands the decision back, at the cost of 20 more rows on a
        #      queue already holding 329.
        #   3. Archive them (`archived=1`), keeping the rows and their dedupe
        #      keys while removing them from the curator's input.
        # Deleting them is not on the list: the row carries the dedupe key, and
        # their content is environment notes about the operator's own machine
        # and workplace — not quoted here, because this file ships.
        if not confirmed and source != SOURCE_PROPOSED:
            raise ValueError(
                f"add_memory(confirmed=False, source={source!r}) would write a "
                f"proposal that `pending()` cannot see — unconfirmed memories "
                f"must use source={SOURCE_PROPOSED!r}. Record the provenance "
                f"in `origin=` instead, which is a second column for exactly "
                f"this question."
            )
        if dedupe_key is not None:
            if await self._fetchone(
                "SELECT id FROM memories WHERE file_path = ? LIMIT 1", (dedupe_key,)
            ):
                return None
        mem_id = uuid.uuid4().hex
        await self.db.execute(
            """INSERT INTO memories
                 (id, type, title, content, file_path, tags, project, source,
                  confirmed, origin, evidence, project_scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, mem_type, title, content, dedupe_key,
             json.dumps(tags or []), project, source, 1 if confirmed else 0,
             origin, json.dumps(evidence) if evidence is not None else None,
             project_scope),
        )
        await self.db.commit()
        return mem_id

    async def memory_dedupe_key_exists(self, dedupe_key: str) -> bool:
        """True when some memory already carries this dedupe signature (stored
        in ``file_path``).

        ``add_memory`` runs the same check — but only once it has been handed a
        finished proposal, which for a BATCH caller means after the utility
        call that built it was already paid for. B2's harvest re-reads the
        whole correction history on every run, so without this it would spend
        one distillation per already-queued cluster and write nothing.

        ARCHIVED ROWS COUNT, and that is load-bearing rather than an oversight
        in the WHERE clause: an archived proposal is how a human's "no" is
        recorded (``LearningQueue.reject`` archives supervisor-origin rows).
        Skipping archived rows here would make every rejected lesson come back
        on the next harvest, re-distilled at the utility tier.
        """
        return await self._fetchone(
            "SELECT id FROM memories WHERE file_path = ? LIMIT 1", (dedupe_key,)
        ) is not None

    async def list_memories(
        self, *, confirmed: bool | None = None, source: str | None = None,
        mem_type: str | None = None, project: str | None = None,
        scope: str | None = None,
        include_global: bool = True, include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List memories, optionally scoped to a project.

        When ``project`` and/or ``scope`` is given, only rules/skills attached
        to that project are returned, plus globals unless ``include_global``
        is False. When both are None, no project filter is applied (all rows).

        ``scope`` is the B4 project identity (``learning/scope.py``: sha256 of
        the normalized remote URL) and ``project`` the checkout path. A row
        matches on EITHER — scope for rows that carry one (the same repo
        cloned at two paths is one project), path for legacy rows written
        before the column or for repos with no remote. A GLOBAL row is one
        with neither key (``project IS NULL AND project_scope IS NULL``) —
        exactly the pre-B4 rows the old ``project IS NULL`` clause matched,
        since no row had a scope before the column existed.
        """
        clauses, params = [], []
        if not include_archived:
            # archived is NULL on rows that predate the column — treat as live
            clauses.append("(archived IS NULL OR archived = 0)")
        if confirmed is not None:
            clauses.append("confirmed = ?")
            params.append(1 if confirmed else 0)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if mem_type is not None:
            clauses.append("type = ?")
            params.append(mem_type)
        if project is not None or scope is not None:
            scoped = []
            if scope is not None:
                scoped.append("project_scope = ?")
                params.append(scope)
            if project is not None:
                scoped.append("project = ?")
                params.append(project)
            if include_global:
                scoped.append("(project IS NULL AND project_scope IS NULL)")
            clauses.append("(" + " OR ".join(scoped) + ")")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._fetchall(
            f"SELECT * FROM memories{where} ORDER BY created_at DESC", params
        )
        return [dict(r) for r in rows]

    @serialized_write
    async def stamp_project_scope(self, project: str, scope: str) -> int:
        """Attach the B4 scope identity to legacy path-keyed rows (B4's online
        migration). Runs when a repo is actually SEEN — the only moment the
        path→remote mapping is knowable, since the migration itself cannot run
        git in checkouts that may no longer exist. Only rows still without a
        scope are touched; returns how many were stamped."""
        cur = await self.db.execute(
            "UPDATE memories SET project_scope = ? "
            "WHERE project = ? AND project_scope IS NULL", (scope, project))
        await self.db.commit()
        return cur.rowcount

    # ----------------------------- playbooks ------------------------------ #

    @serialized_write
    async def add_playbook(
        self, *, title: str, trigger_keywords: list[str] | None = None,
        procedure: str = "", postconditions: list[str] | None = None,
        forbidden: list[str] | None = None,
        required_from_user: list[str] | None = None,
        project: str | None = None,
    ) -> str:
        """Insert an operator-authored playbook (1.4). Returns its id."""
        pb_id = uuid.uuid4().hex
        await self.db.execute(
            """INSERT INTO playbooks
                 (id, title, trigger_keywords, procedure, postconditions,
                  forbidden, required_from_user, project)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pb_id, title, json.dumps(trigger_keywords or []), procedure,
             json.dumps(postconditions or []), json.dumps(forbidden or []),
             json.dumps(required_from_user or []), project),
        )
        await self.db.commit()
        return pb_id

    async def list_playbooks(
        self, *, project: str | None = None, include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """All playbooks, optionally scoped to a project (globals included
        unless ``include_global`` is False). Mirrors ``list_memories``."""
        clauses, params = [], []
        if project is not None:
            if include_global:
                clauses.append("(project = ? OR project IS NULL)")
            else:
                clauses.append("project = ?")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._fetchall(
            f"SELECT * FROM playbooks{where} ORDER BY created_at DESC", params
        )
        return [dict(r) for r in rows]

    @serialized_write
    async def delete_playbook(self, prefix: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM playbooks WHERE id = ? OR id LIKE ?",
            (prefix, prefix + "%"))
        await self.db.commit()
        return cur.rowcount > 0

    # --------------------------- PR merge order (2.2) --------------------- #

    @serialized_write
    async def add_pr_edge(self, *, child_pr: str, parent_pr: str,
                          project: str | None = None) -> None:
        """Record that child_pr must merge AFTER parent_pr (2.2)."""
        await self.db.execute(
            "INSERT OR IGNORE INTO pr_edges (child_pr, parent_pr, project) "
            "VALUES (?, ?, ?)", (child_pr, parent_pr, project))
        await self.db.commit()

    async def list_pr_edges(
        self, *, project: str | None = None,
    ) -> list[tuple[str, str]]:
        """All (child_pr, parent_pr) edges, optionally scoped to a project."""
        if project is not None:
            rows = await self._fetchall(
                "SELECT child_pr, parent_pr FROM pr_edges "
                "WHERE project = ? OR project IS NULL", (project,))
        else:
            rows = await self._fetchall(
                "SELECT child_pr, parent_pr FROM pr_edges")
        return [(r["child_pr"], r["parent_pr"]) for r in rows]

    @serialized_write
    async def delete_pr_edges_for(self, pr: str) -> int:
        """Remove every edge touching a PR (e.g. once it merges or closes)."""
        cur = await self.db.execute(
            "DELETE FROM pr_edges WHERE child_pr = ? OR parent_pr = ?", (pr, pr))
        await self.db.commit()
        return cur.rowcount

    # --------------------------- PR outcomes (0010) ------------------------ #
    #
    # A PR's fate is SETTLED once it merged or was closed without merging;
    # `open` and `unknown` are still in flight. Two behaviours key off this one
    # set — the no-downgrade rule in `record_pr_outcome` and the re-poll
    # selection in `list_pr_outcomes` — and they were separate string literals
    # until they were folded into this constant, which is the shape where one
    # can be widened and the other quietly left behind.
    #
    # It restates `vcs.pr_outcome.MERGED`/`CLOSED_UNMERGED` because `core` must
    # not import `vcs`. That duplication is deliberate and it is PINNED:
    # `tests/test_pr_outcome.py::test_db_and_vcs_agree_on_which_outcomes_are_settled`
    # fails if the two spellings ever diverge.

    @serialized_write
    async def record_pr_outcome(
        self, *, task_id: str, pr_url: str,
        outcome: str, outcome_evidence: str = "", ci_status: str | None = None,
        observed_source: str = "live",
        forge: str = "", forge_host: str = "", repo_slug: str = "",
        pr_number: int | None = None,
        opened_at: str | None = None, checked_at: str | None = None,
        attributes: str | None = None,
    ) -> None:
        """Upsert one PR's recorded outcome (migration 0010).

        UPSERT rather than INSERT OR REPLACE: a REPLACE deletes the old row
        first, so every column the refresh path does not pass — notably
        ``opened_at``, which only the PR-open path knows — would be silently
        reset to its default on the first refresh. The excluded-or-keep
        expressions below preserve a value already on the row whenever the
        caller passes None.

        ``ci_status=None`` means "this observation did not look at CI" and KEEPS
        whatever the row already had. That is not the same as ``"unknown"``,
        which means "we looked and could not tell": the wake watcher's state
        rung polls the PR's state without fetching its checks, and writing
        ``unknown`` from it would erase a real ``fail`` that the previous
        refresh had measured.

        THE NO-DOWNGRADE RULE, and why it is in the SQL rather than in a caller.
        A row that has reached a SETTLED outcome (``merged``/``closed_unmerged``)
        is never overwritten by an UNSETTLED one (``open``/``unknown``). A PR
        does not un-merge, so an observation that says it did is not news — it
        is an instrument failure (``gh`` uninstalled, token expired, laptop
        offline), and letting it land would delete the one fact this table
        exists to hold. The old expression was a plain ``outcome =
        excluded.outcome``: every settled row was one broken poll away from
        reverting to ``unknown``, which is the precise failure the caller-side
        ``COALESCE`` on ``ci_status`` was already written to prevent one column
        over. It lives here, in the single statement every writer goes through,
        because a rule enforced in one caller is a rule the next caller does not
        have; ``evidence``/``checked_at``/``observed_source`` move in lockstep
        with ``outcome`` so a kept verdict never ends up wearing the rejected
        observation's justification.

        Settled → settled IS allowed: that is a correction from a better probe
        (a ship check that could not resolve the branch the first time), not a
        regression.
        """
        # `_SETTLED_OUTCOMES` is duplicated from `vcs.pr_outcome` on purpose —
        # `core` must not import `vcs`. `tests/test_pr_outcome.py` pins the two
        # spellings equal, so the duplication cannot drift silently.
        keep = (
            "CASE WHEN pr_outcomes.outcome IN {s} AND excluded.outcome NOT IN {s} "
            "THEN 1 ELSE 0 END"
        ).format(s=_SETTLED_OUTCOMES_SQL)
        await self.db.execute(
            "INSERT INTO pr_outcomes (task_id, pr_url, forge, forge_host, "
            "  repo_slug, pr_number, outcome, outcome_evidence, ci_status, "
            "  observed_source, opened_at, checked_at, attributes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'unknown'), ?, ?, ?, "
            "        COALESCE(?, '{}')) "
            "ON CONFLICT(task_id, pr_url) DO UPDATE SET "
            "  forge = excluded.forge, forge_host = excluded.forge_host, "
            "  repo_slug = excluded.repo_slug, pr_number = excluded.pr_number, "
            f"  outcome = CASE WHEN {keep} = 1 THEN pr_outcomes.outcome "
            "                 ELSE excluded.outcome END, "
            f"  outcome_evidence = CASE WHEN {keep} = 1 "
            "                     THEN pr_outcomes.outcome_evidence "
            "                     ELSE excluded.outcome_evidence END, "
            "  ci_status = COALESCE(?, pr_outcomes.ci_status), "
            f"  observed_source = CASE WHEN {keep} = 1 "
            "                    THEN pr_outcomes.observed_source "
            "                    ELSE excluded.observed_source END, "
            "  opened_at = COALESCE(pr_outcomes.opened_at, excluded.opened_at), "
            f"  checked_at = CASE WHEN {keep} = 1 THEN pr_outcomes.checked_at "
            "               ELSE COALESCE(excluded.checked_at, "
            "                             pr_outcomes.checked_at) END, "
            "  attributes = COALESCE(?, pr_outcomes.attributes)",
            (task_id, pr_url, forge, forge_host, repo_slug, pr_number,
             outcome, outcome_evidence, ci_status, observed_source,
             opened_at, checked_at, attributes, ci_status, attributes))
        await self.db.commit()

    async def list_pr_outcomes(
        self, *, task_id: str | None = None, unsettled_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recorded PR outcomes, newest-opened first.

        ``unsettled_only`` selects the rows a refresh should re-poll — ``open``
        and ``unknown``. It is spelled as a NEGATIVE (``NOT IN`` the settled
        pair) rather than as a list of the two unsettled values on purpose: a
        row carrying some fifth value written by a future build must be
        re-polled, not silently skipped as if it were settled.
        """
        where, params = [], []
        if task_id is not None:
            where.append("task_id = ?")
            params.append(task_id)
        if unsettled_only:
            where.append(f"outcome NOT IN {_SETTLED_OUTCOMES_SQL}")
        sql = "SELECT * FROM pr_outcomes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(opened_at, checked_at, '') DESC, pr_url"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in await self._fetchall(sql, tuple(params))]

    async def find_memory(self, prefix: str) -> dict[str, Any] | None:
        rows = await self._fetchall(
            "SELECT * FROM memories WHERE id = ? OR id LIKE ? LIMIT 2",
            (prefix, prefix + "%"),
        )
        return dict(rows[0]) if len(rows) == 1 else None

    @serialized_write
    async def archive_memory(self, mem_id: str, reason: str = "") -> bool:
        """Recoverable archive (curator action — never a delete). The reason
        is appended to content so recovery keeps the audit trail."""
        suffix = f"\n\n[archived: {reason}]" if reason else ""
        cur = await self.db.execute(
            "UPDATE memories SET archived = 1, content = content || ? "
            "WHERE id = ? AND archived = 0", (suffix, mem_id))
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def touch_memories_used(self, mem_ids: list[str]) -> int:
        """Stamp ``last_used_at`` on every memory in *mem_ids*. Returns the
        number of rows updated.

        ONE statement per chunk, not one per id. This runs on the per-attempt
        hot path (every task start, every review round) with an active set that
        is 71 rows in the operator's own install, and every write here queues
        behind `serialized_write`'s single connection lock — N awaited UPDATEs
        would be N lock acquisitions on the critical path between a task being
        picked up and the coder starting.

        Chunked at 400 ids because `IN (?, ?, …)` is one bind parameter per id
        and SQLite has a variable ceiling (999 on older builds). The active set
        is far below that today; the chunking is here so a future store that is
        not stays correct rather than raising at the worst possible moment.

        ``updated_at`` is deliberately NOT touched. It records when the memory's
        CONTENT last changed — a human confirming or editing it — and injecting
        a rule changes nothing about the rule. Overloading it would erase the
        only timestamp that says when the operator last had an opinion.
        """
        ids = [i for i in (mem_ids or []) if i]
        if not ids:
            return 0
        now = _now()
        total = 0
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ", ".join("?" for _ in chunk)
            cur = await self.db.execute(
                f"UPDATE memories SET last_used_at = ? WHERE id IN ({marks})",
                (now, *chunk),
            )
            total += cur.rowcount
        await self.db.commit()
        return total

    async def stale_memories(
        self, *, days: int, project: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Confirmed, unarchived memories not injected into a prompt in *days*.

        A NULL ``last_used_at`` counts as stale — but it is genuinely ambiguous
        (never triggered, or written before the column existed), and the caller
        is expected to say so rather than report both as "never used". The
        cutoff and the stored stamps are both `_now()`-format ISO-8601 UTC, so
        the string comparison is a real chronological one.

        READ-ONLY. Nothing here archives or deletes: these are CONFIRMED rows,
        which `learning/curator.py` calls "the operator's — never touched", and
        an unused rule is not a wrong rule. It is a report.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = await self.list_memories(
            confirmed=True, project=project, scope=scope)
        return [r for r in rows
                if not r.get("last_used_at") or r["last_used_at"] < cutoff]

    @serialized_write
    async def confirm_memory(self, mem_id: str) -> bool:
        """Promote a proposed memory into the active set (one-click confirm)."""
        cur = await self.db.execute(
            "UPDATE memories SET confirmed = 1, source = 'confirmed', "
            "updated_at = ? WHERE id = ?",
            (_now(), mem_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def delete_memory(self, mem_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        await self.db.commit()
        return cur.rowcount > 0

    # ----------------------- task events (persisted) ----------------------- #

    @serialized_write
    async def save_events(self, task_id: str, events: list[dict[str, Any]]) -> None:
        """Persist a batch of task events so they survive a server restart."""
        if not events:
            return
        await self.db.executemany(
            "INSERT INTO task_events (task_id, ts, data) VALUES (?, ?, ?)",
            [(task_id, e.get("ts", 0), json.dumps(e)) for e in events],
        )
        await self.db.commit()

    async def list_events(self, task_id: str) -> list[dict[str, Any]]:
        """Return persisted events for a task, ordered oldest → newest."""
        rows = await self._fetchall(
            "SELECT data FROM task_events WHERE task_id = ? ORDER BY ts ASC, id ASC",
            (task_id,),
        )
        return [json.loads(r["data"]) for r in rows]

    async def list_supervisor_corrections(
        self, *, project: str | None = None, limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Every persisted supervisor ``correct`` decision, oldest first (B2).

        The supervisor emits its verdict as a ``supervisor_decision`` event
        whose ``text`` is the action and whose ``message`` is the correction —
        already truncated to 200 chars by ``Orchestrator.emit``'s call site,
        which is the only form that was ever stored. The project is the task's
        ``repo_path``, joined here so the caller clusters per repo without a
        second query per task.

        ``e.task_id`` is the indexed COLUMN, not ``json_extract(data,
        '$.task_id')`` — both are populated and they agree, but only one of
        them can use ``idx_task_events_task_id``.
        """
        clauses = [
            "json_extract(e.data, '$.kind') = 'supervisor_decision'",
            "json_extract(e.data, '$.text') = 'correct'",
        ]
        params: list[Any] = []
        if project is not None:
            clauses.append("t.repo_path = ?")
            params.append(project)
        params.append(int(limit))
        rows = await self._fetchall(
            "SELECT e.task_id AS task_id, t.repo_path AS project, e.ts AS ts, "
            "       json_extract(e.data, '$.message') AS message "
            "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY e.ts ASC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    async def last_event_ts(self, task_id: str) -> float | None:
        """Epoch seconds of the newest persisted event, or None if none. Used
        by the stuck-active-task watchdog to detect a task frozen mid-run."""
        row = await self._fetchone(
            "SELECT MAX(ts) FROM task_events WHERE task_id = ?", (task_id,))
        return float(row[0]) if row and row[0] is not None else None

    # ----------------------- project profiles ----------------------------- #

    @serialized_write
    async def upsert_profile(self, profile: "ProjectProfile") -> None:
        d = profile.to_dict()
        await self.db.execute(
            """INSERT INTO project_profiles
                 (repo_path, ecosystem, install_cmd, test_cmd, lint_cmd,
                  confirmed, data, updated_at)
               VALUES (:repo_path, :ecosystem, :install_cmd, :test_cmd, :lint_cmd,
                       :confirmed, :data, :updated_at)
               ON CONFLICT(repo_path) DO UPDATE SET
                 ecosystem=excluded.ecosystem, install_cmd=excluded.install_cmd,
                 test_cmd=excluded.test_cmd, lint_cmd=excluded.lint_cmd,
                 confirmed=excluded.confirmed, data=excluded.data,
                 updated_at=excluded.updated_at""",
            {
                "repo_path": d["repo_path"], "ecosystem": d["ecosystem"],
                "install_cmd": d["install_cmd"], "test_cmd": d["test_cmd"],
                "lint_cmd": d["lint_cmd"], "confirmed": 1 if d["confirmed"] else 0,
                "data": json.dumps(d), "updated_at": _now(),
            },
        )
        await self.db.commit()

    async def get_profile(self, repo_path: str) -> "ProjectProfile | None":
        from ..profile import ProjectProfile
        row = await self._fetchone(
            "SELECT data FROM project_profiles WHERE repo_path = ?", (str(repo_path),)
        )
        return ProjectProfile.from_dict(json.loads(row["data"])) if row else None

    async def list_profiles(self) -> list[dict[str, Any]]:
        """Return all onboarded repo profiles as dicts."""
        rows = await self._fetchall(
            "SELECT repo_path, ecosystem, confirmed, data FROM project_profiles "
            "ORDER BY repo_path"
        )
        return [dict(r) for r in rows]

    # ----------------------------- projects --------------------------------- #

    @serialized_write
    async def create_project(self, project: "Project") -> "Project":
        from ..project_model import Project
        row = project.to_row()
        await self.db.execute(
            "INSERT INTO projects (id, name, repo_paths, primary_repo, test_layers) "
            "VALUES (:id, :name, :repo_paths, :primary_repo, :test_layers)",
            row,
        )
        await self.db.commit()
        return project

    async def get_project(self, project_id: str) -> "Project | None":
        from ..project_model import Project
        row = await self._fetchone(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        return Project.from_row(row) if row else None

    async def get_project_by_name(self, name: str) -> "Project | None":
        from ..project_model import Project
        row = await self._fetchone(
            "SELECT * FROM projects WHERE name = ?", (name,)
        )
        return Project.from_row(row) if row else None

    async def list_projects(self) -> list["Project"]:
        from ..project_model import Project
        rows = await self._fetchall(
            "SELECT * FROM projects ORDER BY name"
        )
        return [Project.from_row(r) for r in rows]

    async def find_project_by_repo(self, repo_path: str) -> "Project | None":
        """Find the project whose ``repo_paths`` contains *repo_path*."""
        for proj in await self.list_projects():
            if repo_path in proj.repo_paths:
                return proj
        return None

    @serialized_write
    async def update_project(self, project: "Project") -> None:
        row = project.to_row()
        await self.db.execute(
            "UPDATE projects SET name = :name, repo_paths = :repo_paths, "
            "primary_repo = :primary_repo, test_layers = :test_layers, "
            "updated_at = :updated_at WHERE id = :id",
            {**row, "updated_at": _now()},
        )
        await self.db.commit()

    @serialized_write
    async def delete_project(self, project_id: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM projects WHERE id = ?", (project_id,)
        )
        await self.db.commit()
        return cur.rowcount > 0

    # ----------------------- history cache (Phase 7e) ---------------------- #

    async def history_cache_get(self, content_sig: str) -> dict | None:
        """Return cached ingestion result for a transcript content signature."""
        row = await self._fetchone(
            "SELECT * FROM history_cache WHERE content_sig = ?", (content_sig,)
        )
        return dict(row) if row else None

    @serialized_write
    async def history_cache_put(
        self, content_sig: str, cascade_id: str, title: str, findings_json: str,
    ) -> None:
        """Cache ingestion result keyed by content signature (upsert)."""
        await self.db.execute(
            "INSERT OR REPLACE INTO history_cache "
            "(content_sig, cascade_id, title, findings_json) VALUES (?, ?, ?, ?)",
            (content_sig, cascade_id, title, findings_json),
        )
        await self.db.commit()

    @serialized_write
    async def history_cache_clear(self) -> int:
        """Clear the entire history cache (Re-scan). Returns rows deleted."""
        cur = await self.db.execute("DELETE FROM history_cache")
        await self.db.commit()
        return cur.rowcount

    async def list_history_cache(self) -> list[dict[str, Any]]:
        """All cached IDE-transcript ingestion results (title + findings),
        most recent first — for `nh recall` to search alongside tasks/memories."""
        rows = await self._fetchall(
            "SELECT * FROM history_cache ORDER BY ingested_at DESC"
        )
        return [dict(r) for r in rows]
