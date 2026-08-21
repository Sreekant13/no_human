"""Blocker taxonomy and routing (PLAN.md Part 22).

Core principle (22, stated up front): *a blocker is never resolved by lowering
the bar.* When stuck, the agent makes verifiable progress, parks with a wake
condition, or escalates with a precise diagnosis — it never weakens a test,
expands scope, edits acceptance criteria, or fakes "done".

This module is pure data + routing logic (no I/O), so it is trivially testable
and the orchestrator stays the only place that touches the DB / git / SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..core.task import TaskStatus


class BlockerCategory(str, Enum):
    """The Part 22.2 taxonomy. Value is the stable string stored on the task."""

    TRANSIENT_INFRA = "TRANSIENT_INFRA"
    QUOTA = "QUOTA"
    DEPENDENCY_WAIT = "DEPENDENCY_WAIT"
    MISSING_ACCESS = "MISSING_ACCESS"
    AMBIGUITY = "AMBIGUITY"
    SCOPE_EXPLOSION = "SCOPE_EXPLOSION"
    IMPOSSIBLE = "IMPOSSIBLE"
    NOVEL_UNKNOWN = "NOVEL_UNKNOWN"
    STAGNATION = "STAGNATION"
    # The task's lifetime budget (attempts or tokens, resumes included) is
    # spent. Raised by the harness, never the agent.
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

    @classmethod
    def coerce(cls, value: str | "BlockerCategory") -> "BlockerCategory":
        """Map a raw string (e.g. from the agent) to a category, defaulting to
        NOVEL_UNKNOWN so an unrecognized label escalates rather than crashes."""
        if isinstance(value, cls):
            return value
        key = (value or "").strip().upper().replace("/", "_").replace(" ", "_")
        # accept a few aliases the agent might emit
        aliases = {
            "SPEC_GAP": cls.AMBIGUITY,
            "AMBIGUITY_SPEC_GAP": cls.AMBIGUITY,
            "INVALID": cls.IMPOSSIBLE,
            "IMPOSSIBLE_INVALID": cls.IMPOSSIBLE,
            "INFRA": cls.TRANSIENT_INFRA,
            "RATE_LIMIT": cls.TRANSIENT_INFRA,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            return cls.NOVEL_UNKNOWN


@dataclass(frozen=True)
class Route:
    """How a category is handled: the target state, whether to notify now, and
    whether the wake-watcher should poll it (parked) vs. a human must act."""

    target_status: TaskStatus
    notify_now: bool          # 22.6: "needs you now" vs "parked, silent"
    parked: bool              # watcher polls a parked task; escalations wait on a human
    auto_retry: bool = False  # bounded auto-retry before this routing applies


# Part 22.2 taxonomy → routing, with 22.6 severity baked in.
_ROUTING: dict[BlockerCategory, Route] = {
    # Parked, no action needed — silent until resume or timeout-then-escalate.
    BlockerCategory.TRANSIENT_INFRA: Route(
        TaskStatus.BLOCKED, notify_now=False, parked=True, auto_retry=True),
    BlockerCategory.QUOTA: Route(
        TaskStatus.PAUSED_QUOTA, notify_now=False, parked=True),
    BlockerCategory.DEPENDENCY_WAIT: Route(
        TaskStatus.BLOCKED, notify_now=False, parked=True),
    # Needs you now — only a human can move these forward.
    BlockerCategory.MISSING_ACCESS: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    BlockerCategory.AMBIGUITY: Route(
        TaskStatus.AWAITING_INPUT, notify_now=True, parked=False),
    BlockerCategory.SCOPE_EXPLOSION: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    BlockerCategory.IMPOSSIBLE: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    BlockerCategory.NOVEL_UNKNOWN: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    # Spending more is a human's call, never a retry's.
    BlockerCategory.BUDGET_EXHAUSTED: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    # Stagnation is the one blocker a retry provably cannot clear: it is RAISED
    # because two consecutive attempts made no progress (orchestrator.py, review
    # pass rate flat with a recurring specific failure). Parking needs a wake
    # condition and there is none — nothing external will change — so the only
    # honest route is to escalate with the report. This entry was MISSING, and
    # `Blocker.route` is a bare `_ROUTING[category]`, so every stagnation blocker
    # raised KeyError on the one path the stuck detector exists to serve.
    BlockerCategory.STAGNATION: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
}


#: BUDGET_EXHAUSTED under `budget.exhaustion_terminal` (the default). The task
#: ENDS — no question is asked, because the answer is standing policy: "stop;
#: an exhausted budget means the ticket was wrong, refile it inline-complete
#: and smaller". FAILED rather than a parked state so nothing automatic can
#: revive it: the scheduler's `_CLAIMABLE` is (implementing, pending) and the
#: wake watcher only sweeps blocked/paused_quota/awaiting_input/
#: awaiting_approval, so FAILED is the one terminal state both ignore.
#: `notify_now=False` because a "needs you now" ping is exactly the
#: interruption this removes — the Failed lane, the plain card line and
#: `nh doctor` still surface it.
BUDGET_TERMINAL_ROUTE = Route(TaskStatus.FAILED, notify_now=False, parked=False)


def route_for(category: BlockerCategory) -> Route:
    return _ROUTING[category]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BlockerOption:
    """One answer a human can give, optionally carrying the action that makes it
    real.

    ``SCOPE_EXPLOSION`` offered "raise the limit for this task" long before
    anything could raise a limit, so answering that way regenerated the same
    blocker. An option with an ``action`` is applied by ``nh`` or the board when
    the human picks it; an option without one is just a label, submitted as the
    free-text answer exactly as ``nh reply`` has always behaved.

    The agent may never attach an action — that would let it resolve a blocker
    by weakening its own gate. ``parse_blocker`` strips actions at that boundary.
    """

    label: str
    action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "action": self.action}

    @classmethod
    def coerce(cls, raw: Any) -> "BlockerOption":
        """Normalise the three shapes in the wild: an option, a bare string (old
        rows in ``tasks.blocker``, and every agent-raised blocker), or a dict."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            action = raw.get("action")
            return cls(
                label=str(raw.get("label", "")),
                action=action if isinstance(action, dict) and action else None,
            )
        return cls(label=str(raw))


