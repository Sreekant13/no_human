"""Slack Socket-Mode intake worker (SCRUM-60: 1/3 of SCRUM-27's split).

Modeled on ``intake/jira_poll.py``'s shape: a self-contained worker with an
explicit start/stop lifecycle. ``nh serve`` owns the lifecycle externally (it
constructs the worker and calls ``.start()``/``.stop()``) — the worker never
registers itself with the supervisor (mirrors the jira_poll template).

This sub-task builds only the transport (Socket Mode connect/disconnect) and
event routing (``.on_event(event_type, handler)``); actual @mention handling
is downstream work (SCRUM-61/62) that attaches handlers via that API.

Tokens (``SLACK_BOT_TOKEN``, ``SLACK_APP_TOKEN``) are loaded from
``~/.no_human/.env`` via the existing :func:`load_env_var` path ONLY, at
worker initialization — never from ``config.yaml``, never logged. When
``integrations.slack.intake`` is disabled (the default), nothing here runs:
the caller simply never constructs a :class:`SlackWorker`, so there is zero
import-time side effect.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from ...config import load_env_var

log = logging.getLogger("no_human.integrations.slack")

EventHandler = Callable[[dict], None]


class SlackWorker:
    """Socket-Mode worker: connects to Slack, routes Events API events to
    registered handlers. Tokens are resolved once at ``__init__`` and never
    exposed again — not in an attribute repr, a log line, or an exception
    message."""

    def __init__(self, config: dict | None = None, *, on_event=None):
        self._config = config or {}
        self._on_event = on_event or (lambda kind, text: None)
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._client: SocketModeClient | None = None
        self._web_client: WebClient | None = None

        bot_token = load_env_var("SLACK_BOT_TOKEN")
        app_token = load_env_var("SLACK_APP_TOKEN")
        if not bot_token or not app_token:
            # Never interpolate the (empty) token value here — only report
            # which var names are missing.
            missing = [n for n, v in (("SLACK_BOT_TOKEN", bot_token),
                                       ("SLACK_APP_TOKEN", app_token)) if not v]
            raise RuntimeError(
                f"missing {', '.join(missing)} in ~/.no_human/.env"
            )
        self._bot_token = bot_token
        self._app_token = app_token

    def on_event(self, event_type: str, handler: EventHandler) -> None:
        """Register ``handler(event_payload)`` for Events API events whose
        ``event.type`` == ``event_type`` (e.g. ``"app_mention"``). Multiple
        handlers may register for the same event type; all are invoked."""
        self._handlers[event_type].append(handler)

    @property
    def is_running(self) -> bool:
        return self._client is not None

    def reply(self, channel: str, thread_ts: str, text: str) -> None:
        """Post ``text`` as an in-thread reply — the ``ReplyFn`` an
        ``AppMentionHandler`` calls to answer a mention (missing/unknown repo,
        or a creation failure). Best-effort by contract: the handler wraps it
        in ``_safe_reply``. A call before ``start()`` or after ``stop()`` is a
        no-op rather than an error, so a reply that races teardown never
        raises. Uses the client ``start()`` built; the bot token itself is
        never re-exposed."""
        client = self._web_client
        if client is None:
            return
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

    def _dispatch(self, client: SocketModeClient, request: SocketModeRequest) -> None:
        """Socket-mode request listener: ack immediately (Slack requires a
        response within 3s), then route Events API events to handlers.
        A handler exception is logged and never crashes the worker or drops
        the ack."""
        client.send_socket_mode_response(
            SocketModeResponse(envelope_id=request.envelope_id)
        )
        if request.type != "events_api":
            return
        event = (request.payload or {}).get("event") or {}
        event_type = event.get("type")
        if not event_type:
            return
        for handler in self._handlers.get(event_type, []):
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 — one bad handler must never take down the worker
                log.warning("slack handler for %s failed: %s", event_type, type(exc).__name__)
                self._on_event("slack_handler_error", f"{event_type}: {type(exc).__name__}")

    def start(self) -> None:
        """Open the Socket Mode connection. Idempotent — a second call while
        already running is a no-op."""
        if self._client is not None:
            return
        self._web_client = WebClient(token=self._bot_token)
        client = SocketModeClient(app_token=self._app_token, web_client=self._web_client)
        client.socket_mode_request_listeners.append(self._dispatch)
        client.connect()
        self._client = client
        self._on_event("slack_worker_started", "socket mode connected")

    def stop(self) -> None:
        """Close the Socket Mode connection. Idempotent — a second call, or
        one before ``start()``, is a no-op."""
        if self._client is None:
            return
        client, self._client = self._client, None
        self._web_client = None
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 — best-effort teardown
            log.warning("slack worker close failed: %s", type(exc).__name__)
        self._on_event("slack_worker_stopped", "socket mode closed")
