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


async def test_merge_context_concurrent_writers_both_survive(tmp_path):
    """The lost-update class behind the 2026-07-10 incident: two writers
    holding stale Task copies clobber each other via update_task. merge_context
    must let concurrent merges of different keys BOTH land — same connection
    and across connections (the CLI and the server are different processes)."""
    import asyncio
    from no_human.core.db import Store
    from no_human.core.task import Task

    db = tmp_path / "nh.db"
    async with Store(db) as s:
        t = Task.new("x", repo_path="/tmp/x")
        t.context = {"seed": 1}
        await s.create_task(t)
        await asyncio.gather(
            s.merge_context(t.id, {"watcher_key": "a", "nested": {"w": 1}}),
            s.merge_context(t.id, {"coder_key": "b", "nested": {"c": 2}}),
        )
        ctx = (await s.get_task(t.id)).context
        assert ctx["seed"] == 1
        assert ctx["watcher_key"] == "a" and ctx["coder_key"] == "b"
        assert ctx["nested"] == {"w": 1, "c": 2}  # recursive merge, no clobber

    # Cross-connection (cross-process shape): a second Store on the same file.
    async with Store(db) as s1, Store(db) as s2:
        tid = t.id
        await asyncio.gather(
            s1.merge_context(tid, {"proc1": True}),
            s2.merge_context(tid, {"proc2": True}),
        )
        ctx = (await s1.get_task(tid)).context
        assert ctx["proc1"] is True and ctx["proc2"] is True


async def test_merge_context_none_deletes_and_lists_replace(tmp_path):
    from no_human.core.db import Store
    from no_human.core.task import Task
    async with Store(tmp_path / "nh.db") as s:
        t = Task.new("x", repo_path="/tmp/x")
        t.context = {"ci_gate": {"pipeline_id": "1"}, "rounds": [1, 2]}
        await s.create_task(t)
        merged = await s.merge_context(t.id, {"ci_gate": None, "rounds": [3]})
        assert "ci_gate" not in merged
        assert merged["rounds"] == [3]


async def test_append_context_list_is_atomic_and_creates(tmp_path):
    import asyncio
    from no_human.core.db import Store
    from no_human.core.task import Task
    async with Store(tmp_path / "nh.db") as s:
        t = Task.new("x", repo_path="/tmp/x")
        await s.create_task(t)
        await asyncio.gather(
            s.append_context_list(t.id, "send_back_feedback", {"m": "a"}),
            s.append_context_list(t.id, "send_back_feedback", {"m": "b"}),
        )
        fb = (await s.get_task(t.id)).context["send_back_feedback"]
        assert sorted(x["m"] for x in fb) == ["a", "b"]  # both appends land


async def test_update_task_columns_never_touches_context(tmp_path):
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus
    async with Store(tmp_path / "nh.db") as s:
        t = Task.new("x", repo_path="/tmp/x")
        t.context = {"fresh": 1}
        await s.create_task(t)
        # Another writer merges while our copy is stale.
        await s.merge_context(t.id, {"concurrent": True})
        t.context = {"fresh": 1}  # stale copy — would clobber via update_task
        t.blocker = {"category": "AMBIGUITY", "question": "?"}
        await s.update_task_columns(t)
        fresh = await s.get_task(t.id)
        assert fresh.context.get("concurrent") is True  # survived
        assert fresh.blocker["category"] == "AMBIGUITY"  # column written


async def test_playbook_crud_and_project_scope(store):
    """1.4: operator playbooks round-trip; project scope includes globals."""
    pid = await store.add_playbook(
        title="P", trigger_keywords=["x"], procedure="do",
        postconditions=["done"], project="/tmp/r")
    await store.add_playbook(title="G", trigger_keywords=["y"])  # global
    scoped = await store.list_playbooks(project="/tmp/r")
    assert {p["title"] for p in scoped} == {"P", "G"}
    assert await store.delete_playbook(pid[:8]) is True
    assert {p["title"] for p in await store.list_playbooks()} == {"G"}


async def test_pr_edges_round_trip(store):
    """2.2: PR dependency edges round-trip and clear when a PR merges."""
    await store.add_pr_edge(child_pr="pr/2", parent_pr="pr/1")
    await store.add_pr_edge(child_pr="pr/3", parent_pr="pr/2")
    await store.add_pr_edge(child_pr="pr/2", parent_pr="pr/1")  # dup → ignored
    edges = await store.list_pr_edges()
    assert set(edges) == {("pr/2", "pr/1"), ("pr/3", "pr/2")}
    removed = await store.delete_pr_edges_for("pr/2")
    assert removed == 2  # both edges touching pr/2
    assert await store.list_pr_edges() == []


async def test_failed_attempt_never_has_an_empty_reason(store):
    """Store-level backstop (C2): a failed attempt with no reason is an
    observability bug — the row gets a loud sentinel instead of silence."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(a, status="failed", test_results={"ok": False})
    row = (await store.list_attempts(t.id))[-1]
    assert "no failure reason recorded" in (row["failure_reason"] or "")


async def test_failed_attempt_backstop_never_clobbers_a_real_reason(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(a, failure_reason="tests failed: 1 failed")
    await store.update_attempt(a, status="failed")  # status set later
    row = (await store.list_attempts(t.id))[-1]
    assert row["failure_reason"] == "tests failed: 1 failed"


async def test_interrupted_attempts_get_a_reason_stamped(store):
    """Crash-orphaned rows (marked interrupted by the next create_attempt)
    must say why they stopped — they evaded both the failed-only backstop and
    the failed-only doctor check."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.create_attempt(t.id, 1)          # stays in_progress (crash)
    await store.create_attempt(t.id, 2)          # marks #1 interrupted
    rows = await store.list_attempts(t.id)
    assert rows[0]["status"] == "interrupted"
    assert "superseded by a newer attempt" in (rows[0]["failure_reason"] or "")
