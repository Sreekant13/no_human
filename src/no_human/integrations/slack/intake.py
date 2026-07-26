"""Slack ``app_mention`` intake handler (SCRUM-61: 2/3 of SCRUM-27's split).

Registers against the :class:`~no_human.integrations.slack.worker.SlackWorker`
built in sub-task 1 (its ``on_event`` API) and turns an ``app_mention`` event
into a ``no_human`` task — mirroring ``intake/jira_poll.py``'s shape:
normalize, validate, dedupe, create.

Message shape: the bot's own ``<@U...>`` mention is stripped, then the first
line becomes the task title and the remaining lines become the description
verbatim. A ``repo:<name>`` token (first match wins) selects which onboarded
project the task runs against. Validation is against the ``projects`` table —
the only name-keyed repo registry this codebase has (there is no separate
config-file allowlist; ``get_project_by_name``/``list_projects`` are the same
lookups the rest of the app uses to resolve a project by name). A missing or
unrecognized token gets an in-thread reply asking for one and creates NO task.

Dedupe mirrors jira_poll: ``source="slack"`` + ``external_id=f"{channel}+{ts}"``,
looked up via a filtered ``(source, external_id)`` query (no DB UNIQUE
constraint — same as jira) so a Slack retry delivery of the same event never
spawns a second task.

The worker's dispatch callback runs synchronously on slack_sdk's SocketMode
thread while task creation is async (the Store); ``register`` captures the
calling context's running event loop and bridges each event across via
``run_coroutine_threadsafe``. Tests drive the async ``_handle`` core directly,
so the threading bridge itself carries no business logic worth re-testing.
The bridge's future is never discarded silently — a done-callback logs any
exception ``_handle`` raised, so a failure on the SocketMode thread is never
invisible.

Two deliveries of the *same* event can run concurrently on the same loop
(Slack's own retry racing the original delivery), so the dedupe
check-then-insert is serialized behind a per-handler ``asyncio.Lock`` — the
handler only ever runs against a single in-process ``Store``/loop, so a
process-local lock closes the race completely.

``ReplyFn`` is invoked synchronously from the event loop; a real
``chat_postMessage`` adapter is blocking HTTP and must bridge via
``asyncio.to_thread`` (or an async client) when it's wired up in 3/3 — that
wiring is explicitly out of this sub-task's scope. Any exception a reply
raises here is caught and logged, never left to swallow the surrounding
event's telemetry.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

from ...core.db import Store
from ...core.task import Task
from ...profile import apply_default_task_config

log = logging.getLogger("no_human.integrations.slack.intake")

# (channel, thread_ts, text) -> None — an in-thread Slack reply. Production
# wiring adapts this from a WebClient's chat_postMessage; tests pass a
# capturing fake.
ReplyFn = Callable[[str, str, str], None]

_MENTION_RE = re.compile(r"^\s*<@[^>]+>\s*")
_REPO_RE = re.compile(r"repo:\s*(\S+)")


def extract_repo_token(text: str) -> str | None:
    """First ``repo:<name>`` match wins. ``None`` for both a missing token and
    a malformed one (``repo:`` with no non-whitespace name after it) — both
    route to the same "please specify a repo" reply."""
    m = _REPO_RE.search(text or "")
    return m.group(1).strip() if m else None


def parse_message(text: str) -> tuple[str, str]:
    """Strip the leading bot mention, then split first line (title) from the
    rest (description, verbatim — including any ``repo:`` token it contains)."""
    body = _MENTION_RE.sub("", text or "", count=1)
    lines = body.splitlines()
    title = lines[0].strip() if lines else ""
    description = "\n".join(lines[1:]).strip()
    return title or "Slack task", description


class AppMentionHandler:
    """One instance per configured Slack intake; ``register`` wires it to a
    ``SlackWorker``'s ``on_event("app_mention", ...)``."""

    def __init__(self, store: Store, reply: ReplyFn, *,
                 config: dict | None = None, on_event=None):
        self.store = store
        self.reply = reply
        self.config = config or {}
        self._on_event = on_event or (lambda kind, text: None)
        self._loop: asyncio.AbstractEventLoop | None = None
        # Serializes dedupe-check + create against concurrent deliveries of
        # the same event (see module docstring).
        self._lock = asyncio.Lock()

    def register(self, worker) -> None:
        """Capture the calling context's running loop (best-effort — tests
        may call this outside a loop since they drive ``_handle`` directly)
        and attach the sync dispatch wrapper to the worker."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        worker.on_event("app_mention", self._dispatch)

    def _dispatch(self, event: dict) -> None:
        """Sync entry point invoked on the worker's SocketMode callback
        thread — bridges into the async ``_handle`` on the captured loop."""
        if self._loop is None:
            log.warning("app_mention received before register() captured a loop; dropped")
            return
        future = asyncio.run_coroutine_threadsafe(self._handle(event), self._loop)
        future.add_done_callback(self._log_handle_failure)

    def _log_handle_failure(self, future: "asyncio.Future") -> None:
        exc = future.exception()
        if exc is not None:
            log.error("app_mention handler failed: %s", type(exc).__name__)

    def _safe_reply(self, channel: str, thread_ts: str, text: str) -> None:
        """Invoke ``self.reply`` without letting a transport failure escape —
        an in-thread reply is best-effort; it must never crash the handler
        or suppress the event telemetry that follows it."""
        try:
            self.reply(channel, thread_ts, text)
        except Exception as exc:  # noqa: BLE001 — logged, never raised
            log.warning("slack reply failed: %s", type(exc).__name__)

    async def _existing_slack_task(self, external_id: str) -> bool:
        return await self.store.get_task_by_source_external_id("slack", external_id) is not None

    async def _handle(self, event: dict) -> None:
        channel = event.get("channel") or ""
        ts = event.get("ts") or ""
        thread_ts = event.get("thread_ts") or ts
        text = event.get("text") or ""
        external_id = f"{channel}+{ts}"

        title, description = parse_message(text)
        repo_token = extract_repo_token(text)

        available = [p.name for p in await self.store.list_projects()]

        if not repo_token:
            self._safe_reply(
                channel, thread_ts,
                "Please specify a repository using format: repo:<name>. "
                f"Available repos: {available}",
            )
            self._on_event("slack_mention_missing_repo", external_id)
            return

        project = await self.store.get_project_by_name(repo_token)
        if project is None:
            self._safe_reply(
                channel, thread_ts,
                f"Repository '{repo_token}' not recognized. "
                f"Available repos: {available} (repo names are case-sensitive)",
            )
            self._on_event("slack_mention_unknown_repo", f"{external_id}: {repo_token}")
            return

        # Dedupe check + create are serialized behind `_lock`: two concurrent
        # deliveries of the same event (Slack's own retry racing the
        # original) both pass validation identically, so without a lock both
        # could observe "not yet created" and both insert. Dedupe AFTER
        # validation (mirrors jira_poll) — a re-delivered event carries the
        # same text, so it would already have passed validation once; this
        # ordering never blocks a legitimate first attempt.
        async with self._lock:
            if await self._existing_slack_task(external_id):
                self._on_event("slack_mention_dedup", external_id)
                return

            task = Task.new(
                title, source="slack", repo_path=project.primary_repo,
                description=description, external_id=external_id,
            )
            if project.primary_repo:
                profile = await self.store.get_profile(project.primary_repo)
                task.config = apply_default_task_config(profile, task.config)

            try:
                await self.store.create_task(task)
            except Exception as exc:  # noqa: BLE001 — surfaced in-thread, never raised into dispatch
                log.warning("slack task creation failed: %s", type(exc).__name__)
                self._safe_reply(
                    channel, thread_ts,
                    f"Failed to create the task: {type(exc).__name__}. Please retry.",
                )
                self._on_event("slack_mention_create_failed", external_id)
                return

        self._on_event("slack_task_created", f"{external_id}: {task.title}")
