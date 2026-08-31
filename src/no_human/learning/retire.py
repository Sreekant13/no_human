"""Memory lifecycle C: retirement + flood control.

Part C of the memory-lifecycle design (research report 2026-08-12). The
confirm queue floods because unconfirmed proposals never expire (487-488
pending vs 53 active, measured against a copy of the operator's database) and
because the per-success templated skill proposal (`learning.queue._build`'s
AWAITING_APPROVAL/DONE branch) produces most of the NULL-origin pending rows
with no evidence beyond "a task finished".

This module is the retirement half:
- `sweep_unconfirmed` — the 45-day auto-archive of stale UNCONFIRMED
  proposals (reversible: it archives, never deletes).
- `retirement_candidates` — a SUGGEST-only report of stale ACTIVE
  (confirmed) rows. Nothing here archives a confirmed row; that always
  requires a human's explicit `nh learnings --retire <id>` /
  `POST /api/learnings/{id}/retire` — UNLESS it is auto-activated (below).
- `sweep_auto_activated` (D3, 2026-08-31 operator directive) — the ONE
  exception to the "always requires a human" line above: an
  auto-activated row (`confirmed_by = 'auto'`) retires itself after 90
  days unused, the same way it activated itself. An operator-pinned or
  manually-added row can never match its query (`activated_at IS NOT
  NULL` is the whole exemption), so `curator.py`'s pinned-exempt rule
  survives unchanged.
- `find_superseded` / `near_duplicate_key` — the supersede-on-confirm
  mechanism (AC3): when a human confirms a proposal that duplicates an
  existing active rule, the old row is archived with a `superseded_by`
  pointer rather than left as a second live copy.
- `is_templated_success_proposal` — the predicate that names the flood
  source, used by the one-time triage (`nh learnings --triage-templated`)
  and nowhere else; it does not gate anything by itself.

The flood source itself is killed in `learning/queue.py` (gated behind
`propose_on_success`, default off), not here — this module only identifies
its output for the triage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..core.db import SOURCE_PROPOSED, Store
from .curator import dedupe_key as near_duplicate_key
from .queue import TYPE_SKILL

log = logging.getLogger("no_human.learning.retire")

# The two literals `learning.queue._build` writes for a per-success proposal —
# duplicated here rather than imported, because importing them would couple
# this predicate to the exact private strings instead of documenting them as
# the contract they are. If `_build` ever changes either literal, this
# predicate (and the triage/tests that depend on it) must be updated with it.
_TEMPLATE_TITLE_PREFIX = "Approach that worked: "
_TEMPLATE_CONTENT_MARKER = (
    "Consider capturing the successful approach as a reusable skill."
)

# Default lifecycle windows (also the config.py DEFAULT_CONFIG["learning"]
# values — kept here too so a caller that skips config still gets a sane
# default rather than a required kwarg).
DEFAULT_ARCHIVE_UNCONFIRMED_DAYS = 45
DEFAULT_RETIRE_SUGGEST_DAYS = 90


@dataclass
class SweepReport:
    """What one `sweep_unconfirmed` call did (or, on `dry_run`, would do)."""

    archived_ids: list[str]
    scanned: int
    dry_run: bool
    reason: str


# D3 (2026-08-31 operator directive): auto-activated rows retire
# automatically after this many days unused — the SAME window
# `retire_suggest_days`/`DEFAULT_RETIRE_SUGGEST_DAYS` already uses for the
# human SUGGEST report, deliberately: it is the one retirement window this
# product has already chosen, not a second number to keep in sync with it.
DEFAULT_AUTO_RETIRE_DAYS = DEFAULT_RETIRE_SUGGEST_DAYS


async def sweep_unconfirmed(
    store: Store, *, days: int = DEFAULT_ARCHIVE_UNCONFIRMED_DAYS,
    limit: int = 500, dry_run: bool = False,
) -> SweepReport:
    """The 45-day auto-archive sweep (AC1). Thin, documented wrapper around
    `Store.archive_unconfirmed_older_than` — see that method for the exact
    WHERE-clause reasoning (why it cannot reuse `archive_memory`, why the
    `created_at` comparison must run in SQL, why `confirmed = 0` is a literal
    equality and not an `IS NULL OR = 0`).

    Logs a warning when `limit` truncates the result — a sweep that silently
    stops at its cap looks identical to one that covered everything eligible,
    and that is exactly the "unconfirmed proposals never expire" bug this
    module exists to fix.
    """
    reason = f"unconfirmed for {days}d — automatic lifecycle sweep"
    ids = await store.archive_unconfirmed_older_than(
        days=days, limit=limit, reason=reason, dry_run=dry_run)
    if len(ids) >= limit:
        log.warning(
            "unconfirmed-proposal sweep hit its limit (%d) — more may still "
            "be eligible; re-run to continue", limit,
        )
    return SweepReport(
        archived_ids=ids, scanned=len(ids), dry_run=dry_run, reason=reason)


async def sweep_auto_activated(
    store: Store, *, days: int = DEFAULT_AUTO_RETIRE_DAYS,
    limit: int = 500, dry_run: bool = False,
) -> SweepReport:
    """D3 (2026-08-31 operator directive): the 90-day AUTOMATIC retirement
    sweep for AUTO-ACTIVATED rows only. Thin, documented wrapper around
    `Store.archive_stale_auto_activated` — see that method for the exact
    WHERE-clause reasoning (why `confirmed_by = 'auto'` is the whole
    exemption for a pinned/manually-added row, why the unused window is
    measured off `activated_at`, not `created_at`).

    UNLIKE `retirement_candidates` (which stays SUGGEST-only for every
    confirmed row, human-added or auto-activated), this one ARCHIVES —
    that asymmetry is the D3 design, not an oversight: an operator-pinned or
    manually-added rule keeps needing a human's explicit
    `nh learnings --retire`/`POST /api/learnings/{id}/retire`; a rule
    nobody ever reviewed can retire itself the same way it activated
    itself. Writes a `learning_events` 'auto_retire' row per id archived
    (`dry_run` writes neither the archive nor the audit row) — the D3 audit
    trail, same as `LearningQueue.auto_activate`'s.
    """
    reason = f"unused for {days}d — automatic auto-activation retirement"
    ids = await store.archive_stale_auto_activated(
        days=days, limit=limit, reason=reason, dry_run=dry_run)
    if not dry_run:
        for mem_id in ids:
            # Best-effort, per id: the archive for THIS id already committed
            # (`archive_stale_auto_activated` is one statement over the whole
            # batch), so a failed audit write here must never raise out of
            # the loop and abandon auditing every id still to come — the
            # same "a completed transition must not read as a failure"
            # guarantee `LearningQueue._audit` gives every queue-level
            # transition.
            try:
                await store.record_learning_event(
                    mem_id, "auto_retire", detail={"days": days})
            except Exception:  # noqa: BLE001
                log.warning("learning_events write failed for auto_retire %s",
                            mem_id, exc_info=True)
    if len(ids) >= limit:
        log.warning(
            "auto-activated retirement sweep hit its limit (%d) — more may "
            "still be eligible; re-run to continue", limit,
        )
    return SweepReport(
        archived_ids=ids, scanned=len(ids), dry_run=dry_run, reason=reason)


async def retirement_candidates(
    store: Store, *, days: int = DEFAULT_RETIRE_SUGGEST_DAYS,
    project: str | None = None, scope: str | None = None,
) -> list[dict[str, Any]]:
    """SUGGEST-only report of stale ACTIVE (confirmed) rows (AC2). READ-ONLY —
    delegates to `Store.stale_memories`, which never archives or deletes a
    confirmed row, and neither does this. A human decides with
    `nh learnings --retire <id>` / `POST /api/learnings/{id}/retire`; nothing
    here, and nothing that calls this, is permitted to auto-archive a
    confirmed row.

    Each row is annotated with:
    - `stale_days`: how many days old the cutoff was evaluated at (the
      caller's own `days`, echoed back so a mixed-window caller can label
      correctly).
    - `never_used`: True when `last_used_at` is NULL. "Never triggered" and
      "unused for N days" are different facts (a NULL row may predate the
      column) and must not be collapsed into one label.
    """
    rows = await store.stale_memories(days=days, project=project, scope=scope)
    out = []
    for r in rows:
        row = dict(r)
        row["stale_days"] = days
        row["never_used"] = not row.get("last_used_at")
        out.append(row)
    return out


async def find_superseded(
    store: Store, confirmed_row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Other ACTIVE rows sharing *confirmed_row*'s near-duplicate key,
    restricted to the SAME scope. A global rule must never silently
    supersede — or be superseded by — a repo-specific one, so scope match is
    mandatory, not a tiebreaker:

    - both rows carry the same `project_scope`, or
    - both are scope-less (`project_scope IS NULL`) AND carry the same
      `project` (legacy path-keyed rows, or two genuinely global rows).

    Excludes *confirmed_row* itself and any row already archived or already
    superseded (that row is not "active" any more — `list_memories`'s default
    already excludes archived, kept explicit here for readability).
    """
    key = near_duplicate_key(confirmed_row)
    my_scope = confirmed_row.get("project_scope")
    my_project = confirmed_row.get("project")
    # D3 (2026-08-31 operator directive): `include_paused=True` — a paused
    # row is a candidate too. Without this, a confirm's near-duplicate scan
    # would be blind to a rule the operator just paused, the same dedupe gap
    # `LearningQueue._auto_activation_screen` closes on its own read of this
    # table: the row still carries the same lesson, and excluding it here
    # would let it and a freshly confirmed duplicate coexist as two active
    # copies the moment the pause is lifted.
    candidates = await store.list_memories(confirmed=True, include_paused=True)
    out = []
    for row in candidates:
        if row["id"] == confirmed_row.get("id"):
            continue
        if row.get("archived") or row.get("superseded_by"):
            continue
        if near_duplicate_key(row) != key:
            continue
        row_scope = row.get("project_scope")
        row_project = row.get("project")
        if my_scope or row_scope:
            if my_scope != row_scope:
                continue
        elif my_project != row_project:
            continue
        out.append(row)
    return out


