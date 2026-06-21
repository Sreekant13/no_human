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
