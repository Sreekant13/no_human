"""SQLite store: create/get/list/transition/attempts."""

import pytest

from no_human.core.db import Store
from no_human.core.task import IllegalTransition, Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


async def test_create_and_get(store):
    t = Task.new("do a thing", repo_path="/tmp/r")
    await store.create_task(t)
    got = await store.get_task(t.id)
    assert got is not None and got.title == "do a thing"


async def test_find_by_prefix(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    assert (await store.find_task(t.id[:8])).id == t.id


async def test_set_status_enforces_transitions(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.CONTEXT)
    assert (await store.get_task(t.id)).status is TaskStatus.CONTEXT
    with pytest.raises(IllegalTransition):
        await store.set_status(t, TaskStatus.DONE)


async def test_attempts_lifecycle(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, branch_name="no-human/x", turns_used=3,
                               test_results={"ok": True})
    attempts = await store.list_attempts(t.id)
    assert attempts[0]["branch_name"] == "no-human/x"
    assert attempts[0]["turns_used"] == 3
    assert await store.count_attempts(t.id) == 1


async def test_list_by_status(store):
    a = Task.new("a", repo_path="/r")
    b = Task.new("b", repo_path="/r")
    await store.create_task(a)
    await store.create_task(b)
    await store.set_status(b, TaskStatus.CONTEXT)
    pend = await store.list_tasks(TaskStatus.PENDING)
    assert {t.id for t in pend} == {a.id}


async def test_list_memories_project_scoped(store):
    """A task on repo A sees A's rules + globals, never repo B's (B3)."""
    await store.add_memory(mem_type="rule", title="ra", content="for A",
                           project="/repo/a", confirmed=True)
    await store.add_memory(mem_type="rule", title="rb", content="for B",
                           project="/repo/b", confirmed=True)
    await store.add_memory(mem_type="rule", title="rg", content="global",
                           project=None, confirmed=True)

    scoped = await store.list_memories(confirmed=True, project="/repo/a")
    titles = {m["title"] for m in scoped}
    assert titles == {"ra", "rg"}  # A's rule + global, not B's

    scoped_only = await store.list_memories(
        confirmed=True, project="/repo/a", include_global=False
    )
    assert {m["title"] for m in scoped_only} == {"ra"}

    # No project filter → all rows (back-compat).
    everything = await store.list_memories(confirmed=True)
    assert {m["title"] for m in everything} == {"ra", "rb", "rg"}


async def test_save_and_list_events_persist_across_restart(tmp_path):
    """Events saved via save_events survive a Store reconnect (simulated restart)."""
    t = Task.new("x", repo_path="/tmp/r")
    db_path = tmp_path / "events.db"

    s1 = await Store(db_path).connect()
    await s1.create_task(t)
    events = [
        {"ts": 1.0, "kind": "tool_use", "source": "agent", "tool_name": "Read"},
        {"ts": 2.0, "kind": "result", "source": "orchestrator", "text": "done"},
    ]
    await s1.save_events(t.id, events)
    await s1.close()

    # Simulate a server restart: fresh Store instance, same db file.
    s2 = await Store(db_path).connect()
    persisted = await s2.list_events(t.id)
    assert len(persisted) == 2
    assert persisted[0]["kind"] == "tool_use"
    assert persisted[1]["text"] == "done"
    await s2.close()


async def test_list_events_empty_for_unknown_task(store):
    assert await store.list_events("no-such-task") == []