def resume_checkpoint(blocker: dict[str, Any] | None) -> dict[str, str] | None:
    """The [WIP-BLOCKED] commit a resumed task should continue from, or None.

    ``_raise_blocker`` checkpoints the working tree and records the sha here
    before parking the task. Only a human-authorised resume (`nh reply`, or the
    board) copies it onto the task, where the next attempt branches from it
    instead of throwing tens of turns of work away and starting from base.
    """
    if not isinstance(blocker, dict):
        return None
    sha = blocker.get("resume_commit") or ""
    if not sha:
        return None
    return {"sha": sha, "branch": blocker.get("resume_branch") or ""}


def carried_checkpoint(task: Any) -> dict[str, str] | None:
    """The checkpoint a NEW park should carry forward — freshest write wins.

    Two records can name one: ``context.resume_from`` (re-stamped by EVERY
    re-entry — wake, orphan requeue, human) and the retained ``blocker``
    (written by the LAST park, never cleared by a machine resume). They can
    disagree in one direction that matters: a human's send-back / reject
    writes ``resume_from`` with ``sha: None`` — "branch from base, do not
    credit the abandoned partial" — and leaves the blocker's older sha in
    place. A park that then read the blocker revived exactly the checkpoint
    the human cleared, and the next resume stamped it ``by: human``, disarming
    the zero-diff honesty gate over sent-back work. So: a PRESENT
    ``resume_from`` decides — its sha if it has one, a veto if it has none —
    and only an ABSENT one falls back to the blocker. Same rule
    `_park_quota` applies; this is the one home for it.
    """
    ctx = getattr(task, "context", None) or {}
    rf = ctx.get("resume_from") if isinstance(ctx, dict) else None
    if isinstance(rf, dict):
        sha = rf.get("sha") or ""
        if not sha:
            return None
        return {"sha": sha, "branch": rf.get("branch") or ""}
    return resume_checkpoint(getattr(task, "blocker", None))


