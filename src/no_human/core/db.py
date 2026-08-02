"""Async SQLite store (WAL). Single-user, single-host — no Postgres (§3.6)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any, AsyncIterator, Awaitable, Callable, NamedTuple, TypeVar,
)

import aiosqlite

from .task import Task, TaskStatus, assert_transition

log = logging.getLogger("no_human.db")


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
    second Store in the SAME process for the Jira poller and a third for the
    Linear poller (`cli/commands.py::start._go`, locals `jira_store` and
    `linear_store`, under `integrations.*.enabled`), and every `nh` CLI
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
            return await fn(self, *args, **kwargs)

    wrapper.__nh_serialized_write__ = True  # type: ignore[attr-defined]
    return wrapper


class JiraImportedTaskRow(NamedTuple):
    """One row of the Jira-picker imported-chip projection (SCRUM-54) — only
    the four columns the chip lookup needs, never a full Task hydration."""

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
    # start` opens a second Store for the Jira poller and a third for the Linear
    # poller (`cli/commands.py::start._go`, locals `jira_store`/`linear_store`),
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
            # UTILITY-tier burn (supervisor checks, distillation,
            # stuck-hypothesis) — discarded entirely before B2 #6.
            "utility_tokens_used": "INTEGER DEFAULT 0",
            "utility_cache_read_tokens": "INTEGER DEFAULT 0",
            "utility_cache_creation_tokens": "INTEGER DEFAULT 0",
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

    async def list_jira_imported_tasks(self) -> list[JiraImportedTaskRow]:
        """Narrow projection for the Jira picker's imported-chip lookup
        (SCRUM-54): only (external_id, id, status, created_at) for
        jira-sourced tasks with a linked external_id, via one filtered SQL
        query — never a full `list_tasks()` hydration of every task's every
        column just to read four fields."""
        rows = await self._fetchall(
            "SELECT external_id, id, status, created_at FROM tasks "
            "WHERE source = 'jira' AND external_id IS NOT NULL"
        )
        return [
            JiraImportedTaskRow(
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
        # The value is a process fact, cached at startup; never raises.
        from .build_info import loaded_code
        await self.db.execute(
            "INSERT INTO attempts (id, task_id, attempt_number, "
            "loaded_code_version) VALUES (?, ?, ?, ?)",
            (attempt_id, task_id, attempt_number, loaded_code().descriptor),
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

    # The four model tiers the attempts table meters, and the three token
    # columns each one carries. `eval/northstar.py` already sums exactly this
    # set to report cost; the budget gate below now matches it, so the two can
    # no longer disagree about what a task spent.
    _USAGE_TIERS = ("", "review_", "plan_", "utility_")

    @classmethod
    def _usage_columns_by_class(cls) -> dict[str, tuple[str, ...]]:
        """The same twelve columns, grouped by PRICE CLASS rather than by tier.

        The four tiers all bill at the same three rates, so the classes — not
        the tiers — are what a cost-weighted budget has to keep apart
        (``core.pricing``). Keyed by the coder-tier column name so a caller can
        splat the result straight into ``pricing.weighted_tokens``.
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

        The same rows and the same twelve columns ``lifetime_usage`` sums, kept
        in their three price classes so the budget gate can weight them
        (``core.pricing.weighted_tokens``), plus a FOURTH key that is not a
        fourth class: ``output_tokens`` is the output slice of ``tokens_used``,
        carried here so the splat into ``weighted_tokens`` can charge it the
        output premium. It is deliberately absent from ``lifetime_usage``'s
        raw total, which would otherwise count it twice. The classes are
        summed across all
        four model tiers — coder, reviewer, planner, utility — because they all
        bill at the same three rates.
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
        creation, across all four model tiers (coder, reviewer, planner,
        utility). Cache reads are where the bulk of the burn lives (~83%), but
        this used to sum ONLY the coder's ``tokens_used + cache_read_tokens``
        — 2 of 12 columns. The gate was therefore blind to every reviewer,
        planner and utility token, and to cache creation everywhere. Measured
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
        ``"cli.task_add.grill"``, ``"orphaned_utility_usage"`` and
        ``"orphaned_plan_usage"`` — so the residual stays diagnosable rather
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
        dedupe_key: str | None = None,
    ) -> str | None:
        """Insert a memory. If ``dedupe_key`` matches an existing memory's
        signature (stored in file_path), skip and return None."""
        if dedupe_key is not None:
            if await self._fetchone(
                "SELECT id FROM memories WHERE file_path = ? LIMIT 1", (dedupe_key,)
            ):
                return None
        mem_id = uuid.uuid4().hex
        await self.db.execute(
            """INSERT INTO memories
                 (id, type, title, content, file_path, tags, project, source, confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, mem_type, title, content, dedupe_key,
             json.dumps(tags or []), project, source, 1 if confirmed else 0),
        )
        await self.db.commit()
        return mem_id

    async def list_memories(
        self, *, confirmed: bool | None = None, source: str | None = None,
        mem_type: str | None = None, project: str | None = None,
        include_global: bool = True, include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List memories, optionally scoped to a project.

        When ``project`` is given, only rules/skills attached to that project are
        returned, plus globals (``project IS NULL``) unless ``include_global`` is
        False. When ``project`` is None, no project filter is applied (all rows).
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
        if project is not None:
            if include_global:
                clauses.append("(project = ? OR project IS NULL)")
            else:
                clauses.append("project = ?")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._fetchall(
            f"SELECT * FROM memories{where} ORDER BY created_at DESC", params
        )
        return [dict(r) for r in rows]

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
