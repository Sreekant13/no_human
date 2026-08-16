"""Opt-in usage telemetry (src/no_human/telemetry.py) + the CSP variants.

Pins the privacy contract: a CLOSED event allowlist (unknown kind/prop raises),
zero network without consent, the exact ingestion body shape with consent, a
CSP that is byte-identical to the historical value while disabled and gains
exactly the two PostHog hosts when enabled, and an /api/config echo that keeps
the PUBLISHABLE PostHog client token intact (the field is deliberately named
`posthog_publishable` so the secret scrubber's name-based rules do not eat it).
"""
from __future__ import annotations

import contextlib
import importlib
import json

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human import telemetry
from no_human.api.app import app
from no_human.config import Config
from no_human.core.db import Store

app_module = importlib.import_module("no_human.api.app")

# The CSP the board has always sent — the disabled variant must stay
# BYTE-IDENTICAL to this literal (not compared via the constant, on purpose).
_CSP_TODAY = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'"
)

_ENABLED = {
    "enabled": True,
    "endpoint": "https://ingest.invalid/collect",
    "instance_id": "11111111-2222-3333-4444-555555555555",
    "posthog_publishable": "phc_test",
}


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def _urlopen(req, timeout=None):
        calls.append((req, timeout))
        return contextlib.nullcontext()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return calls


@pytest.fixture
def no_thread(monkeypatch):
    """Make record() deterministic: no background flush thread in tests."""
    monkeypatch.setattr(telemetry, "_spawn_flush", lambda section: None)


# ------------------------- closed allowlist ------------------------------- #

def test_unknown_event_kind_raises():
    with pytest.raises(ValueError, match="unknown event kind"):
        telemetry.record("task_opened", config={"telemetry": _ENABLED})


def test_unknown_prop_raises_even_for_known_kind():
    # `title` is exactly the class of thing that must never ship.
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record("task_created", config={"telemetry": _ENABLED},
                         source="feature", title="secret title")


def test_allowlist_is_the_documented_closed_set():
    assert telemetry._ALLOWED_EVENTS == {
        "app_started": frozenset(),
        "task_created": frozenset({"source"}),
        "task_completed": frozenset({"status", "duration_bucket", "attempts"}),
        "task_failed": frozenset({"category"}),
        "approve_clicked": frozenset(),
        "feature_used": frozenset({"name"}),
    }


def test_hook_normalizes_unknown_task_kinds_to_other(monkeypatch):
    """Task.kind is an unvalidated str at the API layer, so the orchestrator
    hook must clamp `source` to the known vocabulary — a client-invented kind
    (which could carry title-like text) leaves the machine only as "other"."""
    from no_human.core.orchestrator import Orchestrator

    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    class _Stub:  # only what the hook touches
        config: dict = {}
        _telemetry_hook = Orchestrator._telemetry_hook

    stub = _Stub()
    stub._telemetry_hook("kind", {"task_kind": "my secret project name"})
    stub._telemetry_hook("kind", {"task_kind": "feature"})
    stub._telemetry_hook("kind", {})
    sources = [p["source"] for k, p in sent if k == "task_created"]
    assert sources == ["other", "feature", "unknown"]


# ------------------------- consent gate ----------------------------------- #

def test_disabled_records_nothing_and_touches_no_network(temp_home, no_network):
    telemetry.record("app_started", config={"telemetry": {"enabled": False,
                                                          "endpoint": "https://x"}})
    assert not (temp_home / ".no_human" / "telemetry-queue.jsonl").exists()
    assert telemetry.flush({"enabled": False, "endpoint": "https://x"}) == 0
    assert no_network == []


def test_enabled_without_endpoint_is_a_noop(temp_home, no_network):
    telemetry.record("app_started",
                     config={"telemetry": {"enabled": True, "endpoint": ""}})
    assert not (temp_home / ".no_human" / "telemetry-queue.jsonl").exists()
    assert no_network == []


# ------------------------- ingestion contract ----------------------------- #

def test_enabled_flush_posts_the_contract_body(temp_home, no_network, no_thread):
    telemetry.record("task_completed", config={"telemetry": _ENABLED},
                     status="done", duration_bucket="<10m", attempts=2)
    sent = telemetry.flush(_ENABLED)
    assert sent == 1
    assert len(no_network) == 1
    req, timeout = no_network[0]
    assert timeout == pytest.approx(3.0)
    assert req.full_url == _ENABLED["endpoint"]
    body = json.loads(req.data.decode())
    assert set(body) == {"instance_id", "version", "events"}
    assert body["instance_id"] == _ENABLED["instance_id"]
    from no_human import __version__
    assert body["version"] == __version__
    [event] = body["events"]
    assert event["kind"] == "task_completed"
    assert event["props"] == {"status": "done", "duration_bucket": "<10m",
                             "attempts": 2}
    # sent events leave the queue — a second flush sends nothing
    assert telemetry.flush(_ENABLED) == 0
    assert len(no_network) == 1


def test_queue_is_bounded_drop_oldest(temp_home, no_thread):
    for i in range(telemetry.MAX_QUEUE_LINES + 25):
        telemetry.record("feature_used", config={"telemetry": _ENABLED},
                         name=f"f{i}")
    lines = (temp_home / ".no_human" / "telemetry-queue.jsonl").read_text().splitlines()
    assert len(lines) == telemetry.MAX_QUEUE_LINES
    # the OLDEST events were dropped, the newest survived
    assert json.loads(lines[-1])["props"]["name"] == f"f{telemetry.MAX_QUEUE_LINES + 24}"
    assert json.loads(lines[0])["props"]["name"] == "f25"