def resume_provenance(checkpoint: dict[str, str] | None, by: str) -> dict[str, Any]:
    """The COMPLETE ``resume_from`` value for one resume, by one actor.

    Every resume path writes this and nothing else, because the zero-diff
    honesty gate reads ``sha`` and ``by`` TOGETHER — it credits work already
    ahead of base only when the branch point is the checkpoint a HUMAN gated —
    and seven review rounds were spent on the two ways those halves came apart.

    ``resume_from`` is stored with RFC 7396 (``merge_context``), where nested
    dicts MERGE and a ``None`` value DELETES the key. That is the whole trap:

    * A write that omitted ``by`` inherited the PREVIOUS actor's — so a resume
      with no checkpoint of its own was described by whoever resumed last. That
      latched in whichever direction the reader preferred: a stale ``"wake"``
      failed a human's answer as fabrication, a stale ``"human"`` credited a
      timer's re-entry.
    * Writing ``by`` alone fixed that and broke the other half — the sha a
      MACHINE resume had chosen survived and was relabelled ``human``, so
      `nh unblock` on a task the wake watcher had resumed disarmed the gate and
      an attempt that edited nothing was credited with the loop's own abandoned
      [WIP-PARTIAL]. **That direction opens a PR on work no attempt produced**,
      which is the failure this gate exists to prevent; an independent review
      reproduced it end to end through `run_task`.

    So provenance is never a separate write. Every key is stated every time:
    with no checkpoint, ``sha`` and ``branch`` are explicitly ``None``, which
    DELETES any inherited value, and ``by`` can only ever describe a sha the
    same actor chose. A human re-entry that names no checkpoint therefore
    branches from BASE and the gate stays ARMED.

    🔴 Be honest about what that costs. An earlier version of this docstring
    said "or from this run's own handoff… costing one attempt asked to redo
    work". Both halves were false: `_resume_branch_point` takes
    `handoff.wip_sha` only when ``attempt_n > 1``, and a resume starts a FRESH
    bounded loop at attempt 1, so there is no handoff to fall back to on the
    path that matters. The real cost is **redo everything committed since
    base**. That is still the right side to fail on — the alternative is a PR
    opened on work nobody did — but a caller that HAS a checkpoint must pass it
    rather than None, or it pays that price for nothing.
    """
    cp = checkpoint or {}
    return {"sha": cp.get("sha") or None,
            "branch": cp.get("branch") or None,
            "by": by}


#: The cooperative-stop reason a SERVER SHUTDOWN hands a running attempt.
#: Never written to ``tasks.cancel_requested`` — it is signalled in-process
#: (`Orchestrator.request_server_stop`) so a SIGKILL cannot leave it behind
#: to re-fire on the next server's first cheap boundary. `_honor_cancel`
#: routes it to a REQUEUE (checkpoint, close the row, stay IMPLEMENTING)
#: instead of a USER_PAUSED park.
SERVER_STOP_REASON = "__server_stop__"

#: ``resume_from.by`` values the MACHINE writes after an interrupted run — a
#: killed process (``orphan_recovery``, the scheduler's startup sweep), a
#: graceful stop (``server_stop``, `Orchestrator._honor_server_stop`), or a
#: hard kill mid-IMPLEMENTING (``hard_kill_salvage``,
#: `core.worktree.salvage_dead_worktrees` — the startup salvage of a worktree
#: whose owner pid died un-gracefully to SIGKILL/OOM/crash; the hard-kill twin
#: of ``server_stop``). The already-satisfied gate reads this set: a zero-diff
#: claim over a diff no completed review judged must route to a full review on
#: any of these paths.
MACHINE_REQUEUE_PROVENANCE = frozenset(
    {"orphan_recovery", "server_stop", "hard_kill_salvage"}
)


