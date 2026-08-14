"""The human landed-override: an explicit human confirmation that completes
an ``awaiting_approval`` task whose content landed via a path automated
containment cannot verify (a supervising session's squash train that a later
train car's classification-decision edits, or a union-resolved real source
conflict, leaves with no candidate commit whose tree matches the branch
verbatim — see ``vcs/pr_watcher.py``'s ``_contained_at``/``default_branch_shipped``).

Single shared implementation for both the CLI (`nh approve --landed`) and the
API (`POST /api/tasks/{id}/approve-landed`) — the exact validation and audit
event, so the two surfaces cannot drift.

**No git state is ever touched.** This module imports only read-only probes
(``pr_watcher.commit_is_ancestor``, ``pr_watcher.containment_residue``); it
never constructs ``vcs.git.GitRepo``, never calls ``vcs.approve_merge.land_task``,
never merges or pushes. The override is a human ASSERTION recorded on the
task, not a merge action — constraint #2 (the agent never merges) is
untouched because there is no merge here to begin with.

This is a HUMAN override of automated containment, not a containment pass:
the audit event's text says so explicitly, and the event's ``kind`` —
``approved_landed_override`` — is deliberately distinct from every kind an
automated path can write (``shipped``, ``approved_landed``, ``human_merged``),
so a reader of the event log can always tell which class of evidence stands
behind a completion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..core.task import Task, TaskStatus
from ..vcs.pr_watcher import commit_is_ancestor, containment_residue

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


async def approve_landed_override(
    store: Any, task: Task, sha: str, justification: str, *,
    is_ancestor: IsAncestor | None = None,
    residue_probe: ResidueProbe | None = None,
    human: str = "human",
) -> dict[str, Any]:
    """Validate and record a human landed-override, or raise ``OverrideRefused``.

    Every check below runs — and can refuse — BEFORE any write. Order:

    1. ``task.status`` must be ``AWAITING_APPROVAL``.
    2. ``justification`` must not be blank.
    3. ``sha`` must not be blank.
    4. ``task.context["base_branch"]`` and ``task.repo_path`` must both be
       recorded — never defaulted to ``"main"`` (the same rule
       ``complete_if_content_landed`` documents: a wrong guessed base is
       worse than refusing).
    5. ``sha`` must be an ancestor of ``base`` (local git only,
       ``commit_is_ancestor`` — ``git merge-base --is-ancestor``, fail-closed
       on any git error: a probe failure is a refusal, never a pass).

    Only once every check above has passed does this write anything: it
    records ``landed_override_sha`` (deliberately a DIFFERENT context key
    from ``landed_sha`` — a human assertion must never later be re-read by
    the watcher as a machine-verified landing) and completes the task with
    an ``approved_landed_override`` event carrying the sha, the justification
    verbatim, and the containment residue at that sha — best-effort: a
    residue-probe failure records ``residue: None`` and a note, and never
    turns the override into a refusal or a pass; the whole point of this
    class of task is that automated containment already refused.

    Returns ``{"sha": ..., "residue": ..., "text": ...}`` for the caller's
    own message/response formatting.
    """
    is_ancestor = is_ancestor or commit_is_ancestor
    residue_probe = residue_probe or containment_residue

    if task.status is not TaskStatus.AWAITING_APPROVAL:
        raise OverrideRefused(
            f"task is {task.status.value!r}, not awaiting_approval")

    justification = (justification or "").strip()
    if not justification:
        raise OverrideRefused("justification must not be empty")

    sha = (sha or "").strip()
    if not sha:
        raise OverrideRefused("sha must not be empty")

    ctx = task.context or {}
    base = ctx.get("base_branch")
    if not base:
        raise OverrideRefused("task has no recorded base_branch — refusing")
    if not task.repo_path:
        raise OverrideRefused("task has no recorded repo_path — refusing")

    if not await is_ancestor(task.repo_path, sha, base):
        raise OverrideRefused(
            f"{sha} is not an ancestor of {base} — refusing")

    branch = ctx.get("pr_branch") or ctx.get("pr_draft_branch") or ""
    residue: list[str] | None
    residue_note: str | None = None
    try:
        residue = await residue_probe(task.repo_path, sha, branch)
    except Exception:  # noqa: BLE001 — best-effort audit data, never a gate
        residue = None
    if residue is None:
        residue_note = "could not be computed"

    ts = _now_iso()
    await store.merge_context(
        task.id, {"landed_override_sha": sha, "approved_at": ts})

    sha12 = sha[:12]
    residue_text = ", ".join(residue) if residue else (
        residue_note or "none (fully contained)")
    text = (
        "HUMAN OVERRIDE of automated containment (not a containment pass): "
        f"a human asserts this task's content landed at {sha12} on {base}. "
        f"Automated containment refused; residue at that commit: {residue_text}. "
        f"Justification: {justification}"
    )
    event = {
        "source": human,
        "kind": LANDED_OVERRIDE_KIND,
        "sha": sha,
        "justification": justification,
        "residue": residue,
        "base": base,
        "branch": branch,
        "ts": ts,
        "text": text,
    }
    if residue_note is not None:
        event["residue_note"] = residue_note

    await store.set_status(task, TaskStatus.DONE, validate=False, event=event)

    return {"sha": sha, "residue": residue, "text": text}
