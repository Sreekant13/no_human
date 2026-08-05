"""Hand-off by LABEL: a label put on a Jira issue, and the task it becomes.

Nothing running inside Jira can reach no_human. no_human listens on 127.0.0.1
on the user's own machine; an Atlassian-side app, an automation rule or a human
doing a bulk edit all live in Atlassian's cloud. So the only hand-off available
to any of them is to write a LABEL, and this poller is the entire transport
back. There is no inbound path and no credential anywhere but on this side.

That puts the whole contract on THIS side, where it can be asserted rather than
assumed: that the operator's label-based JQL reaches Jira verbatim, that one
task is created per NEW issue, and above all that re-labelling an issue never
produces a second task. A label is not a message — it stays on the issue, so
the poller sees it again on every subsequent tick, and idempotence is the only
thing standing between that and a duplicate task per tick, forever.

These tests do not touch the network — `httpx.get` is mocked — and they add to
the existing Jira intake suite rather than replacing any of it.
"""

import pytest

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.intake.jira import JiraAdapter
from no_human.intake.jira_poll import JiraPoller

# A label-based JQL as an operator hand-writes one into `integrations.jira.jql`.
# Deliberately NOT any query this repo could reconstruct: the property under
# test is that whatever the operator wrote reaches Jira unchanged, and a literal
# that some code path might have rebuilt would pass a rebuilding adapter too.
# Held as a literal so a change on either side of the contract shows up as a
# failing test rather than as a silent no-op — a query that quietly stops being
# honoured selects nothing, and nothing is exactly what a no-op looks like.
OPERATOR_JQL = 'labels = "no_human" AND assignee = currentUser() ORDER BY created ASC'


def _cfg(**over):
    j = {
        "enabled": True,
        "site": "https://acme.atlassian.net",
        "project_key": "PROJ",
        "email": "me@x.com",
        "jql": OPERATOR_JQL,
    }
    j.update(over)
    return {"integrations": {"jira": j}}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _issue(key, summary="Ship it"):
    """An issue as the label JQL returns it, carrying the hand-off label."""
    return {"key": key, "fields": {"summary": summary, "labels": ["no_human"]}}


def test_label_jql_reaches_jira_verbatim(monkeypatch):
    """The operator's label JQL is sent as written — never rebuilt, never
    merged with the project default. If this stopped holding, a label-based
    hand-off would silently select nothing."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": [_issue("PROJ-1")]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    JiraAdapter(_cfg()).search()
    assert captured["params"]["jql"] == OPERATOR_JQL


@pytest.mark.asyncio
async def test_relabelling_an_issue_never_creates_a_second_task(tmp_path, monkeypatch):
    """The idempotence requirement, stated the way a user hits it: label the
    same issue a second time.

    Re-applying a label is a no-op in Jira (it is already there), so the issue
    is simply still in the JQL's result set on the next tick. The poller must
    recognise it, not re-import it."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        adapter = JiraAdapter(_cfg())
        monkeypatch.setattr(adapter, "search", lambda: [_issue("PROJ-1")])
        poller = JiraPoller(adapter, store, config=_cfg())

        first = await poller.poll_once()
        assert (first.created, first.skipped) == (1, 0)

        # Labelled again → label already present → same issue, same key.
        second = await poller.poll_once()
        assert (second.created, second.skipped) == (0, 1)

        tasks = [t for t in await store.list_tasks() if t.external_id == "PROJ-1"]
        assert len(tasks) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_bulk_labelling_creates_one_task_per_issue_then_dedupes_all_of_them(
    tmp_path, monkeypatch
):
    """Five tickets labelled at once — a bulk edit in the issue navigator.
    Five tasks, then zero on every subsequent tick for as long as the label
    stays on."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        keys = [f"PROJ-{n}" for n in range(1, 6)]
        adapter = JiraAdapter(_cfg())
        monkeypatch.setattr(adapter, "search", lambda: [_issue(k) for k in keys])
        poller = JiraPoller(adapter, store, config=_cfg())

        first = await poller.poll_once()
        assert (first.created, first.seen) == (5, 5)
        assert sorted(first.numbers) == sorted(keys)

        for _ in range(3):
            again = await poller.poll_once()
            assert (again.created, again.skipped) == (0, 5)

        assert sorted(t.external_id for t in await store.list_tasks()) == sorted(keys)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dedupe_survives_the_label_being_removed_and_re_added(tmp_path, monkeypatch):
    """A user unlabels an issue (it drops out of the JQL) and later labels it
    again (it comes back). The task already exists; nothing new is created.
    Dedupe is on the issue key, not on continuous presence."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        visible = [_issue("PROJ-1")]
        adapter = JiraAdapter(_cfg())
        monkeypatch.setattr(adapter, "search", lambda: list(visible))
        poller = JiraPoller(adapter, store, config=_cfg())

        assert (await poller.poll_once()).created == 1
        visible.clear()                       # label removed → JQL misses it
        assert (await poller.poll_once()).seen == 0
        visible.append(_issue("PROJ-1"))      # labelled again
        third = await poller.poll_once()

        assert (third.created, third.skipped) == (0, 1)
        assert len([t for t in await store.list_tasks() if t.external_id == "PROJ-1"]) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dedupe_still_imports_when_a_completed_task_exists_for_the_issue(
    tmp_path, monkeypatch
):
    """Honest statement of the boundary, so nobody is surprised by it: dedupe
    is on the KEY alone and ignores task status. Once no_human has imported an
    issue, re-applying the label never re-runs it — not even after the first
    task finished. Re-running finished work is `nh task add`'s job, not a
    label's."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        done = Task.new("Ship it", source="jira", external_id="PROJ-1")
        done.status = TaskStatus.DONE
        await store.create_task(done)

        adapter = JiraAdapter(_cfg())
        monkeypatch.setattr(adapter, "search", lambda: [_issue("PROJ-1")])
        result = await JiraPoller(adapter, store, config=_cfg()).poll_once()

        assert (result.created, result.skipped) == (0, 1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dedupe_is_keyed_on_source_not_on_the_key_alone(tmp_path, monkeypatch):
    """The other half of the (source, external_id) pair. A task imported from a
    different tracker that happens to share the key must not shadow a Jira
    issue — otherwise a Linear ENG-1 would silently suppress a Jira ENG-1."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        await store.create_task(Task.new("elsewhere", source="linear", external_id="PROJ-1"))

        adapter = JiraAdapter(_cfg())
        monkeypatch.setattr(adapter, "search", lambda: [_issue("PROJ-1")])
        result = await JiraPoller(adapter, store, config=_cfg()).poll_once()

        assert (result.created, result.skipped) == (1, 0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_two_new_issues_in_one_tick_are_deduped_against_each_other(
    tmp_path, monkeypatch
):
    """A duplicate key inside a single search response (paging overlap, or a
    JQL that returns the same issue twice) creates one task, not two — the
    poller updates its seen-set as it goes rather than only between ticks."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        adapter = JiraAdapter(_cfg())
        monkeypatch.setattr(adapter, "search", lambda: [_issue("PROJ-1"), _issue("PROJ-1")])
        result = await JiraPoller(adapter, store, config=_cfg()).poll_once()

        assert result.created == 1
        assert len(await store.list_tasks()) == 1
    finally:
        await store.close()