def test_default_endpoint_is_empty_so_no_cloud_identifier_ships():
    # The L3 brain invariant bans cloud deployment identifiers from the local
    # product's source; the ingestion URL therefore arrives via config.yaml
    # (like team_brain.control_plane_url), and the shipped default is inert.
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["telemetry"]["endpoint"] == ""
    assert DEFAULT_CONFIG["telemetry"]["enabled"] is False


def test_duration_bucketing():
    assert telemetry.duration_bucket(0) == "<10m"
    assert telemetry.duration_bucket(9.9) == "<10m"
    assert telemetry.duration_bucket(10) == "10-30m"
    assert telemetry.duration_bucket(45) == "30-60m"
    assert telemetry.duration_bucket(61) == ">60m"


# ------------------------- CSP variants ----------------------------------- #

def test_csp_disabled_is_byte_identical_to_today():
    assert app_module._build_csp({}) == _CSP_TODAY
    assert app_module._build_csp({"telemetry": {"enabled": False}}) == _CSP_TODAY
    # enabled but unconfigured (no client token) also stays strict
    assert app_module._build_csp(
        {"telemetry": {"enabled": True, "posthog_publishable": ""}}) == _CSP_TODAY


def test_csp_enabled_adds_exactly_the_two_posthog_hosts():
    got = app_module._build_csp(
        {"telemetry": {"enabled": True, "posthog_publishable": "phc_x"}})
    assert got == (
        "default-src 'self'; "
        "script-src 'self' https://us-assets.i.posthog.com; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss: "
        "https://us.i.posthog.com https://us-assets.i.posthog.com; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )


# ------------------------- /api/config echo ------------------------------- #

@pytest_asyncio.fixture
async def client(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    app.state.store = s
    app.state.config = Config(
        data={"telemetry": {
            "enabled": False,
            "posthog_publishable": "phc_test_publishable_token",
            "posthog_host": "https://us.i.posthog.com",
            "endpoint": "https://ingest.invalid/collect",
            "instance_id": "",
        }},
        path=tmp_path / "config.yaml",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await s.close()


@pytest.mark.asyncio
async def test_config_echo_keeps_publishable_token_intact(client):
    r = await client.get("/api/config")
    assert r.status_code == 200
    tel = r.json()["telemetry"]
    # The PUBLISHABLE client token must survive the scrub — the browser needs
    # it to init the client. (The field name avoids the scrubber's name rules
    # by design; weakening the scrubber itself would be the wrong fix.)
    assert tel["posthog_publishable"] == "phc_test_publishable_token"
    assert tel["enabled"] is False
    assert tel["posthog_host"] == "https://us.i.posthog.com"
    assert tel["endpoint"] == "https://ingest.invalid/collect"


@pytest.mark.asyncio
async def test_csp_header_default_matches_today(client):
    # No lifespan ran in this fixture, so the middleware falls back to the
    # strict constant — which must be the historical byte-identical value.
    if hasattr(app.state, "csp"):
        del app.state.csp
    r = await client.get("/api/config")
    assert r.headers["Content-Security-Policy"] == _CSP_TODAY


# ------------------------- consent endpoint -------------------------------- #

@pytest.mark.asyncio
async def test_consent_write_refused_without_same_origin(client):
    # A write with no Origin (curl / local malicious process) is refused…
    r = await client.put("/api/telemetry/consent", json={"enabled": True})
    assert r.status_code == 403
    # …and so is a cross-origin browser write (drive-by page).
    r = await client.put("/api/telemetry/consent", json={"enabled": True},
                         headers={"Origin": "http://localhost.evil.com"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_consent_enable_mints_stable_id_persists_and_widens_csp(
        client, tmp_path, monkeypatch):
    import yaml

    from no_human import config as config_mod
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("telemetry:\n  enabled: false\n"
                        "  posthog_publishable: phc_test_publishable_token\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    origin = {"Origin": "http://127.0.0.1:8787"}
    r = await client.put("/api/telemetry/consent", json={"enabled": True},
                         headers=origin)
    assert r.status_code == 200
    assert r.json()["reload_required"] is True

    on_disk = yaml.safe_load(cfg_path.read_text())["telemetry"]
    assert on_disk["enabled"] is True
    minted = on_disk["instance_id"]
    assert len(minted) == 36  # uuid4, minted server-side on first enable

    # CSP recomputed live: the widened variant is now served…
    r = await client.get("/api/config")
    assert "us-assets.i.posthog.com" in r.headers["Content-Security-Policy"]

    # …and toggling off both restores the strict header and KEEPS the id
    # (one stable anonymous id, not a fresh "new install" per toggle).
    r = await client.put("/api/telemetry/consent", json={"enabled": False},
                         headers=origin)
    assert r.status_code == 200
    on_disk = yaml.safe_load(cfg_path.read_text())["telemetry"]
    assert on_disk["enabled"] is False
    assert on_disk["instance_id"] == minted
    r = await client.get("/api/config")
    assert "posthog" not in r.headers["Content-Security-Policy"]

    r = await client.put("/api/telemetry/consent", json={"enabled": True},
                         headers=origin)
    assert yaml.safe_load(cfg_path.read_text())["telemetry"]["instance_id"] == minted
