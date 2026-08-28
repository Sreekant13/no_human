"""The failure-harvest loop: escalations, reviewer FAILs and tamper trips
become learning proposals, mined the SAME way B2 (``learning/corrections.py``)
mines supervisor corrections.

Three more signals were persisted and never read again: an ``escalated``
event (the agent gave up and asked a human), a reviewer FAIL round's blocking
findings (the independent gate said no), and a tripped tamper guard (the
agent tried to route around review). Each already fires a ``task_events`` row
or an ``attempts`` column; none of the three ever became a durable lesson.
This module loads all three into the same :class:`~.corrections.CorrectionRecord`
shape B2 uses, tagged with which signal produced them, and hands them to
:func:`~.corrections.cluster_corrections` — the identical ``(project, source,
gist)`` clustering, the identical ``>=2`` recurrence rule before a one-off
becomes a proposal.

Tessl's loop proposes PRs. This one proposes REVIEWABLE ENTRIES. The output
is a ``memories`` row with ``source="proposed"``, ``confirmed=0`` —
unreachable by every agent, because ``confirmed_rules`` is built from
``list_memories(confirmed=True)``. That is not a missing feature: an
escalation, a reviewer FAIL and a tamper trip are the three signals most
entangled with *the agent's own judgment being wrong*, and a loop that turned
them into applied changes would be the agent writing its own standing
instructions from its own mistakes. Curation is the design (see
``eval/harvest.py``'s docstring for the same call on the bench side). Do not
"upgrade" this to auto-apply or auto-PR.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..review.reviewer import findings_from_checklist
from .corrections import CorrectionRecord
from .queue import NON_LEARNABLE_CATEGORIES, NoteFn, _is_infra_finding

__all__ = [
    "SOURCE_ESCALATION",
    "SOURCE_REVIEW_FAIL",
    "SOURCE_TAMPER",
    "FAILURE_SOURCES",
    "load_failure_records",
]

SOURCE_ESCALATION = "escalation"
SOURCE_REVIEW_FAIL = "review_fail"
SOURCE_TAMPER = "tamper"
FAILURE_SOURCES = (SOURCE_ESCALATION, SOURCE_REVIEW_FAIL, SOURCE_TAMPER)

# Chars of cited evidence kept per reviewer finding, matching
# `learning.queue._MAX_EVIDENCE` — the same bound, kept as a separate
# constant here rather than importing a second private name for one integer.
_MAX_EVIDENCE = 200


def _finding_dict(item: Any) -> dict[str, Any]:
    """A blocking `ChecklistItem` (a dataclass, no `.get()`) as the plain dict
    `learning.queue._is_infra_finding` expects — the SAME shape that
    function's docstring and `_finding_lines` already assume
    (`label`/`evidence`/`file`/`line`)."""
    return {
        "label": item.label, "evidence": item.evidence,
        "file": item.file, "line": item.line,
    }


def _parse_started_at(raw: Any) -> float:
    """`attempts.started_at` is a TEXT ISO datetime (``datetime('now')``), NOT
    an epoch float like `task_events.ts` — the established conversion is
    `core/db.py`'s own `datetime.fromisoformat(r["started_at"])`. A malformed
    or missing value degrades to 0.0 rather than raising: a display path
    ordering issue is not worth losing the lesson over."""
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return 0.0


async def load_failure_records(
    store: Any, *, project: str | None = None, limit: int = 5000,
    note: NoteFn | None = None,
) -> list[CorrectionRecord]:
    """Every persisted escalation, reviewer FAIL finding and tamper trip, as
    source-tagged :class:`~.corrections.CorrectionRecord`\\ s, unclustered.

    Mirrors ``Store.list_supervisor_corrections`` → ``CorrectionRecord``
    construction in ``LearningQueue.harvest_supervisor_corrections`` exactly,
    for the three new sources instead of the one. Callers pass the result to
    ``cluster_corrections`` themselves — this function only loads and tags,
    the same division B2 already has between the store read and the
    clustering.

    Two kinds of row are dropped before they ever become a record, both
    counted via *note* rather than silently skipped:

    * an escalation whose blocker ``category`` is in ``NON_LEARNABLE_CATEGORIES``
      — an environment fact (budget, quota, missing access, ...), not a
      reusable lesson about the repository;
    * a reviewer FAIL finding that matches the reviewer's own fail-closed
      "reviewer crashed" sentinel (``_is_infra_finding``) — an SDK outage
      wearing a finding's clothes, not something the reviewer genuinely found.

    A malformed or absent review checklist is skipped, not raised — the same
    tolerance ``findings_from_checklist`` itself already has for a display
    path that must never crash the caller.
    """
    records: list[CorrectionRecord] = []

    # -- escalations ---------------------------------------------------------
    skipped_category = 0
    for r in await store.list_escalations(project=project, limit=limit):
        category = str(r.get("category") or "")
        if category in NON_LEARNABLE_CATEGORIES:
            skipped_category += 1
            continue
        records.append(CorrectionRecord(
            task_id=str(r.get("task_id") or ""),
            project=r.get("project"),
            message=str(r.get("message") or ""),
            ts=float(r.get("ts") or 0.0),
            source=SOURCE_ESCALATION,
        ))
    if skipped_category and note is not None:
        note(f"{skipped_category} escalation(s) skipped: their blocker "
             "category is environmental (NON_LEARNABLE_CATEGORIES), not a "
             "reusable lesson about the repository")

    # -- reviewer FAILs --------------------------------------------------------
    skipped_infra = 0
    for r in await store.list_review_fails(project=project, limit=limit):
        blocking, _advisory = findings_from_checklist(r.get("review_checklist"))
        if not blocking:
            continue
        ts = _parse_started_at(r.get("ts"))
        task_id = str(r.get("task_id") or "")
        proj = r.get("project")
        for item in blocking:
            finding = _finding_dict(item)
            if _is_infra_finding(finding):
                skipped_infra += 1
                continue
            message = f"{item.label} — {item.evidence[:_MAX_EVIDENCE]}"
            records.append(CorrectionRecord(
                task_id=task_id, project=proj, message=message, ts=ts,
                source=SOURCE_REVIEW_FAIL,
            ))
    if skipped_infra and note is not None:
        note(f"{skipped_infra} reviewer FAIL finding(s) skipped: the "
             "reviewer itself crashed (fail-closed sentinel), not a genuine "
             "finding about the change")

    # -- tamper trips ----------------------------------------------------------
    for r in await store.list_tamper_trips(project=project, limit=limit):
        records.append(CorrectionRecord(
            task_id=str(r.get("task_id") or ""),
            project=r.get("project"),
            message=str(r.get("message") or ""),
            ts=float(r.get("ts") or 0.0),
            source=SOURCE_TAMPER,
        ))

    return records
