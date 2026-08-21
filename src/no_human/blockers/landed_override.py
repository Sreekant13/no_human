"""The human landed-override: an explicit human confirmation that completes
a task whose content landed via a path automated containment cannot verify.

Three eligible shapes, resolved by ``_resolve_shape``:

- ``"awaiting_approval"`` — a supervising session's squash train that a later
  train car's classification-decision edits, or a union-resolved real source
  conflict, leaves with no candidate commit whose tree matches the branch
  verbatim (see ``vcs/pr_watcher.py``'s ``_contained_at``/``default_branch_shipped``).
- ``"failed_pre_pr"`` — a task that died BEFORE ever opening a PR (budget
  exhaustion, a pre-review test failure, a compile error — any pre-PR cause)
  whose branch content a human later lands by hand. Live incident: task
  5b2246c1 review-PASSed head 9ba09affa, hit the lifetime budget cap before a
  PR ever opened, and the content was hand-landed at ca7bc32cf — leaving the
  board showing FAILED for shipped work, with neither `nh approve --landed`
  (required AWAITING_APPROVAL) nor `nh task restore-approval` (required PR
  evidence) able to repair it. This shape is narrowly gated — no
  `cancel_reason` (a human's explicit kill is never overridden) and no PR
  evidence (`vcs.task_pr.task_has_pr_evidence`) — so it stays confined to the
  actual gap instead of becoming a generic "force any failed task DONE"
  lever; a FAILED task that already opened a PR is refused here and pointed
  at the existing `nh task restore-approval` -> `nh approve --landed` pair.
- ``"pending_never_ran"`` — a task a supervising session hand-lands BEFORE
  any coder attempt ever dispatched: PENDING, no PR, no `base_branch` (the
  normal dispatch-time write, `Orchestrator._implicit_base_branch`, never
  ran either). Live incident: task 855f1263's fix was implemented and landed
  by the supervising session before the coder ran; `nh approve --landed`
  refused PENDING outright, and once refused ONE more time on "no recorded
  base_branch" — a task that never ran can never have recorded one. This
  shape, and ONLY this shape, accepts an explicit ``--base``/``base=`` from
  the human when none was ever recorded (see `approve_landed_override`'s
  base-resolution step) — it is NEVER guessed from git or a profile. An
  earlier version of this fix DID auto-default from
  ``vcs/pr_watcher.resolve_default_branch``'s ``origin/HEAD`` probe, falling
  back to the checkout's own current branch when nothing configures
  ``origin/HEAD`` — which is every onboarded profile on record. That
  fallback is indistinguishable, at the call site, from a real project
  default: an independent fresh-context review of this very fix caught it as
  a second, quieter instance of the false-completion risk task 855f1263 was
  about (a wrong guessed base is worse than refusing), so it was replaced
  with a required, explicit human assertion — `resolve_default_branch` is
  now used only to compose a non-binding hint in the refusal message, never
  to fill in a value. This shape is gated exactly like `failed_pre_pr` on
  carrying no PR evidence (`task_has_pr_evidence`) — a pending task that
  already has a PR belongs to a different repair (or simply awaiting
  review), not this one — and additionally on carrying no pending
  cancellation request (`Store.get_cancel_request`): a cancel racing a
  hand-land must not be silently dropped by the override.

Single shared implementation for both the CLI (`nh approve --landed`) and the
API (`POST /api/tasks/{id}/approve-landed`) — the exact validation and audit
event, so the two surfaces cannot drift.

**No git state is ever touched.** This module imports only read-only probes
(``pr_watcher.commit_is_ancestor``, ``pr_watcher.containment_residue``); it
never constructs ``vcs.git.GitRepo``, never calls ``vcs.approve_merge.land_task``,
never merges or pushes. The override is a human ASSERTION recorded on the
task, not a merge action — constraint #2 (the agent never merges) is
untouched because there is no merge here to begin with. Closing the task's
PR(s) after the DONE write (``pr_closeout.close_task_prs_on_completion``) is
not a merge or a push either — it changes no code and no state the forge
gates on, the same justification ``approve_merge._close_pr`` and the
abandon path already stand on.

This is a HUMAN override of automated containment, not a containment pass:
the audit event's text says so explicitly, and the event's ``kind`` —
``approved_landed_override`` — is deliberately distinct from every kind an
automated path can write (``shipped``, ``approved_landed``, ``human_merged``),
so a reader of the event log can always tell which class of evidence stands
behind a completion.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..core.task import Task, TaskStatus
from ..vcs.pr_watcher import (
    commit_is_ancestor, containment_residue, resolve_default_branch,
)
from ..vcs.task_pr import task_has_pr_evidence
from .pr_closeout import close_task_prs_on_completion

LANDED_OVERRIDE_KIND = "approved_landed_override"

IsAncestor = Callable[[str, str, str], Awaitable[bool]]
ResidueProbe = Callable[[str, str, str], Awaitable[list[str] | None]]


class OverrideRefused(Exception):
    """Raised when a landed-override request fails a precondition. The
    message (``.reason``) is safe to show a human verbatim — it never
    includes anything the caller did not already supply."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _resolve_shape(store: Any, task: Task) -> str:
    """Which eligible shape *task* is, or raise ``OverrideRefused``.

    Fail-closed, checked before any write: an ``AWAITING_APPROVAL`` task is
    the original shape, byte-for-byte unchanged. A ``FAILED`` task is the
    ``"failed_pre_pr"`` shape ONLY when a human never explicitly cancelled it
    (``context["cancel_reason"]``) and it never opened a PR
    (``task_has_pr_evidence`` — the one place that answers that question). A
    ``PENDING`` task (never dispatched) is the ``"pending_never_ran"`` shape
    ONLY when it never opened a PR (the same evidence gate — a pending task
    has no ``context["cancel_reason"]`` concept to begin with, since a human
    cancelling a PENDING task moves it straight to FAILED) and has no PENDING
    cancellation request either (``Store.get_cancel_request`` — the DB
    column a live cancel races through before the status flip lands). Any
    other status, or a FAILED/PENDING task that fails its evidence or cancel
    gate, is refused.
    """
    if task.status is TaskStatus.AWAITING_APPROVAL:
        return "awaiting_approval"

    if task.status is TaskStatus.FAILED:
        ctx = task.context or {}
        if ctx.get("cancel_reason"):
            raise OverrideRefused(
                "task was cancelled by a human — refusing")
        pr_url = await task_has_pr_evidence(store, task)
        if pr_url:
            raise OverrideRefused(
                f"task has a PR ({pr_url}) — this is not the pre-PR shape; "
                "use `nh task restore-approval` then `nh approve --landed`"
            )
        return "failed_pre_pr"

    if task.status is TaskStatus.PENDING:
        cancel_requested = await store.get_cancel_request(task.id)
        if cancel_requested:
            raise OverrideRefused(
                "task has a pending cancellation request "
                f"({cancel_requested!r}) — refusing"
            )
        pr_url = await task_has_pr_evidence(store, task)
        if pr_url:
            raise OverrideRefused(
                f"task is pending but has a PR ({pr_url}) — this is not the "
                "never-ran shape; use `nh task restore-approval` then "
                "`nh approve --landed`"
            )
        return "pending_never_ran"

    raise OverrideRefused(
        f"task is {task.status.value!r}, not awaiting_approval, a pre-PR "
        "failed task, or a never-dispatched pending task"
    )


