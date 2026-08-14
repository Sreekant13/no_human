"""Single task-level PR resolver.

A task's "current PR" has never had one home. `_finalize` writes
`ctx["pr_watch"]` after a *delivering* PR open (`orchestrator.py`); an
already-satisfied resume (`_gate_already_satisfied`) sets AWAITING_APPROVAL
without ever writing it; a code-review draft (`_open_draft_pr_for_review`)
deliberately does not write `attempts.pr_url` either. Live incident: task
`16f850ae` opened draft PR #230 on attempt 4 (an already-satisfied resume),
no attempt ever recorded a `pr_url`, `pr_watch` was never set — so
`WakeWatcher._check_open_pr` read `ctx.get("pr_watch")`, got nothing, and the
task heartbeated "nothing to do" for over an hour after the PR was merged.

This module is the one place that answers "what PR does this task have,
right now" — every caller (the watcher's rungs, the already-satisfied
backfill) goes through it instead of reading `pr_watch` directly.

Rungs, in priority order, first non-empty wins:
  1. ``pr_watch``    — a `_finalize`-delivered PR. Unchanged current
                        behaviour; every task that already worked keeps
                        resolving here first.
  2. an attempt's ``pr_url`` — the newest non-empty one across all attempts
                        of the task (`Store.latest_attempt_pr_url`). This is
                        the inheritance rung the acceptance criteria pin.
  3. a live draft slot — ``ctx["pr_draft_created"]``, unless that URL was
                        already abandoned (`ctx["abandoned_pr_urls"]`). This
                        is the rung the *live incident* actually needed.

``None`` and ``""`` both collapse to "absent" everywhere in here — never
treated as different facts.

No writes, no network calls. ``store``/``task`` are duck-typed (only
``store.latest_attempt_pr_url`` and ``task.id``/``task.context`` are used) so
this module does not import ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedPR:
    """The task's current PR, and which rung produced it.

    ``source`` is one of ``"pr_watch"``, ``"attempt"``, ``"draft"``, or
    ``"none"`` — ``url``/``branch`` are ``""`` exactly when ``source`` is
    ``"none"``.
    """

    url: str
    branch: str
    source: str


def _clean(value: Any) -> str:
    """``None``/whitespace/anything falsy collapses to ``""`` — the one place
    null-vs-empty-string ambiguity is resolved, so no caller has to."""
    return str(value or "").strip()


async def resolve_task_pr(store: Any, task: Any) -> ResolvedPR:
    ctx = task.context or {}

    watch_url = _clean(ctx.get("pr_watch"))
    if watch_url:
        return ResolvedPR(watch_url, _clean(ctx.get("pr_branch")), "pr_watch")

    attempt_url = _clean(await store.latest_attempt_pr_url(task.id))
    if attempt_url:
        branch = _clean(ctx.get("pr_branch")) or _clean(ctx.get("pr_draft_branch"))
        return ResolvedPR(attempt_url, branch, "attempt")

    draft_url = _clean(ctx.get("pr_draft_created"))
    if draft_url:
        abandoned = {_clean(u) for u in (ctx.get("abandoned_pr_urls") or [])}
        if draft_url not in abandoned:
            return ResolvedPR(draft_url, _clean(ctx.get("pr_draft_branch")), "draft")

    return ResolvedPR("", "", "none")


_PR_EVENT_KINDS = {"pr_open", "pr_draft"}


async def task_has_pr_evidence(store: Any, task: Any) -> str:
    """The PR this task is known to have, or ``""`` — the ONE question "is
    there something for the human to merge?" fails CLOSED on.

    Live incident (8c8b36b5): a draft PR opened pre-review is recorded only
    as ``context["pr_draft_created"]``, never on an attempt row. The two
    "is there a PR to merge" call sites that read ``attempts.pr_url`` alone
    missed it and completed the task while its PR (#253) sat open. This is
    the ONE place both must call instead.

    First tries `resolve_task_pr` (covers `pr_watch` -> `attempts.pr_url` ->
    `pr_draft_created`, minus `abandoned_pr_urls`). If that comes up empty,
    scans persisted `task_events` for any event whose `kind` is in
    ``{"pr_open", "pr_draft"}`` and reads its URL from `pr_url` (the
    `pr_draft` shape — ``orchestrator.emit("pr_draft", ..., pr_url=url)``)
    falling back to `text` (the `pr_open` shape — the URL IS the emitted
    text there). Not abandoned, not counted. This is the belt-and-braces
    rung for a PR recorded only in the event log and nowhere else.
    """
    resolved = await resolve_task_pr(store, task)
    if resolved.url:
        return resolved.url

    ctx = task.context or {}
    abandoned = {_clean(u) for u in (ctx.get("abandoned_pr_urls") or [])}
    for e in await store.list_events(task.id):
        if e.get("kind") not in _PR_EVENT_KINDS:
            continue
        url = _clean(e.get("pr_url")) or _clean(e.get("text"))
        if url and url not in abandoned:
            return url
    return ""


async def task_pr_urls(store: Any, task: Any) -> list[str]:
    """Every PR URL this task is known to have, deduped, minus
    ``abandoned_pr_urls`` — read-only, no network.

    ``resolve_task_pr``/``task_has_pr_evidence`` each answer "the ONE PR to
    look at", first non-empty rung wins. A completion closeout needs the
    OPPOSITE question — "every PR this task might still have open" — because
    a task can accumulate more than one live URL (e.g. ``pr_watch`` plus a
    separately recorded ``pr_draft`` event for an earlier attempt's PR that
    was never explicitly abandoned). This scans every source those two
    functions read, unions them, and returns all surviving URLs instead of
    stopping at the first.
    """
    ctx = task.context or {}
    abandoned = {_clean(u) for u in (ctx.get("abandoned_pr_urls") or [])}

    urls: list[str] = []
    for candidate in (
        _clean(ctx.get("pr_watch")),
        _clean(ctx.get("pr_delivered_url")),
        _clean(ctx.get("pr_draft_created")),
        _clean(await store.latest_attempt_pr_url(task.id)),
    ):
        if candidate:
            urls.append(candidate)

    for e in await store.list_events(task.id):
        if e.get("kind") not in _PR_EVENT_KINDS:
            continue
        url = _clean(e.get("pr_url")) or _clean(e.get("text"))
        if url:
            urls.append(url)

    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not url.startswith("http") or url in abandoned or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out
