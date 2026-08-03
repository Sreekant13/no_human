"""Human-confirmed learning queue (PLAN.md 4.5).

On success → propose a skill/fact. On failure → propose an anti-pattern/rule.
On a reviewer FAIL round → propose a rule/anti-pattern distilled from the
blocking findings (B1/G2).
Proposals are queued (``confirmed=0``) and *never* enter the active rule set
until a human confirms them in ``nh learnings`` — auto-writing rules from a
leniency-biased self-assessment would let bad lessons accumulate silently.

The active rule set (confirmed memories) is what later tasks consult; proposals
are inert until promoted.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..core.db import Store
from ..core.task import Task, TaskStatus
from .pii import contains_pii

log = logging.getLogger("no_human.learning")


# Memory types (matches the migrations schema CHECK-free `type` column).
TYPE_SKILL = "skill"
TYPE_FACT = "fact"
TYPE_RULE = "rule"
TYPE_ANTI_PATTERN = "anti_pattern"

# Failure categories that are transient/resource/environmental, not a reusable
# code lesson. A budget cap, an infra flake, a quota wall, a waiting-on-dep, or
# a missing credential recurs because of the environment — capturing it as a
# durable anti-pattern floods the human's confirm queue with noise (the queue
# reached 197 pending, almost all BUDGET_EXHAUSTED + env-failure artifacts).
# Genuine anti-patterns (NOVEL_UNKNOWN, STAGNATION, SCOPE_EXPLOSION, IMPOSSIBLE,
# AMBIGUITY) still propose.
NON_LEARNABLE_CATEGORIES = frozenset({
    "TRANSIENT_INFRA",
    "QUOTA",
    "DEPENDENCY_WAIT",
    "BUDGET_EXHAUSTED",
    "MISSING_ACCESS",
})


# A reviewer FAIL is QUALITY-shaped by construction: the gate judges a diff
# against the acceptance criteria, so its findings carry no failure category
# and NON_LEARNABLE_CATEGORIES above — transient infra, quota, budget, missing
# access — cannot apply to them. There is exactly ONE infra-shaped verdict this
# path can produce: the fail-closed sentinel the orchestrator writes when the
# REVIEWER ITSELF crashes (`ChecklistItem("reviewer run", False, "reviewer
# crashed: …")`). That is an environment failure wearing a finding's clothes,
# and learning it would file an SDK outage in the human's confirm queue as a
# durable lesson about the repo. It is filtered for the same reason the
# categories above are, and it is the only such case — anything the reviewer
# genuinely found is learnable.
_INFRA_FINDING_MARKERS = ("reviewer crashed", "reviewer run", "reviewerunavailable")

_MAX_FINDINGS = 3        # the blocking few; the gate already orders by severity
_MAX_EVIDENCE = 200      # chars of cited evidence kept per finding
_MAX_DISTILL = 600       # bound on the utility tier's whole reply
_MAX_CONTENT = 1000      # bound on the stored proposal body

# An async (prompt) -> text callable. Injected, exactly like intake's optional
# `backend=`, so this layer never reaches for an LLM itself and tests never
# make a real call.
DistillFn = Callable[[str], Awaitable[str]]

# A sync (text) -> None sink for "this proposal did NOT get written, and here is
# why". Injected the same way `DistillFn` is; the orchestrator passes
# `self._advisory`, so a refusal lands in the event stream and `nh doctor`'s
# count instead of vanishing into a bare `None` return that the caller cannot
# tell apart from "nothing was learnable".
NoteFn = Callable[[str], None]


def _is_infra_finding(finding: dict[str, Any]) -> bool:
    text = f"{finding.get('label', '')} {finding.get('evidence', '')}".lower()
    return any(marker in text for marker in _INFRA_FINDING_MARKERS)


def _finding_lines(findings: list[dict[str, Any]]) -> str:
    lines = []
    for f in findings:
        where = f":{f.get('line')}" if f.get("file") and f.get("line") else ""
        loc = f" ({f.get('file')}{where})" if f.get("file") else ""
        lines.append(
            f"  - {str(f.get('label') or '?')}"
            f" — {str(f.get('evidence') or '')[:_MAX_EVIDENCE]}{loc}"
        )
    return "\n".join(lines)


_STOP_TAGS = frozenset({
    "this", "that", "with", "from", "must", "does", "when", "then", "have",
    "there", "which", "into", "should", "review", "test", "tests", "code",
})


def _tags_from_findings(findings: list[dict[str, Any]]) -> list[str]:
    """Trigger keywords derived from the findings themselves (learning/
    triggers.py matches a tag as a substring of a future task's text), plus
    ``review`` so the queue can be filtered by where a lesson came from."""
    words: list[str] = []
    for f in findings:
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{3,}", str(f.get("label") or "")):
            low = word.lower()
            if low not in words and low not in _STOP_TAGS:
                words.append(low)
    return ["review", *words[:4]]


def build_review_distill_prompt(task: Task, findings: list[dict[str, Any]]) -> str:
    """The bounded, single-turn prompt handed to the utility tier."""
    return (
        "An independent code reviewer BLOCKED an autonomous coding agent's "
        "change to this repository. Its blocking findings were:\n"
        f"{_finding_lines(findings)}\n"
        f"\nThe task was: {task.title}\n"
        "\nDistill ONE durable lesson a FUTURE task in THIS repository should "
        "carry, so the same finding is not re-derived from scratch. Describe "
        "the repository's requirement, not this task's bug.\n"
        "Reply in EXACTLY these four lines, under 600 characters in total:\n"
        "TYPE: rule|anti_pattern   (rule = a requirement this repo imposes; "
        "anti_pattern = a mistake to avoid)\n"
        "TITLE: <one line, <=80 chars>\n"
        "LESSON: <<=300 chars, concrete and checkable — name the file, API or "
        "command involved>\n"
        "TAGS: <2-4 comma-separated lowercase keywords that would appear in a "
        "future task's text>"
    )


def parse_review_lesson(
    text: str | None,
) -> tuple[str, str, str, list[str]] | None:
    """(type, title, lesson, tags) from the utility tier's reply, or None when
    it did not answer in the required shape. Advisory: an unparseable reply
    degrades to the undistilled findings, it never blocks the proposal."""
    raw = (text or "").strip()[:_MAX_DISTILL]
    if not raw:
        return None
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().upper() in ("TYPE", "TITLE", "LESSON", "TAGS"):
            fields[key.strip().upper()] = value.strip()
    lesson = fields.get("LESSON", "")
    if not lesson:
        return None  # no lesson → nothing was distilled
    mem_type = TYPE_RULE if fields.get("TYPE", "").lower().startswith(
        "rule") else TYPE_ANTI_PATTERN
    tags = [t.strip().lower() for t in fields.get("TAGS", "").split(",") if t.strip()]
    return mem_type, fields.get("TITLE", "")[:120], lesson[:400], tags[:4]


def _sig(*parts: str) -> str:
    """Stable dedupe signature so the same lesson isn't proposed twice."""
    raw = "\x1f".join(p.strip().lower() for p in parts if p)
    return "learn:" + hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class Proposal:
    mem_type: str
    title: str
    content: str
    dedupe_key: str
    tags: list[str]


class LearningQueue:
    """Proposes learnings from task outcomes and manages confirmation."""

    def __init__(self, store: Store):
        self.store = store

    # ----------------------------- propose --------------------------------- #

    async def propose_from_outcome(
        self, task: Task, *, status: TaskStatus, blocker: dict[str, Any] | None = None,
        summary: str = "",
    ) -> str | None:
        """Queue a proposal derived from a terminal task outcome.

        success (awaiting_approval/done) → skill; structural blocker/failure →
        anti-pattern. Returns the new memory id, or None if deduped/not applicable.
        """
        proposal = self._build(task, status=status, blocker=blocker, summary=summary)
        if proposal is None:
            return None
        # Personal data is never a durable engineering lesson. A task title or a
        # blocker's root-cause text can quote a user's data verbatim; drop the
        # whole proposal rather than redact it (see learning/pii.py).
        pii = contains_pii(proposal.title, proposal.content)
        if pii is not None:
            log.info("refused a proposed learning carrying personal data (%s)",
                     pii.kind)
            return None
        return await self.store.add_memory(
            mem_type=proposal.mem_type,
            title=proposal.title,
            content=proposal.content,
            tags=proposal.tags,
            project=task.repo_path,
            source="proposed",
            confirmed=False,
            dedupe_key=proposal.dedupe_key,
        )

    def _build(
        self, task: Task, *, status: TaskStatus, blocker: dict[str, Any] | None,
        summary: str,
    ) -> Proposal | None:
        if status in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE):
            title = f"Approach that worked: {task.title}"[:120]
            content = (
                f"Task '{task.title}' completed to a reviewable PR.\n"
                f"{('Summary: ' + summary) if summary else ''}\n"
                "Consider capturing the successful approach as a reusable skill."
            ).strip()
            return Proposal(
                TYPE_SKILL, title, content,
                _sig("skill", task.title, task.repo_path or ""),
                tags=["success", task.source],
            )

        # Failure / structural blocker → anti-pattern (22.8).
        if blocker:
            cat = blocker.get("category", "NOVEL_UNKNOWN")
            # Transient/resource/environmental failures are not reusable code
            # lessons — don't durably propose them (they flooded the queue).
            if str(cat).upper() in NON_LEARNABLE_CATEGORIES or blocker.get("transient"):
                return None
            cause = blocker.get("root_cause_hypothesis", "")
            tried = blocker.get("tried", []) or []
            title = f"Anti-pattern [{cat}]: {task.title}"[:120]
            content = (
                f"Category: {cat}\n"
                f"Root cause hypothesis: {cause}\n"
                f"Tried: {'; '.join(tried) if tried else '(none recorded)'}\n"
                "If this surprise recurs, anticipate it: capture the fix as a "
                "rule/skill so it becomes an auto-handled case (Part 22.8)."
            )
            return Proposal(
                TYPE_ANTI_PATTERN, title, content,
                _sig("anti", cat, cause or task.title, task.repo_path or ""),
                tags=["blocker", cat],
            )
        return None

    # --------------------- reviewer findings (B1 / G2) --------------------- #

    async def propose_from_review(
        self, task: Task, *, findings: list[dict[str, Any]],
        attempt: int | None = None, review_round: int = 1,
        distill: DistillFn | None = None, note: NoteFn | None = None,
    ) -> str | None:
        """Queue ONE proposal distilled from a reviewer FAIL round's blocking
        findings. Returns the new memory id, or None (nothing learnable, or
        deduped against a finding already proposed).

        The gate's findings were the biggest labelled-failure signal the product
        threw away: they were written to ``task.context['review_feedback']``,
        read by the next attempt of the SAME task, and discarded with it. The
        next task re-derived "this repo requires X" from scratch.

        *distill* is an optional async ``(prompt) -> text`` callable (the
        utility tier, injected by the orchestrator exactly as intake injects a
        backend). Without it the lesson is the findings themselves — degraded,
        never absent, and never an LLM call this layer made on its own.

        *note* is an optional sink for the two ways this returns ``None`` with
        something a human should know: a refusal on personal data, and a dedupe
        hit. Without it both are silent, which is how the second occurrence of a
        recurring finding disappeared with no trace.

        GATE INDEPENDENCE (design principle 6): the proposal is written
        ``confirmed=0``/``source="proposed"``, and the reviewer prompt is built
        from ``list_memories(confirmed=True, …)``. A reviewer therefore cannot
        consume a rule derived from its own verdict until a HUMAN confirms it.
        """
        proposal = await self._build_from_review(
            task, findings=findings, attempt=attempt,
            review_round=review_round, distill=distill,
        )
        if proposal is None:
            return None
        # THE SAME GATE `propose_from_outcome` APPLIES, and this path needs it
        # at least as badly: a reviewer's cited evidence quotes repo content
        # VERBATIM, so whatever a file or a diff hunk contains — a shipping
        # address in a fixture, a personal email in a test — arrives here inside
        # `evidence` and would be persisted to `memories` and shown back to the
        # user. Drop the whole proposal rather than redact it (learning/pii.py).
        pii = contains_pii(proposal.title, proposal.content)
        if pii is not None:
            log.info("refused a proposed learning carrying personal data (%s)",
                     pii.kind)
            # Not silent: a dropped lesson is a thing a human may need to
            # explain later, and `kind` names the category without ever
            # carrying the value (PIIFinding exposes nothing else on purpose).
            if note is not None:
                note("review learning refused: the finding's evidence carries "
                     f"personal data ({pii.kind})")
            return None
        mem_id = await self.store.add_memory(
            mem_type=proposal.mem_type,
            title=proposal.title,
            content=proposal.content,
            tags=proposal.tags,
            # Repo-scoped exactly as every other proposal is today. The absolute
            # path is a poor key (B4 replaces it with a remote hash); changing
            # it HERE only would split one repo's rows across two conventions.
            project=task.repo_path,
            # NOT the provenance string. `source` is the queue-visibility
            # contract — `pending()`, `nh learnings`, the API and the ingester
            # all select `source="proposed"`, so a proposal that named its task
            # here would be invisible to the human gate it exists for. The
            # task/attempt/round provenance rides in the content, where the
            # human reading the confirm queue actually sees it (B3 formalises
            # it into a structured evidence field).
            source="proposed",
            confirmed=False,
            dedupe_key=proposal.dedupe_key,
        )
        # A dedupe hit is the queue working as designed — but it is ALSO the
        # only evidence that this wall was hit twice, and `add_memory` returns a
        # bare None for it. Recurrence is the signal a human most wants out of
        # this queue ("we keep tripping on the same rule"), so say it rather
        # than let the second occurrence vanish. The dedupe key is quoted so the
        # reader can find the row it collapsed onto.
        if mem_id is None and note is not None:
            note("recurring review finding, deduped to "
                 f"{proposal.dedupe_key}")
        return mem_id

    async def _build_from_review(
        self, task: Task, *, findings: list[dict[str, Any]],
        attempt: int | None, review_round: int, distill: DistillFn | None,
    ) -> Proposal | None:
        learnable = [f for f in (findings or []) if not _is_infra_finding(f)]
        if not learnable:
            return None
        learnable = learnable[:_MAX_FINDINGS]
        evidence = _finding_lines(learnable)

        mem_type, title, lesson, tags = TYPE_ANTI_PATTERN, "", "", []
        if distill is not None:
            parsed = parse_review_lesson(
                await distill(build_review_distill_prompt(task, learnable))
            )
            if parsed is not None:
                mem_type, title, lesson, tags = parsed
        if not title:
            title = f"Review blocked: {learnable[0].get('label') or task.title}"
        if not lesson:
            # Degraded but honest: the findings ARE the lesson, unpolished.
            lesson = "(not distilled) " + "; ".join(
                str(f.get("label") or "") for f in learnable)
        if not tags:
            tags = _tags_from_findings(learnable)

        provenance = "task {} · attempt {} · review round {}".format(
            task.id[:8], attempt if attempt is not None else "?", review_round)
        # ORDER IS LOAD-BEARING, and a trailing `[:_MAX_CONTENT]` over
        # header→evidence→lesson silently ate the lesson: three findings at
        # `_MAX_EVIDENCE` each fill ~700 of the 1000 characters before the
        # word "Lesson" is even reached, so the ONE distilled sentence the
        # utility call was spent on arrived truncated to a few dozen chars —
        # and the padding that displaced it is the verbatim evidence, which
        # the reader can still go and look up in the repo. So: the lesson goes
        # FIRST and whole, and the evidence gets the room that is left. Both
        # are budgeted explicitly; nothing here relies on a final slice.
        head = f"Blocked by the review gate ({provenance}).\nLesson: {lesson}\n"
        label = "Blocking findings:\n"
        room = _MAX_CONTENT - len(head) - len(label)
        content = (head + label + evidence[:room]) if room > 0 else head[:_MAX_CONTENT]
        return Proposal(
            mem_type, title[:120], content,
            # Keyed on the FINDING LABELS, not on the distilled text: the same
            # finding raised again next round (or on the next task in this
            # repo) must collapse onto one queue entry, and an LLM's phrasing
            # is not stable enough to dedupe on.
            _sig("review", *sorted(
                str(f.get("label") or "") for f in learnable
            ), task.repo_path or ""),
            tags=tags,
        )

    # --------------------------- confirm / list ---------------------------- #

    async def pending(self) -> list[dict[str, Any]]:
        """Proposals awaiting human confirmation."""
        return await self.store.list_memories(confirmed=False, source="proposed")

    async def active(self) -> list[dict[str, Any]]:
        """The confirmed, active rule/skill set later tasks consult."""
        return await self.store.list_memories(confirmed=True)

    async def confirm(self, mem_id: str) -> bool:
        return await self.store.confirm_memory(mem_id)

    async def reject(self, mem_id: str) -> bool:
        return await self.store.delete_memory(mem_id)
