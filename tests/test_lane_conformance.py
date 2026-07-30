"""Board lane routing: Python is the source of truth, and it must agree with the JS.

The lane decision used to exist only in ``web/src/boardLanes.js``. Anything else
that wanted a lane (the API, a CLI) had to reimplement it, and this repo already
shipped that failure once - PR-007, where the counts were right and the lane
LABEL lied.

Every case here is loaded from ``testdata/lane_conformance.json``, the same file
``web/src/laneConformance.test.mjs`` reads. One fixture file, two runners: the
implementations cannot drift apart without one of the two suites going red.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.core.lanes import LANE_KEYS, LANE_STATUSES, is_waiting, lane_for
from no_human.core.task import Task, TaskStatus
from no_human.core.db import Store
from no_human.api.app import app


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "testdata" / "lane_conformance.json"
NODE_TEST_PATH = Path(__file__).resolve().parents[1] / "web" / "src" / "laneConformance.test.mjs"

_FIXTURES = json.loads(FIXTURE_PATH.read_text())["cases"]


def _ids() -> list[str]:
    return [c["name"] for c in _FIXTURES]


# --------------------------------------------------------------------------- #
# The shared fixtures                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("case", _FIXTURES, ids=_ids())
def test_lane_matches_the_shared_fixture(case):
    assert lane_for(case["task"]) == case["lane"]


@pytest.mark.parametrize("case", _FIXTURES, ids=_ids())
def test_is_waiting_matches_the_shared_fixture(case):
    assert is_waiting(case["task"]) is case["waiting"]


def test_every_task_status_the_product_can_produce_has_a_fixture():
    """A new TaskStatus must arrive with a lane decision, not fall through the
    unknown-status default in silence."""
    covered = {c["task"].get("status") for c in _FIXTURES}
    missing = sorted(s.value for s in TaskStatus if s.value not in covered)
    assert missing == [], f"TaskStatus values with no lane fixture: {missing}"


def test_the_node_test_reads_this_exact_fixture_file():
    """Both runners must read ONE file. A second copy would let them agree with
    themselves while disagreeing with each other."""
    assert NODE_TEST_PATH.exists()
    assert "testdata/lane_conformance.json" in NODE_TEST_PATH.read_text()


def test_every_fixture_expects_a_real_lane_key():
    for case in _FIXTURES:
        assert case["lane"] in LANE_KEYS, case["name"]


# --------------------------------------------------------------------------- #
# Routing semantics, stated directly (not only via the fixture table)          #
# --------------------------------------------------------------------------- #

def test_blocked_splits_on_the_wake_condition():
    assert lane_for({"status": "blocked", "blocker_wake_condition": "ci_green_on:main"}) == "working"
    assert lane_for({"status": "blocked"}) == "answer"


def test_a_status_is_never_claimed_by_two_lanes():
    seen: dict[str, str] = {}
    for key, statuses in LANE_STATUSES:
        for status in statuses:
            assert status not in seen, f"{status} claimed by {seen.get(status)} and {key}"
            seen[status] = key


def test_the_outcome_lanes_still_route():
    """The trap recorded in boardLanes.js: filter the routing table down to the
    lanes the board renders and finished work reappears as in-flight."""
    assert lane_for({"status": "done"}) == "done"
    assert lane_for({"status": "failed"}) == "failed"


def test_lane_for_reads_attributes_as_well_as_keys():
    """The board endpoint routes a TaskSummaryOut, not a dict."""
    class _Summary:
        status = "blocked"
        blocker_wake_condition = "ci_green_on:main"

    assert lane_for(_Summary()) == "working"
    assert is_waiting(_Summary()) is True


def test_lane_for_accepts_a_task_status_enum():
    class _Summary:
        status = TaskStatus.AWAITING_APPROVAL

    assert lane_for(_Summary()) == "review"


def test_lane_for_survives_none():
    assert lane_for(None) == "working"
    assert is_waiting(None) is False


# --------------------------------------------------------------------------- #
# The board endpoint ships the lane                                            #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "lanes.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (TaskStatus.PENDING, "working"),
        (TaskStatus.IMPLEMENTING, "working"),
        (TaskStatus.AWAITING_APPROVAL, "review"),
        (TaskStatus.AWAITING_INPUT, "answer"),
        (TaskStatus.ESCALATED, "answer"),
        (TaskStatus.PAUSED_QUOTA, "working"),
        (TaskStatus.DONE, "done"),
        (TaskStatus.FAILED, "failed"),
    ],
)
async def test_board_endpoint_ships_the_lane(client, store, status, expected):
    t = Task.new(f"lane {status.value}", repo_path="/tmp/repo")
    await store.create_task(t)
    if status != TaskStatus.PENDING:
        await store.set_status(t, status, validate=False)

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    row = next(x for x in r.json() if x["id"] == t.id)
    assert row["lane"] == expected


@pytest.mark.asyncio
async def test_board_lane_follows_the_blocker_not_just_the_status(client, store):
    parked = Task.new("parked on its own signal", repo_path="/tmp/repo")
    await store.create_task(parked)
    await store.set_status(parked, TaskStatus.BLOCKED, validate=False)
    parked.blocker = {"question": "waiting on CI", "wake_condition": "ci_green_on:main"}
    await store.update_task(parked)

    stuck = Task.new("needs a human", repo_path="/tmp/repo")
    await store.create_task(stuck)
    await store.set_status(stuck, TaskStatus.BLOCKED, validate=False)
    stuck.blocker = {"question": "which database?"}
    await store.update_task(stuck)

    rows = {x["id"]: x for x in (await client.get("/api/tasks")).json()}
    assert rows[parked.id]["lane"] == "working"
    assert rows[stuck.id]["lane"] == "answer"


@pytest.mark.asyncio
async def test_board_lane_agrees_with_lane_for_on_every_row(client, store):
    """Whatever the endpoint sends must be what the shared function says - no
    second computation living in the endpoint."""
    for status in TaskStatus:
        t = Task.new(f"row {status.value}", repo_path="/tmp/repo")
        await store.create_task(t)
        if status != TaskStatus.PENDING:
            await store.set_status(t, status, validate=False)

    rows = (await client.get("/api/tasks")).json()
    assert len(rows) == len(list(TaskStatus))
    for row in rows:
        assert row["lane"] == lane_for(row), row["status"]
