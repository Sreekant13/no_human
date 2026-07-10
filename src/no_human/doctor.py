"""Liveness diagnostics: which guarded mechanisms have actually ever fired.

The system's worst historical bugs were not crashes but silences — TESTING
never ran for the system's entire life, the supervisor's guidance was dropped
on the floor, the wake watcher persisted nothing, distillation has never
fired once. A dead subsystem produces no error; it produces an absence. This
module makes the absences enumerable: every mechanism that should leave
evidence in ``task_events`` is listed with its lifetime firing count, and a
set of contradiction rules encodes the known silent-death patterns (evidence
of the *surrounding* activity without evidence of the mechanism itself).

Read-only by design; ``nh doctor`` renders the result.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .core.db import Store

# name → (evidence kinds, hint shown when the count is zero). The hint says
# whether zero is plausible or alarming — doctor reports, the human decides.
MECHANISMS: list[tuple[str, tuple[str, ...], str]] = [
    ("planning", ("planning",), "zero = planning disabled or no task got past intake"),
    ("moa_fanout", ("planning_moa",),
     "zero is normal when tasks stay below the MoA complexity gate"),
    ("supervisor", ("supervisor", "supervisor_decision"),
     "fires every N coder tool calls — zero alongside coder activity is a dead hook"),
    ("review_gate", ("review",),
     "zero while attempts complete = the gate is not being consulted (the M0 root cause)"),
    ("tests", ("tests",),
     "zero across attempts = TESTING is dead (it was, for the system's entire life)"),
    ("tamper_guard", ("tamper",), "zero = no diff ever tamper-checked"),
    ("context_distill", ("context_distill",),
     "has never fired to date — plausible with quota headroom, worth knowing"),
    ("lifetime_budget", ("lifetime_budget",),
     "zero = no task ever hit its lifetime caps (good), or the gate is dead"),
    ("stuck_detection", ("stuck",), "zero = no attempt ever looped (or detector dead)"),
    ("pr_open", ("pr_open",), "zero = no task has ever reached a PR"),
    ("pr_open_retry", ("pr_open_retry",), "zero is good — no transient forge failures"),
    ("advisory_degradations", ("advisory",),
     "zero is good — no subsystem silently degraded mid-run"),
    ("citation_rule", ("review_citation_demoted",),
     "zero is good — no hallucinated citation tried to block the gate"),
    ("repro_gate", ("repro_gate",),
     "zero with attempts reaching review = the gate is off or dead; "
     "high waived-share means coders aren't writing manifests"),
    ("pr_watch_ladder",
     ("merged", "pr_closed", "pr_feedback", "pr_feedback_skipped", "pr_ci_red",
      "escalated_ci", "escalated_revisions", "escalated_timeout", "resumed"),
     "zero = the watcher never had to act (fine if pr_watch_heartbeat is alive)"),
    ("pr_watch_heartbeat", ("wake_tick",),
     "zero while tasks sit parked = the watcher is silent or dead "
     "(it was, until 2026-07-10 — events before then were never persisted)"),
    ("ci_gate_integration", ("ci_gate_trigger", "ci_gate_pass", "ci_gate_fail"),
     "zero = the post-PR CI_GATE gate never ran — fine while ci_gate.enabled "
     "is off, dead if a governed PR sat green without a run"),
]

# A parked task whose newest watcher evidence is older than this is unshepherded.
WATCHER_STALE_SECONDS = 2 * 3600.0

# Statuses that assert something happened → the event kinds that must exist to
# back the claim. A status without its evidence is a signal that lies.
REQUIRED_EVIDENCE: dict[str, tuple[str, ...]] = {
    "awaiting_approval": ("pr_open",),
    "done": ("pr_open",),
}


@dataclass
class Diagnosis:
    mechanisms: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.contradictions and not self.evidence_gaps


async def _kind_stats(store: Store) -> dict[str, tuple[int, float]]:
    """kind → (count, last_ts) over all persisted task events."""
    cur = await store.db.execute(
        "SELECT json_extract(data, '$.kind') AS kind, COUNT(*), MAX(ts) "
        "FROM task_events GROUP BY kind"
    )
    return {row[0]: (row[1], row[2] or 0.0) for row in await cur.fetchall() if row[0]}


async def diagnose(store: Store) -> Diagnosis:
    d = Diagnosis()
    stats = await _kind_stats(store)

    def total(kinds: tuple[str, ...]) -> tuple[int, float]:
        count = sum(stats.get(k, (0, 0.0))[0] for k in kinds)
        last = max((stats.get(k, (0, 0.0))[1] for k in kinds), default=0.0)
        return count, last

    for name, kinds, hint in MECHANISMS:
        count, last = total(kinds)
        d.mechanisms.append(
            {"name": name, "count": count, "last_ts": last,
             "hint": hint if count == 0 else ""}
        )

    counts = {m["name"]: m["count"] for m in d.mechanisms}

    # Contradiction rules — each one is a silent death the project has really
    # had. Evidence of surrounding activity without evidence of the mechanism.
    coder_activity, _ = total(("tool_use",))
    if counts["tests"] == 0 and counts["review_gate"] > 0:
        d.contradictions.append(
            f"TESTS NEVER RAN while the review gate fired {counts['review_gate']}× "
            "— the exact failure that went unnoticed for the system's entire life."
        )
    if counts["supervisor"] == 0 and coder_activity > 50:
        d.contradictions.append(
            f"SUPERVISOR SILENT across {coder_activity} coder tool calls — the "
            "every-N-calls hook is not firing."
        )
    if counts["review_gate"] == 0 and counts["pr_open"] > 0:
        d.contradictions.append(
            f"UNREVIEWED PRs: {counts['pr_open']} pr_open event(s) with zero "
            "review events — the gate was bypassed."
        )
    cur = await store.db.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'awaiting_approval'"
    )
    parked = (await cur.fetchone())[0]
    if parked > 0:
        # A healthy watcher leaves either actions or heartbeats. Neither, or
        # both stale, means nothing is shepherding the parked PRs right now.
        _, last_action = total(
            next(k for n, k, _ in MECHANISMS if n == "pr_watch_ladder"))
        _, last_beat = total(("wake_tick",))
        newest = max(last_action, last_beat)
        if newest == 0.0:
            d.contradictions.append(
                f"WATCHER SILENT: {parked} task(s) parked at awaiting_approval "
                "with zero persisted watcher events — nothing is shepherding "
                "their PRs."
            )
        elif time.time() - newest > WATCHER_STALE_SECONDS:
            age_h = (time.time() - newest) / 3600
            d.contradictions.append(
                f"WATCHER STALE: {parked} task(s) parked but the newest watcher "
                f"evidence is {age_h:.1f}h old (heartbeat is hourly)."
            )

    # A CI_GATE validation that STARTED must have finished green before the
    # task may claim done — a done task whose integration run never passed is
    # a verdict without its evidence (the M6 contradiction).
    cur = await store.db.execute(
        """SELECT t.id FROM tasks t WHERE t.status = 'done' AND EXISTS (
              SELECT 1 FROM task_events e WHERE e.task_id = t.id
              AND json_extract(e.data, '$.kind') = 'ci_gate_trigger')
           AND NOT EXISTS (
              SELECT 1 FROM task_events e WHERE e.task_id = t.id
              AND json_extract(e.data, '$.kind') = 'ci_gate_pass')""")
    for (task_id,) in await cur.fetchall():
        d.contradictions.append(
            f"CI_GATE UNPROVEN: task {task_id[:8]} is 'done' but its CI_GATE "
            "integration run was triggered and never passed."
        )

    # Per-status required evidence: a task claiming a status must have the
    # events that back the claim.
    for status, kinds in REQUIRED_EVIDENCE.items():
        placeholders = ",".join("?" for _ in kinds)
        cur = await store.db.execute(
            f"""SELECT t.id FROM tasks t WHERE t.status = ? AND NOT EXISTS (
                  SELECT 1 FROM task_events e WHERE e.task_id = t.id
                  AND json_extract(e.data, '$.kind') IN ({placeholders}))""",
            (status, *kinds),
        )
        for (task_id,) in await cur.fetchall():
            d.evidence_gaps.append(
                f"task {task_id[:8]} is '{status}' with no {'/'.join(kinds)} "
                "event — the status is not backed by evidence."
            )
    cur = await store.db.execute(
        "SELECT id, blocker FROM tasks WHERE status = 'escalated'"
    )
    for task_id, blocker in await cur.fetchall():
        data = json.loads(blocker) if blocker else {}
        if not data.get("question") and not data.get("root_cause_hypothesis"):
            d.evidence_gaps.append(
                f"task {task_id[:8]} is 'escalated' with an empty blocker — "
                "a human was summoned with nothing to decide on."
            )
    return d
