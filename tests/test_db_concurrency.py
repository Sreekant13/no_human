"""Two tasks, one Store: the shared-connection write race (KI-1).

The pool runs `concurrency.max_workers` tasks against a single
`aiosqlite.Connection`. aiosqlite serialises individual operations on its
worker thread but not *sequences* of them, so before the fix a second
coroutine's `commit()` could land in the middle of another's write. Two
symptoms, both covered here:

  * the crash — `OperationalError: cannot commit transaction - SQL statements
    in progress`, raised when the interrupted statement was a writer that had
    produced a row (`UPDATE … RETURNING`);
  * the silent one — every multi-statement write in `Store` claims implicit
    atomicity, and a foreign commit split it.

The atomicity tests all observe through a SECOND connection, because the
connection doing the write sees its own uncommitted rows and would prove
nothing.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "c.db").connect()
    yield s
    await s.close()


@pytest.fixture
async def observer(tmp_path, store):
    """A second connection to the same file — sees COMMITTED state only."""
    s = await Store(tmp_path / "c.db").connect()
    yield s
    await s.close()


async def _mk_task(store, title="t") -> Task:
    t = Task.new(title, repo_path="/tmp/r")
    await store.create_task(t)
    return t


def _park_after(store, marker: str):
    """Freeze the next Store write inside its statement sequence, right after
    the statement whose SQL contains *marker*. Returns (reached, release)."""
    reached, release = asyncio.Event(), asyncio.Event()
    original = store.db.execute

    async def patched(sql, *args, **kwargs):
        result = await original(sql, *args, **kwargs)
        if marker in sql:
            store.db.execute = original  # park once, not on every statement
            reached.set()
            await release.wait()
        return result

    store.db.execute = patched
    return reached, release


# --------------------------- the crash itself --------------------------- #


async def test_concurrent_writers_can_always_commit(store):
    """The reported bug: one coroutine mid-write, another commits -> boom.

    Pre-fix this raised `cannot commit transaction - SQL statements in
    progress` on every run of this loop; it is the same error that killed
    attempts in `test_two_repos_run_concurrently_in_worktrees`.
    """
    a = await _mk_task(store, "a")
    b = await _mk_task(store, "b")
    errors: list[BaseException] = []

    async def updater():
        for _ in range(200):
            try:
                await store.update_task(a)
            except BaseException as exc:  # noqa: BLE001 - the assertion is "none"
                errors.append(exc)
                return
            await asyncio.sleep(0)

    async def committer():
        for i in range(200):
            try:
                await store.set_status(
                    b,
                    TaskStatus.IMPLEMENTING if i % 2 else TaskStatus.TESTING,
                    validate=False,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return
            await asyncio.sleep(0)

    await asyncio.gather(updater(), committer())
    assert errors == []


async def test_a_cancelled_write_does_not_wedge_the_connection(store):
    """A write cancelled between its statement and its commit must leave the
    connection usable.

    Honest scope: this passes on the pre-fix code too. Dropping the last
    reference to a `sqlite3.Cursor` finalizes its statement, so unwinding the
    cancelled frame happens to reset the live `UPDATE … RETURNING` writer. It is
    a guard, not a proof of the fix: it fails the moment anything starts holding
    a cursor on the Store (a cache, a lazily-consumed iterator), which would
    make a cancellation wedge every later commit rather than lose one attempt.
    """
    a = await _mk_task(store, "a")
    reached, release = _park_after(store, "kind=:kind")   # after update_task's UPDATE
    writer = asyncio.ensure_future(store.update_task(a))
    await asyncio.wait_for(reached.wait(), 5)
    writer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer

    # The connection must still be able to commit.
    await store.set_status(a, TaskStatus.IMPLEMENTING, validate=False)
    assert (await store.get_task(a.id)).status == TaskStatus.IMPLEMENTING


# ------------------------- atomicity, per write ------------------------- #


async def test_create_attempt_update_plus_insert_stays_atomic(store, observer):
    """UPDATE (close superseded attempts) + INSERT (the new one) is one unit."""
    t = await _mk_task(store)
    other = await _mk_task(store, "other")  # before parking: writes take the lock
    await store.create_attempt(t.id, 1)  # left in_progress, as a crash leaves it

    reached, release = _park_after(store, "status = 'interrupted'")
    writer = asyncio.ensure_future(store.create_attempt(t.id, 2))
    await asyncio.wait_for(reached.wait(), 5)

    # A concurrent Store write must not be able to commit the half-done unit.
    foreign = asyncio.ensure_future(
        store.set_status(other, TaskStatus.IMPLEMENTING, validate=False))
    for _ in range(20):
        await asyncio.sleep(0)

    rows = await observer.list_attempts(t.id)
    assert [r["status"] for r in rows] == ["in_progress"], (
        "a foreign commit exposed create_attempt's UPDATE without its INSERT")

    release.set()
    await writer
    await foreign
    rows = await observer.list_attempts(t.id)
    assert [(r["attempt_number"], r["status"]) for r in rows] == [
        (1, "interrupted"), (2, "in_progress")]


async def test_update_attempt_read_modify_write_stays_atomic(store, observer):
    """SELECT failure_reason -> UPDATE: the read must not be committed away."""
    t = await _mk_task(store)
    att = await store.create_attempt(t.id, 1)

    reached, release = _park_after(store, "UPDATE attempts SET")
    writer = asyncio.ensure_future(store.update_attempt(att, status="failed"))
    await asyncio.wait_for(reached.wait(), 5)

    foreign = asyncio.ensure_future(
        store.set_status(t, TaskStatus.IMPLEMENTING, validate=False))
    for _ in range(20):
        await asyncio.sleep(0)
    assert (await observer.list_attempts(t.id))[0]["status"] == "in_progress"

    release.set()
    await writer
    await foreign
    row = (await observer.list_attempts(t.id))[0]
    assert row["status"] == "failed"
    assert "no failure reason recorded" in (row["failure_reason"] or "")


async def test_add_memory_dedupe_then_insert_stays_atomic(store, observer):
    """SELECT dedupe -> INSERT is a read-modify-write: two concurrent inserts of
    the same dedupe_key must not both pass the check and both insert."""
    results = await asyncio.gather(*[
        store.add_memory(mem_type="lesson", title=f"m{i}", content="c",
                         dedupe_key="k1")
        for i in range(4)
    ])
    assert sum(r is not None for r in results) == 1, (
        "concurrent dedupe checks all ran before any insert")
    assert len(await observer.list_memories()) == 1


async def test_update_task_write_and_readback_stay_atomic(store, observer):
    """UPDATE + status read-back + COMMIT is one unit; the read-back must see
    this write's own row, and nothing may commit it early."""
    t = await _mk_task(store, "before")
    other = await _mk_task(store, "other")  # before parking: writes take the lock
    reached, release = _park_after(store, "SELECT status FROM tasks WHERE id")
    t.title = "after"
    writer = asyncio.ensure_future(store.update_task(t))
    await asyncio.wait_for(reached.wait(), 5)

    foreign = asyncio.ensure_future(
        store.set_status(other, TaskStatus.IMPLEMENTING, validate=False))
    for _ in range(20):
        await asyncio.sleep(0)
    assert (await observer.get_task(t.id)).title == "before"

    release.set()
    await writer
    await foreign
    assert (await observer.get_task(t.id)).title == "after"


