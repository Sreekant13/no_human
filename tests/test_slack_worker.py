"""Slack Socket-Mode worker foundation (SCRUM-60). Mirrors test_jira_intake.py's
style: tokens are read from the env and must never appear in a log line; the
transport (slack_sdk's SocketModeClient) is monkeypatched EXPLICITLY, per test
that needs it — never via an autouse fixture — so every test exercises the
real SlackWorker code path and only stubs the slack_sdk network boundary."""

from __future__ import annotations

import importlib
import logging

import pytest

from no_human.config import DEFAULT_CONFIG
from no_human.integrations.slack.worker import SlackWorker

# Tests here reach ``config.load_env_var``, which reads the operator's real
# ``~/.no_human/.env`` BEFORE the process env — so `monkeypatch.setenv` LOSES
# to a configured machine and three of these fail with the operator's own
# token. Requested by NAME through `usefixtures`, never an autouse marker:
# this removes an ambient input, it does not stub the SlackWorker under test,
# so the module docstring's rule above still holds. See tests/conftest.py.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


class _FakeRequest:
    def __init__(self, type_, envelope_id, payload):
        self.type = type_
        self.envelope_id = envelope_id
        self.payload = payload


class _FakeSocketModeClient:
    """Stand-in for slack_sdk's SocketModeClient: records connect/close/ack
    calls instead of touching a real websocket."""
    instances = []

    def __init__(self, app_token=None, web_client=None):
        self.app_token = app_token
        self.web_client = web_client
        self.socket_mode_request_listeners = []
        self.connect_calls = 0
        self.close_calls = 0
        self.sent_responses = []
        _FakeSocketModeClient.instances.append(self)

    def connect(self):
        self.connect_calls += 1

    def close(self):
        self.close_calls += 1

    def send_socket_mode_response(self, response):
        self.sent_responses.append(response)


def _patch_transport(monkeypatch):
    """Stub only the slack_sdk boundary (SocketModeClient/WebClient) — the
    same per-test monkeypatch discipline test_jira_intake.py uses for httpx."""
    _FakeSocketModeClient.instances.clear()
    monkeypatch.setattr(
        "no_human.integrations.slack.worker.SocketModeClient", _FakeSocketModeClient)
    monkeypatch.setattr(
        "no_human.integrations.slack.worker.WebClient", lambda token=None: object())


def _worker(monkeypatch, bot="BOT-TOK", app="APP-TOK", **kw):
    monkeypatch.setenv("SLACK_BOT_TOKEN", bot)
    monkeypatch.setenv("SLACK_APP_TOKEN", app)
    return SlackWorker(**kw)


# ------------------------------- tokens -------------------------------------


def test_tokens_loaded_via_load_env_var(monkeypatch):
    w = _worker(monkeypatch, bot="B1", app="A1")
    assert w._bot_token == "B1"
    assert w._app_token == "A1"


def test_missing_tokens_raise_without_leaking(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as exc:
        SlackWorker()
    assert "SLACK_BOT_TOKEN" in str(exc.value)
    assert "SLACK_APP_TOKEN" in str(exc.value)


def test_tokens_never_logged(monkeypatch, caplog):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch, bot="SUPERBOT", app="SUPERAPP")
    w.on_event("app_mention", lambda evt: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.DEBUG):
        w.start()
        client = _FakeSocketModeClient.instances[-1]
        request = _FakeRequest("events_api", "env-1", {"event": {"type": "app_mention"}})
        client.socket_mode_request_listeners[0](client, request)
        w.stop()
    assert "SUPERBOT" not in caplog.text
    assert "SUPERAPP" not in caplog.text


# ------------------------------- lifecycle -----------------------------------


def test_start_initiates_socket_mode_connection(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)
    assert w.is_running is False
    w.start()
    assert w.is_running is True
    client = _FakeSocketModeClient.instances[-1]
    assert client.connect_calls == 1
    assert client.app_token == "APP-TOK"
    # start() is idempotent — a second call must not reconnect.
    w.start()
    assert client.connect_calls == 1
    assert len(_FakeSocketModeClient.instances) == 1


def test_stop_cleanly_closes_connection(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)
    w.start()
    client = _FakeSocketModeClient.instances[-1]
    w.stop()
    assert w.is_running is False
    assert client.close_calls == 1
    # stop() is idempotent — a second call, or one before start(), is a no-op.
    w.stop()
    assert client.close_calls == 1


def test_stop_before_start_is_a_noop(monkeypatch):
    w = _worker(monkeypatch)
    w.stop()  # must not raise
    assert w.is_running is False


# --------------------------- handler registration -----------------------------