def is_templated_success_proposal(mem: dict[str, Any]) -> bool:
    """True only for a row the flood source (the per-success templated skill
    proposal in `learning.queue._build`) produced — used exclusively by the
    one-time triage (`nh learnings --triage-templated`) to find and archive
    them in bulk. ALL of:

    - `confirmed == 0` — an already-confirmed row is never a triage target.
    - `source == SOURCE_PROPOSED` — the queue-visibility contract.
    - `type == TYPE_SKILL`.
    - `origin` NULL/empty — `_build`'s AWAITING_APPROVAL/DONE branch never
      set `origin`; any of the five named origins (review, supervisor,
      history, reply, curator) is evidence-bearing and must never match.
    - title starts with the exact `_build` literal, OR content contains the
      exact `_build` literal — either is sufficient, since a row surviving
      truncation (`title[:120]`/`content[:1000]`) may only carry one intact.
    - `evidence` absent, or its `kind` is `"task_outcome"` — any other
      evidence kind (a review finding, a correction cluster) means a
      different producer built this row and it must never match.
    """
    if mem.get("confirmed"):
        return False
    if mem.get("source") != SOURCE_PROPOSED:
        return False
    if mem.get("type") != TYPE_SKILL:
        return False
    if mem.get("origin"):
        return False
    title = str(mem.get("title") or "")
    content = str(mem.get("content") or "")
    if not (title.startswith(_TEMPLATE_TITLE_PREFIX)
            or _TEMPLATE_CONTENT_MARKER in content):
        return False
    raw_evidence = mem.get("evidence")
    if raw_evidence:
        try:
            evidence = (json.loads(raw_evidence)
                        if isinstance(raw_evidence, str) else raw_evidence)
        except (ValueError, TypeError):
            evidence = None
        if not isinstance(evidence, dict):
            return False
        if evidence.get("kind") not in (None, "task_outcome"):
            return False
    return True
