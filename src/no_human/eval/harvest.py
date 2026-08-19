"""Failure-harvested bench-spec candidates (gap-close W6).

The north-star corpus is hand-picked (~53 specs) while the product generates
its own richest test material every day: tasks that escalated, parked, or
failed. Each of those is a replayable scenario for exactly the failure modes
the bench measures — and today it evaporates when the board moves on. This
module turns every terminal non-success into a spec CANDIDATE the operator
curates.

Curation is the design, not a limitation (the Tessl loop-engineering pass,
field notes 2026-08-18, auto-harvests evals from production mistakes — but
OUR corpus feeds a published trust number, so nothing enters it un-reviewed):

- Candidates are written OUTSIDE the corpus (default ``~/.no_human/harvest``);
  the spec loader's non-recursive glob would not see a subdirectory either,
  but distance beats accident.
- Every candidate carries ``runnable: false`` with a skip_reason naming what
  the operator must supply (a repo pin, a subset verdict, an
  expect_escalation judgment) — so even a candidate dropped into the corpus
  dir verbatim cannot run, let alone score.
- ``expect_escalation`` is left FALSE with the blocker's own story in the
  ``harvest:`` provenance block: whether the escalation was the RIGHT outcome
  is precisely the judgment being requested (the 2026-08-18 event forensics
  found three "failures" that were honest escalations against infidelitous
  specs — the corpus, not the agent, needed fixing).
- Existing candidate files are never overwritten: a curated-in-place edit
  survives re-harvest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..core.task import Task, TaskStatus

#: Terminal states worth a candidate. DONE/AWAITING_APPROVAL are successes;
#: everything here stopped short of the deliverable.
HARVEST_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.ESCALATED,
    TaskStatus.AWAITING_INPUT,
    TaskStatus.BLOCKED,
    TaskStatus.FAILED,
})

_DEFAULT_OUT = Path.home() / ".no_human" / "harvest"


def candidate_from_task(task: Task) -> dict[str, Any] | None:
    """One spec-candidate dict for a harvest-worthy terminal task, else None.

    The dict is the BenchTask YAML shape (`eval/northstar_tasks/*.yaml`) plus
    a ``harvest:`` provenance block the loader ignores. The request is the
    task's own title+description VERBATIM — the same no-cheat rule as `bench
    build` (initial ask only, nothing the run later learned).
    """
    if task.status not in HARVEST_STATUSES:
        return None
    if not (task.title or "").strip():
        return None
    request = (task.title or "").strip()
    if (task.description or "").strip():
        request += "\n\n" + task.description.strip()
    blocker = task.blocker or {}
    return {
        "id": f"hv-{task.id[:8]}",
        "title": (task.title or "")[:120],
        "request": request,
        "source": {"kind": "harvest", "task_id": task.id},
        "repo": {"path": task.repo_path or "", "pin": "", "branch": ""},
        "original": {"tokens": {}, "wall_clock_s": 0.0, "user_messages": 0,
                     "corrections": 0},
        "acceptance_criteria": list(task.acceptance_criteria or []),
        "holdout": "",
        "subset": "candidate",
        "runnable": False,
        "skip_reason": (
            "harvested candidate — before this can run the operator must: "
            "pin repo.pin to the pre-task commit, decide subset "
            "(core/full/canary), and judge expect_escalation from the "
            "harvest block below"
        ),
        "expect_escalation": False,
        "escalation_reason": "",
        "harvest": {
            "outcome": task.status.value,
            "blocker_category": blocker.get("category", ""),
            "blocker_question": (blocker.get("question") or "")[:400],
            "root_cause_hypothesis":
                (blocker.get("root_cause_hypothesis") or "")[:400],
            "attempt_log": list(
                (task.context or {}).get("attempt_log") or [])[-3:],
            "harvested_at": datetime.now(timezone.utc).isoformat(),
        },
    }


async def harvest(store: Any, *, out_dir: Path | None = None) -> list[Path]:
    """Write one candidate YAML per harvest-worthy task; return written paths.

    Idempotent by filename: an existing candidate file is the operator's
    (possibly curated) copy and is left untouched.
    """
    out = Path(out_dir) if out_dir else _DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for status in sorted(HARVEST_STATUSES, key=lambda s: s.value):
        for task in await store.list_tasks(status=status):
            cand = candidate_from_task(task)
            if cand is None:
                continue
            path = out / f"{cand['id']}.yaml"
            if path.exists():
                continue
            path.write_text(
                yaml.safe_dump(cand, sort_keys=False, allow_unicode=True),
                encoding="utf-8")
            written.append(path)
    return written
