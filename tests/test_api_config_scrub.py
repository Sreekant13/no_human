"""GET /api/config must never mutate the running config, and must scrub
secret-shaped keys recursively at any nesting depth."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.api.app import app
from no_human.config import Config
from no_human.core.db import Store


@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = Config(
        data={
            "notify": {
                "slack_webhook_url": "https://hooks.slack.com/services/SECRET",
                "channel": "#builds",
            },
            "llm": {
                "primary_model": "claude-sonnet-5",
            },
            "integrations": {
                "jira": {
                    "auth": {
                        "api_token": "sekrit-nested-value",
                    },
                },
            },
        },
        path=tmp_path / "config.yaml",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_show_config_does_not_mutate_running_config(client):
    r1 = await client.get("/api/config")
    assert r1.status_code == 200
    # The in-memory config must still hold the original secret after the
    # handler ran once — the bug was a shallow dict-copy that let the scrub
    # mutate the shared nested dict.
    assert (
        app.state.config.data["notify"]["slack_webhook_url"]
        == "https://hooks.slack.com/services/SECRET"
    )
    # Calling it again must give the same scrubbed result, proving the
    # source config was never damaged by the first call.
    r2 = await client.get("/api/config")
    assert r2.json() == r1.json()
    assert app.state.config.data["notify"]["slack_webhook_url"] == (
        "https://hooks.slack.com/services/SECRET"
    )


@pytest.mark.asyncio
async def test_show_config_scrubs_top_level_secret(client):
    r = await client.get("/api/config")
    data = r.json()
    assert data["notify"]["slack_webhook_url"] == "●●● set"
    assert data["notify"]["channel"] == "#builds"


@pytest.mark.asyncio
async def test_show_config_scrubs_nested_secret_recursively(client):
    r = await client.get("/api/config")
    data = r.json()
    nested = data["integrations"]["jira"]["auth"]["api_token"]
    assert nested == "●●● set"
    # the raw value must never appear anywhere in the response
    assert "sekrit-nested-value" not in r.text


@pytest.mark.asyncio
async def test_show_config_passes_through_empty_secret_values(client):
    store_config = app.state.config
    store_config.data["notify"]["password"] = ""
    store_config.data["notify"]["token"] = None
    r = await client.get("/api/config")
    data = r.json()
    assert data["notify"]["password"] == ""
    assert data["notify"]["token"] is None