async def test_merge_context_write_then_readback_stays_atomic(store, observer):
    """merge_context commits, then reads back — the read must observe its own
    merge, and concurrent merges of different keys must all survive.

    Honest scope: this passes on the pre-fix code too — the merge is a single
    atomic `json_patch` UPDATE, so it was never split. It pins that property
    against a future rewrite into a Python-side read-modify-write, which the
    write lock would then be the only thing making safe.
    """
    t = await _mk_task(store)
    await store.merge_context(t.id, {"a": 1})

    async def merge(key, val):
        return await store.merge_context(t.id, {key: val})

    results = await asyncio.gather(*[merge(f"k{i}", i) for i in range(10)])
    for i, ctx in enumerate(results):
        assert ctx[f"k{i}"] == i, "read-back missed its own merge"
    final = (await observer.get_task(t.id)).context
    assert final["a"] == 1
    assert all(final[f"k{i}"] == i for i in range(10)), "a merge was lost"


# ------------------------------ drift guard ------------------------------ #


async def test_every_committing_store_method_is_serialized():
    """Any future `Store` method that commits must take the write lock, or the
    race comes straight back. Checked against the real attribute, not source
    text: an undecorated committer fails here."""
    import no_human.core.db as db_mod

    src = inspect.getsource(Store)
    missing = []
    for name, member in vars(Store).items():
        if not inspect.iscoroutinefunction(member):
            continue
        try:
            body = inspect.getsource(member)
        except OSError:  # pragma: no cover
            continue
        if "self.db.commit()" not in body:
            continue
        if not getattr(member, "__nh_serialized_write__", False):
            missing.append(name)
    assert missing == [], f"Store methods commit without the write lock: {missing}"
    assert src and db_mod.serialized_write  # the guard is testing the real class
