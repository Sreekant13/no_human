"""GET /api/integrations/jira/issues — Task 1.6's Import-from-Jira read side.

Mirrors tests/test_jira_intake.py's mocking style (httpx.get monkeypatched on
the intake.jira module) and tests/test_integrations_write.py's API-client
fixture (isolated ENV_PATH/CONFIG_PATH, ASGITransport). No task is ever
created here — POST /api/tasks stays the one create path.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human.api.app import app
from no_human.core.db import Store


def _cfg(**over):
    j = {"site": "https://acme.atlassian.net", "project_key": "PROJ",
         "email": "me@x.com", "jql": ""}
    j.update(over)
    return {"integrations": {"jira": j}}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    return tmp_path


@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    """A live config the endpoint reads via request.app.state.config.data —
    write config.yaml directly (no HTTP round trip needed for these tests)."""
    import yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_cfg()))
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_unconfigured_returns_503_with_clear_detail(store, tmp_path):
    # No config.yaml written at all this time — jira section stays empty.
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/integrations/jira/issues")
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unconfigured_when_token_missing(client, monkeypatch):
    # site/project_key/email present but no JIRA_API_TOKEN → still unconfigured.
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    r = await client.get("/api/integrations/jira/issues")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_configured_returns_shaped_issues_and_clamps_description(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    long_desc = "x" * 3000
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params, auth=auth)
        return _Resp({"issues": [{
            "key": "PROJ-9",
            "fields": {
                "summary": "Fix the thing",
                "description": long_desc,
                "status": {"name": "In Progress"},
                "assignee": {"displayName": "Ada Lovelace"},
                "updated": "2026-07-18T10:00:00.000+0000",
            },
        }]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "thing"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    item = body[0]
    assert item["key"] == "PROJ-9"
    assert item["summary"] == "Fix the thing"
    assert item["status"] == "In Progress"
    assert item["assignee"] == "Ada Lovelace"
    assert item["updated"] == "2026-07-18T10:00:00.000+0000"
    assert item["url"].endswith("/browse/PROJ-9")
    assert len(item["description"]) == 2000

    # Reused the successor endpoint + Basic auth, exactly like the poller's search().
    assert "/rest/api/3/search/jql" in captured["url"]
    assert captured["auth"] == ("me@x.com", "SEKRET")
    assert 'text ~ "thing"' in captured["params"]["jql"]
    assert "ORDER BY updated DESC" in captured["params"]["jql"]

    # The token never appears anywhere in the response body.
    assert "SEKRET" not in r.text


@pytest.mark.asyncio
async def test_limit_is_clamped_between_1_and_50(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)

    await client.get("/api/integrations/jira/issues", params={"limit": 500})
    assert captured["params"]["maxResults"] == 50

    await client.get("/api/integrations/jira/issues", params={"limit": 0})
    assert captured["params"]["maxResults"] == 1

    await client.get("/api/integrations/jira/issues")  # default
    assert captured["params"]["maxResults"] == 20


@pytest.mark.asyncio
async def test_upstream_error_surfaces_as_502_never_leaking_token(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"errorMessages": ["boom"]}, status_code=500)

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "x"})
    assert r.status_code == 502
    assert "SEKRET" not in r.text


@pytest.mark.asyncio
async def test_upstream_connection_error_surfaces_as_502(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        import httpx
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "x"})
    assert r.status_code == 502
    assert "SEKRET" not in r.text


@pytest.mark.asyncio
async def test_test_connection_loads_token_from_env_not_only_serve(client, tmp_path, monkeypatch):
    """Regression (settings 'Test connection' bug): the health check must load
    JIRA_API_TOKEN from ~/.no_human/.env ITSELF, so it authenticates from a plain
    `nh start` — not only when `nh serve`'s Jira poll happened to load the token.
    Here the token lives ONLY in the .env file (never pre-set in os.environ), yet
    the endpoint must authenticate and never echo the secret back."""
    (tmp_path / ".env").write_text("JIRA_API_TOKEN=secret-shhh\n")
    assert "JIRA_API_TOKEN" not in os.environ  # proves it is not pre-loaded

    import no_human.integrations as integ

    async def _fake_get(url, headers=None, auth=None, timeout=10.0):
        assert auth == ("me@x.com", "secret-shhh")  # token was read from the .env
        return _Resp({"displayName": "Me"}, status_code=200)

    monkeypatch.setattr(integ, "_http_get", _fake_get)

    r = await client.post("/api/integrations/jira/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["healthy"] is True, body
    assert "authenticated" in body["detail"].lower()
    assert "secret-shhh" not in body["detail"]  # never leak the token
