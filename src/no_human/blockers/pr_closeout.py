"""Close a task's open PR(s) when it reaches DONE by a path other than the
merge flow — the merge path (`vcs.approve_merge.land_task`) already closes
the PR it just merged, and the abandon path (`orchestrator._abandon_draft_pr`)
already closes a draft it walks away from. Neither of those covers a
completion that never merges: `nh approve --landed` (a human asserting the
content already landed elsewhere), and the shipped/containment
landed-completion rungs (`blockers/shipped.py`) that reach the same
conclusion automatically. Both used to leave the task's PR open forever —
live incident: PRs #387, #382, #375 found dangling after exactly this.

``close_task_prs_on_completion`` is the shared hook both call, once, right
after they write ``TaskStatus.DONE``. It enumerates every PR
``vcs.task_pr.task_pr_urls`` still knows about for the task, closes each with
a bare (no-comment) forge call, and records one ``pr_closed_on_completion``
event per successful close.

**Best-effort, fail-open.** By the time this runs the task is already DONE —
that write already committed. A forge error (auth, 404, rate limit, a merged
PR the forge refuses to close) must never roll that back or raise past this
function: a completed task with a PR that failed to close is strictly better
than losing the completion over a transient API error. Every close is
attempted independently; one failure does not stop the rest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..vcs.task_pr import task_pr_urls

log = logging.getLogger("no_human.wake")

PR_CLOSED_ON_COMPLETION_KIND = "pr_closed_on_completion"

CloseFn = Callable[[str], dict]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def close_task_prs_on_completion(
    store: Any, task: Any, *, completion_path: str,
    close: CloseFn | None = None,
) -> list[str]:
    """Close every open PR this task still has, record one event per closed
    PR, and return the URLs actually closed. Never raises.

    ``close`` defaults to ``vcs.comment_poster.close_pr`` (imported lazily —
    keeps ``blockers`` -> ``vcs`` a call-time dependency, matching every
    other caller in this package), run via ``asyncio.to_thread`` since it
    shells out. Tests inject a fake here instead of monkeypatching the forge.
    """
    try:
        urls = await task_pr_urls(store, task)
    except Exception as exc:  # noqa: BLE001 — enumeration must never block completion
        log.warning("pr enumeration failed for %s: %s", task.id[:8], exc)
        return []
    if not urls:
        return []

    if close is None:
        from ..vcs.comment_poster import close_pr as close

    closed: list[str] = []
    events: list[dict[str, Any]] = []
    for url in urls:
        try:
            res = await asyncio.to_thread(close, url)
        except Exception as exc:  # noqa: BLE001 — one forge failure must not stop the rest
            log.warning("could not close %s on completion (%s): %s",
                       url, completion_path, exc)
            continue
        if not res.get("ok"):
            log.warning("could not close %s on completion (%s): %s",
                       url, completion_path, res.get("error"))
            continue
        closed.append(url)
        ts_iso = _now_iso()
        events.append({
            "source": "system",
            "kind": PR_CLOSED_ON_COMPLETION_KIND,
            "task_id": task.id,
            "pr_id": url,
            "pr_url": url,
            "completion_path": completion_path,
            "closed_timestamp": ts_iso,
            "ts": time.time(),
            "text": f"closed {url} on completion via {completion_path}",
        })

    if events:
        try:
            await store.save_events(task.id, events)
        except Exception as exc:  # noqa: BLE001 — the PR is already closed; never undo that
            log.warning("could not record pr_closed_on_completion for %s: %s",
                       task.id[:8], exc)

    return closed
