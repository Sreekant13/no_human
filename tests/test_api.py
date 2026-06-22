"""FastAPI board endpoint tests (Phase 4 DoD)."""
from __future__ import annotations

import json
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.core.task import Task, TaskStatus
from no_human.api.app import app
from no_human.core.db import Store


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store):
    app.state.store = store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_task(store: Store, *, status=TaskStatus.PENDING, title="Fix thing") -> Task:
    t = Task.new(title, repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    if status != TaskStatus.PENDING:
        await store.set_status(t, status, validate=False)
    return t


# --------------------------------------------------------------------------- #
# GET /api/tasks                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_list_tasks_empty(client):
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_tasks_returns_summary(client, store):
    await _seed_task(store, title="Alpha")
    await _seed_task(store, title="Beta", status=TaskStatus.IMPLEMENTING)
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    titles = {t["title"] for t in data}
    assert titles == {"Alpha", "Beta"}
    # summary shape — no attempts field
    for item in data:
        assert "id" in item
        assert "status" in item
        assert "attempts" not in item


@pytest.mark.asyncio
async def test_list_tasks_status_values(client, store):
    await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.get("/api/tasks")
    assert r.json()[0]["status"] == "awaiting_approval"


# --------------------------------------------------------------------------- #
# GET /api/tasks/{id}                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_task_detail(client, store):
    t = await _seed_task(store)
    r = await client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert data["title"] == t.title
    assert data["acceptance_criteria"] == ["Should work"]
    assert "attempts" in data


@pytest.mark.asyncio
async def test_get_task_404(client):
    r = await client.get("/api/tasks/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_task_prefix_lookup(client, store):
    t = await _seed_task(store)
    r = await client.get(f"/api/tasks/{t.id[:8]}")
    assert r.status_code == 200
    assert r.json()["id"] == t.id


# --------------------------------------------------------------------------- #
# GET /api/tasks/{id}/diff                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_diff_no_repo(client, store):
    t = Task.new("No repo", repo_path=None)
    await store.create_task(t)
    r = await client.get(f"/api/tasks/{t.id}/diff")
    assert r.status_code == 200
    assert r.text == ""


@pytest.mark.asyncio
async def test_get_diff_no_attempts(client, store):
    t = await _seed_task(store)
    r = await client.get(f"/api/tasks/{t.id}/diff")
    assert r.status_code == 200
    assert r.text == ""


# --------------------------------------------------------------------------- #
# POST /api/tasks/{id}/approve                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_approve_awaiting(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # "never merges" language must be present
    assert "never merges" in data["message"].lower() or "agent never merges" in data["message"].lower()


@pytest.mark.asyncio
async def test_approve_wrong_status_409(client, store):
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_approve_records_timestamp(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await client.post(f"/api/tasks/{t.id}/approve")
    refreshed = await store.find_task(t.id)
    assert refreshed.context.get("approved_at") is not None


@pytest.mark.asyncio
async def test_approve_404(client):
    r = await client.post("/api/tasks/no-such-task/approve")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/tasks/{id}/send-back                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_send_back_stores_feedback(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(
        f"/api/tasks/{t.id}/send-back",
        json={"message": "Handle the edge case with empty input."},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    refreshed = await store.find_task(t.id)
    feedback = refreshed.context.get("send_back_feedback", [])
    assert len(feedback) == 1
    assert "edge case" in feedback[0]["message"]


@pytest.mark.asyncio
async def test_send_back_resets_to_implementing(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await client.post(
        f"/api/tasks/{t.id}/send-back",
        json={"message": "Please redo."},
    )
    refreshed = await store.find_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_send_back_accumulates_feedback(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "First."})
    # reset back to awaiting to send again
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "Second."})
    refreshed = await store.find_task(t.id)
    feedback = refreshed.context.get("send_back_feedback", [])
    assert len(feedback) == 2


@pytest.mark.asyncio
async def test_send_back_404(client):
    r = await client.post("/api/tasks/ghost/send-back", json={"message": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_send_back_missing_message_422(client, store):
    t = await _seed_task(store)
    r = await client.post(f"/api/tasks/{t.id}/send-back", json={})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Static SPA path resolution                                                   #
# --------------------------------------------------------------------------- #

def test_web_dist_path_points_at_repo_web_dir():
    """_WEB_DIST must resolve to <repo>/web/dist — not above the repo. A wrong
    parents[] index silently breaks SPA serving (the API just 404s on /)."""
    from pathlib import Path

    from no_human.api.app import _WEB_DIST

    repo_root = Path(__file__).resolve().parents[1]  # tests/ -> repo root
    assert _WEB_DIST == repo_root / "web" / "dist"


def test_board_lanes_cover_every_task_status():
    """Every TaskStatus must map to a board lane — otherwise a task in that state
    silently vanishes from the UI (regression: parked states were dropped)."""
    import re
    from pathlib import Path

    from no_human.core.task import TaskStatus

    board = (Path(__file__).resolve().parents[1] / "web" / "src" / "Board.jsx").read_text()
    # Collect every status string listed in a `statuses: [...]` array.
    listed: set[str] = set()
    for arr in re.findall(r"statuses:\s*\[([^\]]*)\]", board):
        listed |= set(re.findall(r'"([a-z_]+)"', arr))
    missing = {s.value for s in TaskStatus} - listed
    assert not missing, f"task statuses with no board lane: {sorted(missing)}"
