"""THE completion path for "this branch's content is already on base".

Extracted from ``WakeWatcher._complete_if_content_landed`` (wake.py) so a
second caller — the scheduler's resume/restart dispatch gate — can share the
exact same check instead of a copy. ``wake.py`` still owns every rung that
calls into this; this module owns only the shared probe-and-complete body.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from ..core.task import Task, TaskStatus
from ..vcs.pr_outcome import observe_pr
from ..vcs.pr_watcher import commit_is_ancestor

log = logging.getLogger("no_human.wake")

#: Sentinel returned by ``complete_if_content_landed`` when the task went
#: TERMINAL while the (subprocess-heavy) content probe was running. It is not
#: an action: the caller must abandon its tick entirely rather than fall
#: through to its own escalate/send-back path, which is the SCRUM-68 guard the
#: probe's own callers had inline before the path was shared.
_TICK_ABORTED = "__tick_aborted__"


async def complete_if_content_landed(
    store: Any, task: Task, url: str, *,
    pr_shipped: Callable[[str, str, str], Awaitable[bool | str]] | None,
    is_terminal: Callable[[Task], Awaitable[bool]],
    on_event: Callable[[str, str], None],
    forge_state: str | None, action: str,
    situation: str, branch: str | None = None,
) -> str | None:
    """THE completion path for "this branch's content is already on base".

    ONE path, multiple callers — the CLOSED rung (which has always had it,
    inline), the CONFLICTING rung (which used to start a rebase round
    without ever asking), and the scheduler's resume/restart dispatch gate.
    Returns *action* once it has recorded the outcome and written DONE;
    ``_TICK_ABORTED`` if the task went terminal while the probe ran — on ANY
    answer the probe gave, see the guard below, and every caller must abandon
    its tick on it; ``None`` for every "no" — hook not wired, no branch or no
    base recorded, probe error, or content genuinely absent — all of which
    mean the caller keeps its existing behaviour unchanged.

    The question is CONTENT, not ancestry, and that is not a preference:
    this repo lands every PR as an identity-normalized LOCAL squash, so the
    landing commit has no lineage back to the branch and
    ``git merge-base --is-ancestor`` is False for every PR we ever merged.
    See ``default_branch_shipped``.

    WHAT A ``True`` HERE MEANS, precisely — it is stronger than "some of
    this shipped". ``default_branch_shipped`` merges the branch into the
    base tip (both directions) and demands the result be EXACTLY the tip's
    tree, i.e. the branch has nothing left to contribute. A PARTIALLY
    landed branch — half a rename, one of two files, a follow-up commit
    still outstanding — writes a different tree and reads False, so it can
    never complete here. That is pinned by test (the half-landed rename).

    A ``False`` is deliberately overloaded ("absent" and "could not run"
    collapse into it, per that function's contract), which is why it is
    only ever read as "keep going": the caller's existing path — escalate
    to a human, run the rebase round, or dispatch a fresh attempt — is the
    safe side of the ambiguity in every caller.

    REVIEW PRECONDITION (wake.py's rungs only). Neither of wake.py's callers
    may complete a task whose review never passed, and neither needs its own
    check for that: both are reached only through ``_check_open_pr``, which
    ``_evaluate`` calls only for ``AWAITING_APPROVAL`` — the status a task
    reaches only after its review passed and its PR was opened. That, plus a
    recorded ``pr_branch``, is the whole precondition set of the CLOSED rung
    this was extracted from, and it is preserved exactly. Pinned by the test
    that a BLOCKED task with landed content is still never completed. The
    scheduler's resume gate is not gated on AWAITING_APPROVAL — it runs on
    whatever the store's own claimable-status check already allows — but it
    is gated on the same "no PR, no completion" and "still terminal? abort"
    rules below, which is what makes sharing this function safe rather than
    a shortcut.

    LANDED-COMMIT ANCHORING (2026-08-12). ``pr_shipped`` may now return a
    commit SHA instead of a bare ``True`` — ``branch_landed_commit``'s
    contract, wired in by every host (``api/app.py``, ``cli/commands.py``)
    — recording exactly where the content landed rather than merely that
    it did. Two things follow, both required by the incident this fixes
    (two stacked squash trains sharing a file; the earlier one's task
    re-escalated the moment the later one landed, because the old
    tip-only check re-asked the question at a tip that had moved on):

    - A previously recorded ``ctx["landed_sha"]`` is checked FIRST, via
      ``commit_is_ancestor`` — no merge-tree at all — before the probe
      ever runs. Once a landing is known, re-confirming it costs one
      cheap ancestry check forever, not a full content scan every tick.
    - A fresh string result is written to ``ctx["landed_sha"]`` before
      the DONE status is written, so the anchor survives a restart and
      the next tick (or the next incident) does not have to re-derive it.
    ``bool(result)`` keeps every existing caller that injects a plain
    ``True``/``False`` fake working unchanged.
    """
    if pr_shipped is None:
        return None
    ctx = task.context or {}
    # `branch` is the resolver's answer (task_pr.resolve_task_pr) when the
    # caller has one — it may be an inherited attempt's or a draft's
    # branch, not `ctx["pr_branch"]`. Falling back to `ctx["pr_branch"]`
    # keeps every caller that predates the resolver working unchanged.
    if branch is None:
        branch = ctx.get("pr_branch")
    base = ctx.get("base_branch")
    if not task.repo_path or not branch:
        return None
    if not base:
        # NEVER default to "main". The orchestrator persists the resolved
        # base before any attempt runs (`orchestrator.py`: `if not
        # ctx.get("base_branch")` → `_implicit_base_branch`, then
        # `update_task`), so every task that can reach AWAITING_APPROVAL
        # has one and this is unreachable for them. If it is ever reached —
        # a legacy row, a hand-built context — the honest answer is "I do
        # not know what this PR targets", not a guess: a backport opened
        # against `release/2.3` would be asked about `main`, where the
        # content usually IS present, and would complete a task whose work
        # never reached its actual base. Falling back costs a spurious
        # escalation or one wasted rebase round, with a human on the other
        # end of both.
        log.warning("no base_branch recorded for %s — skipping the "
                    "content check rather than guessing", task.id[:8])
        return None
    recorded_sha = ctx.get("landed_sha")
    fresh_sha: str | None = None
    if recorded_sha and await commit_is_ancestor(
            task.repo_path, recorded_sha, base):
        shipped = True
    else:
        try:
            result = await pr_shipped(task.repo_path, branch, base)
        except Exception as exc:  # noqa: BLE001 — a checker error must not crash the watcher
            log.warning("pr_shipped check failed for %s: %s", task.id[:8], exc)
            result = False
        shipped = bool(result)
        if isinstance(result, str):
            fresh_sha = result
    # The shipped check just ran several local git subprocesses (rev-parse,
    # and up to two merge-trees per candidate base tip) — easily a few
    # seconds on a large repo — so re-verify terminal-ness before ANY
    # caller acts, same SCRUM-68 guard as every other rung.
    #
    # 🔴 THIS SITS ABOVE THE `not shipped` EARLY-OUT AND MUST STAY THERE.
    # The guard is about the AWAIT, not about the answer: a `POST /shipped`
    # or `/cancel` landing while the probe ran leaves the caller's own
    # recheck stale on EVERY path out of here. Review 2026-08-11 caught it
    # narrowed to the positive path alone, and reproduced the cost — a
    # phantom "abandon or rework?" blocker, a `pr_closed` event and an
    # outcome row on a task that was already DONE, plus a round counter and
    # a send-back on the conflict rung. The store's CAS refuses only the
    # STATUS flip; nothing else here is CAS-guarded, so a "harmless"
    # refused transition still leaves a record of a state change that never
    # happened — the shape `cli/commands.py`'s restore-approval path
    # already documents as ruled wrong. The probe raising is treated as
    # `shipped=False` (not as an early return) for exactly this reason.
    if await is_terminal(task):
        return _TICK_ABORTED
    if not shipped:
        return None
    landed_sha = recorded_sha if fresh_sha is None else fresh_sha
    if fresh_sha is not None:
        # Written BEFORE the DONE status so the anchor survives a restart
        # even if the process dies between the two writes.
        task.context = await store.merge_context(
            task.id, {"landed_sha": fresh_sha})
    await observe_pr(store, task.id, url, forge_state=forge_state,
                     shipped=True)
    sha_note = f" (landed as {landed_sha[:8]})" if landed_sha else " (squash-merged)"
    shipped_text = (
        f"{task.id[:8]} {situation} but its content is already on "
        f"{base}{sha_note}: {url}"
    )
    # `set_status`'s own event insert is the persistence now (atomic with
    # the status write) — a separate emit would double-persist, so call
    # the host mirror directly instead.
    await store.set_status(
        task, TaskStatus.DONE, validate=False,
        event={"source": "watcher", "kind": "shipped", "text": shipped_text,
               "ts": time.time()},
    )
    on_event("shipped", shipped_text)
    return action


async def complete_if_approved_and_landed(
    store: Any, task: Task, pr_url: str, *,
    branch: str | None = None,
    probe: Callable[[str, str, str], Awaitable[bool | str]] | None = None,
    is_terminal: Callable[[Task], Awaitable[bool]] | None = None,
    on_event: Callable[[str, str], None] | None = None,
) -> str | None:
    """The human-approve path's landed-completion check (`api/app.py`'s
    ``_merge_task_pr`` and ``cli/commands.py``'s ``approve`` command) — a
    thin wrapper over :func:`complete_if_content_landed`, the SAME shared
    check the wake/scheduler rungs use (see the module docstring), so
    "is this branch's content on the default branch?" is answered once, not
    reimplemented per caller. Fixes the live incident where a closed PR
    whose content had already landed via a squash train was re-merged by
    `nh approve`, hit conflicts, and left the task stuck awaiting_approval.

    Defaults ``probe`` to ``vcs.pr_watcher.branch_landed_commit`` (lazy
    import — keeps `blockers` -> `vcs` at call time only, mirroring every
    other caller of this module), and ``is_terminal``/``on_event`` to the
    scheduler's re-read-the-row check (`Scheduler._is_terminal_row`) and a
    no-op respectively — the CLI/API callers have no watcher instance to
    reuse the way `wake.py`'s rungs do, so both are constructed here instead
    of imported from a class.

    Returns ``None`` on every ambiguity (no probe, no branch/base recorded,
    probe error, content genuinely absent) — the caller keeps today's merge
    behaviour exactly. Returns ``_TICK_ABORTED`` if the task went terminal
    while the probe ran — treated the same as "nothing to do" by both
    callers, since either way there is no merge left to attempt. Returns
    ``"approved_landed"`` once the task has been completed, after which an
    extra ``approved_landed`` audit event is appended (`save_events` is
    append-only — `complete_if_content_landed`'s own `set_status` call
    already wrote the `shipped` event and the DONE status; this does not
    re-write status).

    No review-PASS precondition is checked here — this path performs NO
    merge and NO push, and both callers already refuse unless
    ``task.status is AWAITING_APPROVAL``, the same precondition
    `complete_if_content_landed`'s "REVIEW PRECONDITION" paragraph documents
    for wake.py's CLOSED rung.

    Never raises: any exception is logged and swallowed to ``None`` so a
    probe error falls through to the existing merge attempt rather than
    turning an approve into a 500 — fail-open here means "merge like today",
    not a silent completion.
    """
    try:
        if probe is None:
            from ..vcs.pr_watcher import branch_landed_commit
            probe = branch_landed_commit
        if is_terminal is None:
            async def _default_is_terminal(t: Task) -> bool:
                current = await store.get_task(t.id)
                if current is None:
                    return False
                if current.status == TaskStatus.DONE:
                    return True
                return current.status == TaskStatus.FAILED and bool(
                    (current.context or {}).get("cancel_reason"))
            is_terminal = _default_is_terminal
        if on_event is None:
            on_event = lambda kind, text: None  # noqa: E731 — default is a no-op printer/logger

        result = await complete_if_content_landed(
            store, task, pr_url, pr_shipped=probe, is_terminal=is_terminal,
            on_event=on_event, forge_state=None, action="approved_landed",
            situation="approval was requested", branch=branch,
        )
    except Exception as exc:  # noqa: BLE001 — the gate must never turn an approve into a 500
        log.warning("approve landed-completion check failed for %s: %s",
                    task.id[:8], exc)
        return None
    if result != "approved_landed":
        return result  # None (keep merging) or _TICK_ABORTED (abandon the tick)

    ctx = task.context or {}
    base = ctx.get("base_branch") or "the default branch"
    landed = ctx.get("landed_sha") or ""
    sha_note = f" ({landed[:8]})" if landed else ""
    await store.save_events(task.id, [{
        "source": "human", "kind": "approved_landed", "ts": time.time(),
        "text": f"approve: content already on {base}{sha_note} — "
                "completed without a merge",
    }])
    return result