@dataclass
class Blocker:
    """Structured blocker report (PLAN.md 22.1). Never prose — a human acts on
    this in under a minute (the escalation-precision promise, 22.4)."""

    category: BlockerCategory
    transient: bool = False
    wake_condition: str | None = None      # machine-checkable, see watcher
    root_cause_hypothesis: str = ""
    confidence: float = 0.0                # 0-1; low confidence biases to escalate
    tried: list[str] = field(default_factory=list)
    question: str | None = None            # the ONE decision/info needed
    options: list[BlockerOption] = field(default_factory=list)
    resume_branch: str = ""
    resume_commit: str = ""
    goal: str = ""                         # the step it was attempting (22.4 #1)
    evidence: str = ""                      # exact command + output (22.4 #2)
    raised_at: str = field(default_factory=_now)
    # 🔴 PROVENANCE OF THE PROSE, DECIDED WHERE THE PROSE COMES FROM.
    # `root_cause_hypothesis` and `question` have two completely different
    # origins. `parse_blocker` lifts them verbatim out of the coder's
    # `final_text`, so they are model-authored, unverified, and untrusted.
    # Every OTHER Blocker in this codebase — the fourteen constructions in
    # `orchestrator.py`, plus `fallback_blocker`, `missing_access`,
    # `ci_misconfigured` and `plan_gate.build_blocker` — carries prose written
    # as a source literal in no_human's own repo, which no_human demonstrably
    # DID write and which is usually its own bookkeeping ("max_attempts (3)
    # reached…" is not a hypothesis, it is a counter).
    #
    # Anything that republishes this prose has to know which it is holding.
    # `_abandon_draft_pr` labels the agent-authored kind "in the coding agent's
    # own words — no_human did not write this text and has not verified it";
    # printing that over a harness literal is a FALSE provenance claim in both
    # directions at once, and it tells the reader to distrust a fact the
    # harness established. Defaulting to False is the direction that cannot
    # lie by construction: a Blocker built by a constructor call IS
    # harness-authored, and the one place that is not — `parse_blocker` — sets
    # this True explicitly as a trust boundary, exactly like `options` and
    # `category`.
    reason_is_agent_authored: bool = False
    # {"attempts_before_escalation": int, "tokens_before_escalation": int} or
    # None when unmeasured (e.g. `store.lifetime_usage` failed — telemetry is
    # fail-open, routing is not). Never invent zeros for a missing object.
    escalation_latency: dict[str, int] | None = None
    # Which DB attempt raised this blocker (`store.create_attempt`'s id), so a
    # stored `human_replies` answer can record `source_attempt_id` and the
    # answer-reuse idempotency guard can key "already reused for THIS attempt"
    # (blockers/answers.py, orchestrator._raise_blocker). "" when unknown (a
    # blocker built outside an attempt, e.g. the plan-approval gate).
    attempt_id: str = ""

    def __post_init__(self) -> None:
        # `options` is list[BlockerOption] unconditionally, whoever built it:
        # from_dict off an old row of bare strings, an agent's JSON, or a caller
        # that still passes labels.
        self.options = [BlockerOption.coerce(o) for o in (self.options or [])]

    @property
    def route(self) -> Route:
        return route_for(self.category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "transient": self.transient,
            "wake_condition": self.wake_condition,
            "root_cause_hypothesis": self.root_cause_hypothesis,
            "confidence": self.confidence,
            "tried": list(self.tried),
            "question": self.question,
            "options": [o.to_dict() for o in self.options],
            "resume_branch": self.resume_branch,
            "resume_commit": self.resume_commit,
            "goal": self.goal,
            "evidence": self.evidence,
            "raised_at": self.raised_at,
            "reason_is_agent_authored": self.reason_is_agent_authored,
            "escalation_latency": (
                dict(self.escalation_latency) if self.escalation_latency else None
            ),
            "attempt_id": self.attempt_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Blocker":
        return cls(
            category=BlockerCategory.coerce(data.get("category", "NOVEL_UNKNOWN")),
            transient=bool(data.get("transient", False)),
            wake_condition=data.get("wake_condition"),
            root_cause_hypothesis=data.get("root_cause_hypothesis", ""),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            tried=list(data.get("tried", []) or []),
            question=data.get("question"),
            options=[BlockerOption.coerce(o) for o in (data.get("options") or [])],
            resume_branch=data.get("resume_branch", ""),
            resume_commit=data.get("resume_commit", ""),
            goal=data.get("goal", ""),
            evidence=data.get("evidence", ""),
            raised_at=data.get("raised_at", _now()),
            # Round-trips so a blocker rehydrated from `task.blocker` keeps the
            # provenance the run established. `parse_blocker` OVERWRITES this
            # after calling here, so an agent that puts the key in its own JSON
            # cannot clear it and get its prose published as no_human's.
            reason_is_agent_authored=bool(
                data.get("reason_is_agent_authored", False)),
            escalation_latency=(
                {
                    "attempts_before_escalation": int(
                        (data.get("escalation_latency") or {}).get(
                            "attempts_before_escalation", 0) or 0),
                    "tokens_before_escalation": int(
                        (data.get("escalation_latency") or {}).get(
                            "tokens_before_escalation", 0) or 0),
                }
                if data.get("escalation_latency") else None
            ),
            attempt_id=str(data.get("attempt_id", "") or ""),
        )


def triage(
    blocker: Blocker, *, escalate_below_confidence: float = 0.6,
    budget_exhaustion_terminal: bool = True,
) -> Route:
    """Decide routing for a blocker.

    Honours the 22.2 taxonomy, with two overrides from config:

    * ``escalate_on_low_confidence_below`` (22.8 / Part 22 config): if the agent
      is *unsure what's wrong* (low confidence) on an otherwise-parkable
      blocker, we escalate and ask rather than silently parking on a wrong
      hypothesis.
    * ``budget.exhaustion_terminal`` (the default): BUDGET_EXHAUSTED ends the
      task instead of escalating it. See ``BUDGET_TERMINAL_ROUTE``. Checked
      FIRST — it is not a confidence call, and the budget blocker's confidence
      is a hardcoded 1.0 anyway (the harness counted, it did not guess).
    """
    if (budget_exhaustion_terminal
            and blocker.category is BlockerCategory.BUDGET_EXHAUSTED):
        return BUDGET_TERMINAL_ROUTE
    route = blocker.route
    if route.parked and blocker.confidence < escalate_below_confidence:
        # Unsure → don't thrash silently; ask a human.
        return Route(TaskStatus.ESCALATED, notify_now=True, parked=False)
    return route
