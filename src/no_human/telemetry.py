"""Anonymous, opt-in usage telemetry (server-side events).

Default OFF (`telemetry.enabled: false` — consent). When enabled, a CLOSED
allowlist of event kinds is buffered to ``~/.no_human/telemetry-queue.jsonl``
and flushed in small batches by a daemon thread to the first-party ingestion
endpoint. Everything is fail-open: a dead endpoint, a full disk or a malformed
queue line can never break a task run — the only exception `record` raises on
purpose is ``ValueError`` for an event kind or prop outside the allowlist,
because an unlisted event is a privacy bug, not an operational hiccup.

NEVER include: task ids, titles, repo names, paths, prompts, tokens. Props are
validated against `_ALLOWED_EVENTS` — kind AND prop names are closed sets.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# Closed allowlist: event kind -> allowed prop names. Anything else raises.
_ALLOWED_EVENTS: dict[str, frozenset[str]] = {
    "app_started": frozenset(),
    "task_created": frozenset({"source"}),
    "task_completed": frozenset({"status", "duration_bucket", "attempts"}),
    "task_failed": frozenset({"category"}),
    "approve_clicked": frozenset(),
    "feature_used": frozenset({"name"}),
}

MAX_QUEUE_LINES = 500   # bounded buffer; oldest lines dropped first
FLUSH_BATCH = 50        # max events per POST
_HTTP_TIMEOUT = 3.0

_LOCK = threading.Lock()


def _queue_path() -> Path:
    # Resolved per call (not module-level) so a temp-HOME test suite never
    # touches the operator's real ~/.no_human.
    return Path.home() / ".no_human" / "telemetry-queue.jsonl"


def duration_bucket(minutes: float) -> str:
    """Bucket a task duration so no precise timing ever leaves the machine."""
    if minutes < 10:
        return "<10m"
    if minutes < 30:
        return "10-30m"
    if minutes < 60:
        return "30-60m"
    return ">60m"


def _conf(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is None:
        from .config import load_config
        config = load_config().data
    section = config.get("telemetry") or {}
    return section if isinstance(section, dict) else {}


def enabled(config: dict[str, Any] | None = None) -> bool:
    section = _conf(config)
    return bool(section.get("enabled")) and bool(str(section.get("endpoint") or "").strip())


def record(kind: str, config: dict[str, Any] | None = None, **props: Any) -> None:
    """Queue one telemetry event. No-op unless consented AND endpoint set.

    Raises ``ValueError`` for a kind or prop name outside `_ALLOWED_EVENTS`
    (validated even when disabled — an unlisted event is a bug either way).
    Every other failure is swallowed: telemetry must never break the caller.
    """
    allowed = _ALLOWED_EVENTS.get(kind)
    if allowed is None:
        raise ValueError(f"telemetry: unknown event kind {kind!r}")
    unknown = set(props) - set(allowed)
    if unknown:
        raise ValueError(
            f"telemetry: props {sorted(unknown)!r} not allowed for {kind!r}")
    try:
        section = _conf(config)
        if not (bool(section.get("enabled"))
                and str(section.get("endpoint") or "").strip()):
            return
        event = {"kind": kind, "ts": int(time.time()), "props": props}
        _append(event)
        _spawn_flush(section)
    except ValueError:
        raise
    except Exception:
        return  # fail-open


def _append(event: dict[str, Any]) -> None:
    path = _queue_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if path.exists():
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        lines.append(json.dumps(event, separators=(",", ":")))
        if len(lines) > MAX_QUEUE_LINES:
            lines = lines[-MAX_QUEUE_LINES:]  # drop-oldest
        path.write_text("\n".join(lines) + "\n")


def _spawn_flush(section: dict[str, Any]) -> None:
    threading.Thread(
        target=flush, args=(section,), name="nh-telemetry-flush", daemon=True,
    ).start()


def flush(section: dict[str, Any] | None = None,
          config: dict[str, Any] | None = None) -> int:
    """POST up to `FLUSH_BATCH` queued events to the ingestion endpoint.

    Returns the number of events actually sent (0 on any failure — the queue
    keeps them for a later flush; fail-open, 3s timeout, stdlib urllib only).
    """
    if section is None:
        section = _conf(config)
    endpoint = str(section.get("endpoint") or "").strip()
    if not (bool(section.get("enabled")) and endpoint):
        return 0
    try:
        path = _queue_path()
        with _LOCK:
            if not path.exists():
                return 0
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
            batch_lines = lines[:FLUSH_BATCH]
        if not batch_lines:
            return 0
        events = []
        for ln in batch_lines:
            try:
                events.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # a corrupt line is dropped, never re-sent forever
        from . import __version__  # the same string `nh --version` prints
        body = json.dumps({
            "instance_id": str(section.get("instance_id") or ""),
            "version": __version__,
            "events": events,
        }).encode()
        from urllib import request as _urlrequest
        req = _urlrequest.Request(
            endpoint, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with _urlrequest.urlopen(req, timeout=_HTTP_TIMEOUT):
            pass
        with _LOCK:
            # Only what we sent is removed; anything queued meanwhile stays.
            current = []
            if path.exists():
                current = [ln for ln in path.read_text().splitlines() if ln.strip()]
            sent = set(batch_lines)
            kept = [ln for ln in current if ln not in sent]
            if kept:
                path.write_text("\n".join(kept) + "\n")
            else:
                path.write_text("")
        return len(events)
    except Exception:
        return 0  # fail-open: events stay queued
