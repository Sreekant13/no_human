"""Integrations WRITE path — FIELD_SPECS + save_integration_config + the
PUT /api/integrations/{name}/config endpoint.

CRITICAL SAFETY: every test monkeypatches no_human.config.ENV_PATH and
no_human.config.CONFIG_PATH onto tmp_path — the real ~/.no_human is never
read or written.
"""
from __future__ import annotations

import stat

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human.api.app import app
from no_human.core.db import Store
from no_human.integrations import (
    FIELD_SPECS,
    integration_fields,
    save_integration_config,
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------- #
# Unit-level: save_integration_config + the env-file upsert                    #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    """Redirect the module-level path constants off the real ~/.no_human, and
    make sure no secret env var leaks in from the real process environment."""
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    for var in (
        "JIRA_API_TOKEN", "CIRCLECI_TOKEN", "JENKINS_USER", "JENKINS_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_env_upsert_appends_new_key_and_creates_file_0600(tmp_path):
    save_integration_config("jira", {"api_token": "tok-123"})
    env_path = nh_config.ENV_PATH
    assert env_path.exists()
    assert _mode(env_path) == 0o600
    assert "JIRA_API_TOKEN=tok-123" in env_path.read_text()


def test_env_value_newline_injection_rejected_and_file_unchanged(tmp_path):
    """A value containing \\n (or \\r) must never reach the .env file — it
    would let an attacker inject an arbitrary extra line, e.g. forging a
    CLAUDE_CODE_OAUTH_TOKEN override."""
    env_path = nh_config.ENV_PATH
    assert not env_path.exists()

    with pytest.raises(ValueError):
        save_integration_config(
            "jira", {"api_token": "x\nCLAUDE_CODE_OAUTH_TOKEN=attacker"}
        )

    # No file was created at all — the write was never dispatched.
    assert not env_path.exists()


def test_env_value_carriage_return_injection_rejected_on_existing_file(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("JIRA_API_TOKEN=old-value\n")
    env_path.chmod(0o600)
    before = env_path.read_text()

    with pytest.raises(ValueError):
        save_integration_config(
            "jira", {"api_token": "x\rCLAUDE_CODE_OAUTH_TOKEN=attacker"}
        )

    # File is byte-for-byte unchanged — no extra line, no injected key.
    assert env_path.read_text() == before
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_path.read_text()


def test_env_upsert_never_world_readable_at_creation(tmp_path):
    """Regression: the file must be 0600 from the very first byte on disk —
    never briefly created at the process umask (e.g. 0644) before a later
    chmod. We can't observe the intermediate state directly (the write is a
    single atomic os.replace), so we assert the *only* path a reader could
    ever observe the file at is already 0600."""
    env_path = nh_config.ENV_PATH
    assert not env_path.exists()

    save_integration_config("jira", {"api_token": "tok-123"})

    # Immediately after a fresh-create save, mode must already be 0600 — and
    # there must be no leftover .tmp sibling from the atomic-write dance.
    assert _mode(env_path) == 0o600
    assert not env_path.with_name(env_path.name + ".tmp").exists()


def test_env_upsert_replaces_existing_key_preserves_unrelated_lines(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# a comment\n"
        "CLAUDE_CODE_OAUTH_TOKEN=unrelated-token\n"
        "JIRA_API_TOKEN=old-value\n"
        "\n"
        "CIRCLECI_TOKEN=other-secret\n"
    )
    env_path.chmod(0o600)

    save_integration_config("jira", {"api_token": "new-value"})

    content = env_path.read_text()
    assert "# a comment" in content
    assert "CLAUDE_CODE_OAUTH_TOKEN=unrelated-token" in content
    assert "CIRCLECI_TOKEN=other-secret" in content
    assert "JIRA_API_TOKEN=new-value" in content
    assert "JIRA_API_TOKEN=old-value" not in content
    # Exactly one JIRA_API_TOKEN line — replaced, not duplicated.
    assert content.count("JIRA_API_TOKEN=") == 1
    assert _mode(env_path) == 0o600


def test_env_upsert_keeps_mode_0600_after_write(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SOMETHING=else\n")
    env_path.chmod(0o644)  # simulate a loosely-permissioned file

    save_integration_config("circleci", {"api_token": "cc-tok"})

    assert _mode(env_path) == 0o600


def test_secret_value_never_appears_in_returned_status(tmp_path):
    status = save_integration_config("jira", {"api_token": "super-secret-value"})
    import dataclasses
    serialized = str(dataclasses.asdict(status))
    assert "super-secret-value" not in serialized


def test_unknown_integration_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="unknown integration"):
        save_integration_config("mystery", {"api_token": "x"})


def test_unknown_field_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        save_integration_config("jira", {"not_a_real_field": "x"})


def test_config_yaml_nonsecret_round_trip_preserves_unrelated_keys(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "server:\n  host: 127.0.0.1\n  port: 8420\nintegrations:\n"
        "  jira:\n    unrelated_note: keep-me\n"
    )

    status = save_integration_config(
        "jira", {"site": "https://acme.atlassian.net", "project_key": "PROJ", "email": "me@x.com"}
    )
    assert status.configured is True

    import yaml
    on_disk = yaml.safe_load(config_path.read_text())
    assert on_disk["server"]["host"] == "127.0.0.1"
    assert on_disk["integrations"]["jira"]["unrelated_note"] == "keep-me"
    assert on_disk["integrations"]["jira"]["site"] == "https://acme.atlassian.net"
    assert on_disk["integrations"]["jira"]["project_key"] == "PROJ"
    assert on_disk["integrations"]["jira"]["email"] == "me@x.com"

    # Re-read via the real loader confirms the value is actually live.
    reloaded = nh_config.load_config(nh_config.CONFIG_PATH)
    assert reloaded.data["integrations"]["jira"]["site"] == "https://acme.atlassian.net"


def test_field_specs_cover_every_integration():
    assert set(FIELD_SPECS) == {"jira", "linear", "github", "gitlab", "jenkins",
                                "circleci", "slack", "teams"}
    for name, specs in FIELD_SPECS.items():
        for spec in specs:
            assert bool(spec.env_var) != bool(spec.config_path), name


def test_integration_fields_reports_set_booleans(tmp_path):
    save_integration_config("jira", {"site": "https://acme.atlassian.net"})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    fields = integration_fields("jira", cfg)
    by_name = {f["name"]: f for f in fields}
    assert by_name["site"]["set"] is True
    assert by_name["api_token"]["set"] is False  # never set in this test
    assert all(set(f) == {"name", "label", "secret", "set"} for f in fields)


# --------------------------------------------------------------------------- #
# API-level: PUT /api/integrations/{name}/config + GET /api/integrations       #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    # This route writes ~/.no_human/.env, so it requires a local Origin like
    # the auth route does. Its only caller is the browser (web/src/api.js),
    # which always sends one — the fixture mirrors that legitimate client.
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


@pytest.mark.asyncio
async def test_put_unknown_integration_404(client):
    r = await client.put("/api/integrations/mystery/config", json={"fields": {"x": "y"}})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_unknown_field_422(client):
    r = await client.put(
        "/api/integrations/jira/config", json={"fields": {"bogus_field": "y"}}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_success_returns_refreshed_status_and_fields_no_secret(client):
    r = await client.put(
        "/api/integrations/jira/config",
        json={"fields": {
            "site": "https://acme.atlassian.net",
            "project_key": "PROJ",
            "email": "me@x.com",
            "api_token": "leak-me-not",
        }},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "jira"
    assert body["configured"] is True
    assert "leak-me-not" not in r.text
    fields = {f["name"]: f for f in body["fields"]}
    assert fields["site"]["set"] is True
    assert fields["api_token"]["secret"] is True
    assert fields["api_token"]["set"] is True
    # And the raw value is genuinely absent from the whole response body.
    assert "leak-me-not" not in str(body)


@pytest.mark.asyncio
async def test_get_integrations_includes_fields_with_set_booleans(client, mock_ambient_probes):
    r = await client.get("/api/integrations")
    assert r.status_code == 200
    body = r.json()
    jira = next(x for x in body["integrations"] if x["name"] == "jira")
    assert "fields" in jira
    assert all(f["set"] is False for f in jira["fields"])  # nothing configured yet

    await client.put(
        "/api/integrations/circleci/config",
        json={"fields": {"org_slug": "gh/acme", "project": "svc", "api_token": "cc-secret"}},
    )
    r2 = await client.get("/api/integrations")
    circleci = next(x for x in r2.json()["integrations"] if x["name"] == "circleci")
    by_name = {f["name"]: f for f in circleci["fields"]}
    assert by_name["org_slug"]["set"] is True
    assert by_name["api_token"]["set"] is True
    assert "cc-secret" not in r2.text