def test_on_event_registers_and_invokes_matching_handler(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)
    seen = []
    w.on_event("app_mention", lambda evt: seen.append(evt))
    w.start()
    client = _FakeSocketModeClient.instances[-1]
    listener = client.socket_mode_request_listeners[0]

    payload = {"event": {"type": "app_mention", "text": "hi"}}
    listener(client, _FakeRequest("events_api", "env-42", payload))

    assert seen == [{"type": "app_mention", "text": "hi"}]
    # Every request is ack'd immediately, regardless of event type.
    assert len(client.sent_responses) == 1


def test_on_event_ignores_non_matching_event_type(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)
    seen = []
    w.on_event("app_mention", lambda evt: seen.append(evt))
    w.start()
    client = _FakeSocketModeClient.instances[-1]
    listener = client.socket_mode_request_listeners[0]

    listener(client, _FakeRequest("events_api", "env-1", {"event": {"type": "message"}}))
    assert seen == []


def test_on_event_supports_multiple_handlers_for_same_type(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)
    calls = []
    w.on_event("app_mention", lambda evt: calls.append("first"))
    w.on_event("app_mention", lambda evt: calls.append("second"))
    w.start()
    client = _FakeSocketModeClient.instances[-1]
    listener = client.socket_mode_request_listeners[0]
    listener(client, _FakeRequest("events_api", "env-1", {"event": {"type": "app_mention"}}))
    assert calls == ["first", "second"]


def test_non_events_api_requests_are_acked_but_not_routed(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)
    seen = []
    w.on_event("hello", lambda evt: seen.append(evt))
    w.start()
    client = _FakeSocketModeClient.instances[-1]
    listener = client.socket_mode_request_listeners[0]
    listener(client, _FakeRequest("hello", "env-1", {}))
    assert seen == []
    assert len(client.sent_responses) == 1


def test_handler_exception_does_not_crash_dispatch(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)

    def boom(evt):
        raise ValueError("nope")

    w.on_event("app_mention", boom)
    w.start()
    client = _FakeSocketModeClient.instances[-1]
    listener = client.socket_mode_request_listeners[0]
    listener(client, _FakeRequest("events_api", "env-1", {"event": {"type": "app_mention"}}))
    # No exception propagated, and the ack still went out.
    assert len(client.sent_responses) == 1


# ---------------------------- disabled by default -----------------------------


def test_intake_disabled_by_default_in_config():
    assert DEFAULT_CONFIG["integrations"]["slack"]["intake"] is False


def test_importing_worker_module_has_no_side_effects_without_tokens(monkeypatch, tmp_path):
    """If the module (or its package __init__) ever grew a top-level
    ``SlackWorker(...)`` call, re-executing it with no tokens anywhere would
    raise (SlackWorker.__init__ requires SLACK_BOT_TOKEN/SLACK_APP_TOKEN).
    It doesn't — proving zero import-time side effects."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setattr("no_human.config.ENV_PATH", tmp_path / "empty.env")

    import no_human.integrations.slack as pkg
    import no_human.integrations.slack.worker as w

    importlib.reload(w)
    importlib.reload(pkg)


def test_disabled_by_default_constructs_no_client_when_never_started(monkeypatch):
    """Mirrors how a supervisor honors ``integrations.slack.intake`` being
    False (the default): it simply never constructs/starts a SlackWorker.
    Guard the OTHER half here — even if a worker exists, nothing under
    slack_sdk is touched (no client constructed) until .start() is called."""
    _patch_transport(monkeypatch)
    assert DEFAULT_CONFIG["integrations"]["slack"]["intake"] is False
    _worker(monkeypatch)
    assert _FakeSocketModeClient.instances == []


# ---------------------- reply() ReplyFn (D4c wiring) ------------------------
# The worker holds the bot token and is the only object that can post; the
# AppMentionHandler needs a ReplyFn to answer a mention. These bind reply()
# and its web_client lifecycle (start stores it, stop clears it).

class _FakeWebClient:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, *, channel, thread_ts, text):
        self.posts.append((channel, thread_ts, text))


def test_reply_posts_in_thread_via_web_client(monkeypatch):
    w = _worker(monkeypatch)
    fake = _FakeWebClient()
    w._web_client = fake  # exactly what start() assigns
    w.reply("C123", "1700000000.0001", "on it")
    assert fake.posts == [("C123", "1700000000.0001", "on it")]


def test_reply_before_start_or_after_stop_is_a_noop(monkeypatch):
    w = _worker(monkeypatch)  # never started -> no client to post through
    w.reply("C123", "1.0", "dropped, not raised")  # must not raise
    assert w._web_client is None


def test_start_stores_web_client_and_stop_clears_it(monkeypatch):
    _patch_transport(monkeypatch)
    w = _worker(monkeypatch)
    assert w._web_client is None
    w.start()
    assert w._web_client is not None  # reply() has a client to post through
    w.stop()
    assert w._web_client is None      # a reply racing teardown is a no-op