async def _base_hint(task: Task) -> str:
    """Best-effort, NON-BINDING text appended to a refusal message ONLY —
    this is never used to fill in a ``base`` value automatically.

    An earlier version of this fix used ``resolve_default_branch`` (and a
    profile's ``default_branch``) as an authoritative default for the
    ``pending_never_ran`` shape. Independent review found that, in practice,
    every onboarded profile on this product has an empty ``default_branch``
    and nothing ever calls ``git remote set-head``, so
    ``resolve_default_branch`` always fell through to the checkout's own
    *current* branch — the parked branch of whatever repo happened to be
    checked out, not the project's real default. That made the base
    unreliable exactly where the ancestor check depends on it being
    trustworthy (point 5 below), so it is never used as a value here — only
    as advisory text a human can sanity-check before typing ``--base``.
    """
    try:
        guess = (await resolve_default_branch(task.repo_path) or "").strip()
    except Exception:  # noqa: BLE001 — advisory text, never load-bearing
        guess = ""
    if not guess:
        return " — pass --base <branch> (the tool will not guess one)"
    return (
        " — pass --base <branch> (hint: this repo's checkout currently "
        f"reports {guess!r}, which may or may not be the project's real "
        "default — confirm before using it)"
    )


async def approve_landed_override(
    store: Any, task: Task, sha: str, justification: str, *,
    is_ancestor: IsAncestor | None = None,
    residue_probe: ResidueProbe | None = None,
    base: str | None = None,
    human: str = "human",
) -> dict[str, Any]:
    """Validate and record a human landed-override, or raise ``OverrideRefused``.

    Every check below runs — and can refuse — BEFORE any write. Order:

    1. ``task.status`` must resolve to an eligible shape (``_resolve_shape``):
       ``AWAITING_APPROVAL``, a ``FAILED`` task that was neither
       human-cancelled nor ever opened a PR (the pre-PR shape), or a
       ``PENDING`` task that never opened a PR (the never-ran shape).
    2. ``justification`` must not be blank.
    3. ``sha`` must not be blank.
    4. ``task.repo_path`` must be recorded. ``task.context["base_branch"]``
       must be recorded too — EXCEPT for the ``pending_never_ran`` shape,
       where a missing base may instead be supplied explicitly via the
       caller's ``base=`` argument (CLI: ``--base``; API: the request body's
       ``base`` field): a task that never dispatched never got the chance to
       record one. This is a required human ASSERTION, never a guess — if
       neither a recorded base nor an explicit ``base=`` is present, the call
       refuses with a non-binding hint (``_base_hint``) rather than filling
       one in. The other two shapes keep the strict rule unconditionally (the
       same rule ``complete_if_content_landed`` documents: a wrong guessed
       base is worse than refusing) — they accept no ``base=`` override at
       all, recorded or nothing.
    5. ``sha`` must be an ancestor of ``base`` (local git only,
       ``commit_is_ancestor`` — ``git merge-base --is-ancestor``, fail-closed
       on any git error: a probe failure is a refusal, never a pass). This is
       what discharges "refused for tasks whose content is NOT landed" for
       every shape: a branch that never reached the base branch has no
       ancestor sha to name. For ``pending_never_ran`` this check is only as
       trustworthy as ``base`` itself, which is exactly why point 4 never
       lets ``base`` be guessed: it is always either a value a real prior
       dispatch recorded, or a branch the human typed explicitly for this
       call — never a checkout's incidental current branch.

    Only once every check above has passed does this write anything: it
    records ``landed_override_sha`` (deliberately a DIFFERENT context key
    from ``landed_sha`` — a human assertion must never later be re-read by
    the watcher as a machine-verified landing) and completes the task with
    an ``approved_landed_override`` event carrying the sha, the justification
    verbatim, the resolved shape, an ``equivalence`` verdict, and the
    containment residue at that sha — best-effort: a residue-probe failure
    records ``residue: None`` and a note, and never turns the override into a
    refusal or a pass; the whole point of this class of task is that
    automated containment already refused (or, for the pre-PR shape, never
    ran at all).

    Returns ``{"sha": ..., "residue": ..., "text": ..., "shape": ...,
    "prior_status": ...}`` for the caller's own message/response formatting.
    """
    is_ancestor = is_ancestor or commit_is_ancestor
    residue_probe = residue_probe or containment_residue

    shape = await _resolve_shape(store, task)
    prior_status = task.status.value

    justification = (justification or "").strip()
    if not justification:
        raise OverrideRefused("justification must not be empty")

    sha = (sha or "").strip()
    if not sha:
        raise OverrideRefused("sha must not be empty")

    base_override = (base or "").strip()

    ctx = task.context or {}
    base = ctx.get("base_branch")
    base_source = "recorded"
    if not base and shape == "pending_never_ran" and base_override:
        base, base_source = base_override, "human_asserted"
    if not base:
        hint = await _base_hint(task) if shape == "pending_never_ran" else ""
        raise OverrideRefused(
            "task has no recorded base_branch — refusing" + hint)
    if not task.repo_path:
        raise OverrideRefused("task has no recorded repo_path — refusing")

    if not await is_ancestor(task.repo_path, sha, base):
        raise OverrideRefused(
            f"{sha} is not an ancestor of {base} — refusing")

    branch = ctx.get("pr_branch") or ctx.get("pr_draft_branch") or ""
    if not branch and shape == "failed_pre_pr":
        try:
            row = await store.latest_attempt_branch(task.id)
            branch = row.get("branch") or row.get("commit_sha") or ""
        except Exception:  # noqa: BLE001 — metadata read, never a crash
            branch = ""

    residue: list[str] | None
    residue_note: str | None = None
    try:
        residue = await residue_probe(task.repo_path, sha, branch)
    except Exception:  # noqa: BLE001 — best-effort audit data, never a gate
        residue = None
    if residue is None:
        residue_note = "could not be computed"

    equivalence = (
        "content_equivalent" if residue == [] else
        "asserted" if residue is None else
        "asserted_with_residue"
    )

    ts_iso = _now_iso()
    context_patch = {"landed_override_sha": sha, "approved_at": ts_iso}
    if base_source == "human_asserted":
        context_patch["base_branch"] = base
    await store.merge_context(task.id, context_patch)

    sha12 = sha[:12]
    residue_text = ", ".join(residue) if residue else (
        residue_note or "none (fully contained)")
    text = (
        "HUMAN OVERRIDE of automated containment (not a containment pass): "
        f"a human asserts this task's content landed at {sha12} on {base}. "
        f"Automated containment refused; residue at that commit: {residue_text}. "
        f"Justification: {justification}"
    )
    if shape == "failed_pre_pr":
        text += " prior status: failed (no PR was ever opened)."
    elif shape == "pending_never_ran":
        text += " prior status: pending (no attempt ever ran)."
    event = {
        "source": human,
        "kind": LANDED_OVERRIDE_KIND,
        "sha": sha,
        "justification": justification,
        "residue": residue,
        "base": base,
        "base_source": base_source,
        "branch": branch,
        # unix float, same clock every other emitter uses for task_events.ts
        # (a REAL column) — context["approved_at"] above stays ISO for the
        # drawer; only this event-level clock must match the column's type.
        "ts": time.time(),
        "text": text,
        "shape": shape,
        "equivalence": equivalence,
        "prior_status": prior_status,
    }
    if residue_note is not None:
        event["residue_note"] = residue_note

    moved = await store.set_status(
        task, TaskStatus.DONE, validate=False, event=event)
    if moved is None:
        raise OverrideRefused(
            "the store refused the transition — nothing was recorded")
    await close_task_prs_on_completion(
        store, task, completion_path=LANDED_OVERRIDE_KIND)

    return {
        "sha": sha, "residue": residue, "text": text,
        "shape": shape, "prior_status": prior_status,
    }
