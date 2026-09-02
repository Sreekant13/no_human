"""The loopback boundary on the unauthenticated board API.

Three checks, all in `api/app.py`:
  * `_require_local_host`        — Host must be loopback (DNS-rebinding).
  * `_refuse_cross_origin_writes` — a cross-origin browser write is refused.
  * CORS `allow_origin_regex`    — a cross-origin page cannot read responses.
  * `ws_board`                   — the same Host/Origin gate on the WebSocket.

A same-user non-browser client (the `nh` CLI, the MCP bridge) sends no
`Origin`, so it is unaffected — the tests pin that too.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from starlette.testclient import TestClient
from fastapi import FastAPI

from no_human.api.app import app, ws_board
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.config import load_config

EVIL = "https://evil.example"
LOCAL = "http://localhost:8420"


@pytest_asyncio.fixture
async def client(tmp_path):
    store = await Store(tmp_path / "b.db").connect()
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://localhost") as c:
        yield c
    await store.close()


def _rule(title):
    return {"title": title, "content": "be nice", "tags": [], "project": None}


# ------------------------------- writes ------------------------------------ #

@pytest.mark.asyncio
async def test_cross_origin_write_is_refused(client):
    r = await client.post("/api/rules", json=_rule("x1"), headers={"Origin": EVIL})
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "cross_origin_refused"


@pytest.mark.asyncio
async def test_lookalike_origin_is_refused(client):
    # exact-host match, not startswith: localhost.evil.com is an attacker domain
    r = await client.post("/api/rules", json=_rule("x2"),
                          headers={"Origin": "http://localhost.evil.com"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_absent_origin_write_is_allowed(client):
    # the nh CLI / MCP bridge send no Origin — must still work
    r = await client.post("/api/rules", json=_rule("x3"))
    assert r.status_code != 403, r.text


@pytest.mark.asyncio
async def test_same_origin_write_is_allowed(client):
    r = await client.post("/api/rules", json=_rule("x4"), headers={"Origin": LOCAL})
    assert r.status_code != 403, r.text


# -------------------------------- Host ------------------------------------- #

@pytest.mark.asyncio
async def test_non_loopback_host_is_refused(client):
    # DNS rebinding: the request reaches us with the attacker's domain in Host
    r = await client.get("/api/tasks", headers={"Host": "attacker.example"})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "bad_host"


@pytest.mark.asyncio
async def test_loopback_host_passes(client):
    r = await client.get("/api/tasks", headers={"Host": "127.0.0.1:8420"})
    assert r.status_code == 200, r.text


# --------------------------------- read ------------------------------------ #

@pytest.mark.asyncio
async def test_cross_origin_read_gets_no_cors_grant(client):
    r = await client.get("/api/tasks", headers={"Origin": EVIL})
    # request still processes, but the browser is told nothing: no ACAO for evil
    acao = r.headers.get("access-control-allow-origin")
    assert acao != EVIL and acao != "*", f"leaked ACAO={acao!r}"


@pytest.mark.asyncio
async def test_same_origin_read_is_granted(client):
    r = await client.get("/api/tasks", headers={"Origin": LOCAL})
    assert r.headers.get("access-control-allow-origin") == LOCAL


# ------------------------------ websocket ---------------------------------- #

def _ws_shim(store):
    shim = FastAPI()
    shim.state.store = store
    shim.websocket("/ws")(ws_board)
    return shim


# TestClient hardcodes the WebSocket `Host` to "testserver" regardless of
# base_url, so the tests set `host` explicitly to isolate each check. A real
# browser sends the true Host/Origin, which is what production sees.

@pytest.mark.asyncio
async def test_ws_cross_origin_is_rejected(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    store = await Store(tmp_path / "ws.db").connect()
    try:
        with TestClient(_ws_shim(store)) as tc:
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect(
                        "/ws", headers={"host": "localhost", "origin": EVIL}) as ws:
                    ws.receive_text()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ws_non_loopback_host_is_rejected(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    store = await Store(tmp_path / "wsh.db").connect()
    try:
        with TestClient(_ws_shim(store)) as tc:
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect(
                        "/ws", headers={"host": "attacker.example", "origin": LOCAL}) as ws:
                    ws.receive_text()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ws_same_origin_is_accepted(tmp_path):
    store = await Store(tmp_path / "ws2.db").connect()
    try:
        t = Task.new("visible", repo_path="/tmp/r")
        t.acceptance_criteria = ["n/a"]
        await store.create_task(t)
        with TestClient(_ws_shim(store)) as tc:
            with tc.websocket_connect(
                    "/ws", headers={"host": "localhost", "origin": LOCAL}) as ws:
                msg = ws.receive_json()
                assert msg["type"] == "init"
    finally:
        await store.close()
