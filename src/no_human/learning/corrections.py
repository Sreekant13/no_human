"""Supervisor corrections become learning proposals (B2).

The supervisor (``agent/supervisor.py``) fires every ``check_every`` tool calls
and returns CONTINUE / CORRECT / ANSWER / STOP. Every decision is emitted as a
``supervisor_decision`` event and persisted to ``task_events`` — and there it
stopped. A ``correct`` decision is a labelled, in-flight "the agent got this
wrong" signal, injected into ONE session's context via ``additionalContext``
and then discarded with that session. On the measured store that is 257
corrections across 69 tasks that taught the product nothing.

B2 routes them into the SAME human-gated proposals path B1 built for reviewer
FAIL findings: distil → PII gate → dedupe → ``memories`` row with
``confirmed=0``/``source="proposed"``, confirmed by a human in ``nh learnings``.
Nothing here writes an active rule, and an unconfirmed proposal is unreachable
by the agents (``confirmed_rules`` is built from ``list_memories(confirmed=True)``).

AGGREGATION IS THE POINT, and it is why this module exists instead of a
one-line call into :meth:`LearningQueue.propose_from_review`. A reviewer FAIL
round is already an aggregate — one verdict over a whole diff. A supervisor
correction is not: it is one nudge out of dozens in a single session, most of
them local and disposable ("you ran `python`, use `python3`"). Proposing each
one would put 257 entries in a queue that already floods at 211
(``learning/curator.py``). Repetition, not isolation, is what marks a
correction as a durable lesson — so corrections are clustered by
``(project, gist)`` and only a cluster seen ``min_occurrences`` times (default
2) is ever proposed.

WHY A DETERMINISTIC GIST AND NOT SIMILARITY CLUSTERING. A greedy
Jaccard/nearest-neighbour clustering groups more, but a cluster's identity then
depends on the whole corpus: add three corrections next week and the cluster's
canonical representative moves, its dedupe key changes, and the SAME lesson is
proposed a second time. :func:`normalize_gist` is a pure function of ONE
message, so ``(project, gist)`` is stable under re-run and under the cluster
growing — which is exactly the property the idempotency requirement needs.

THE COST, stated here rather than left to be discovered: the gist is a
POSITIONAL key, so it groups repeated and near-identical corrections and it
does NOT group paraphrases. Two messages that say the same thing in different
words, or that lead with different clauses, get different gists and stay two
one-offs. ``test_a_paraphrase_of_the_same_correction_does_not_cluster`` pins
that limit so it cannot be mistaken for a bug later. Measured on the store this
milestone was written against (257 corrections, 69 tasks, 2 repos):

    tokens=3 → 238 gists, 14 clusters at >=2, covering 33 corrections
    tokens=4 → 244 gists,  9 clusters at >=2, covering 22 corrections
    tokens=5 → 248 gists,  5 clusters at >=2, covering 14 corrections

Two other corpus-independent keys were measured and rejected: the k
lexicographically smallest tokens (2-3 clusters) and the k longest tokens (2-4
clusters). Neither grouped paraphrases either, and both grouped less. The knob
is one constant, and the trade is precision — does a cluster really name ONE
recurring mistake — against how many lessons are found at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("no_human.learning")

__all__ = [
    "CorrectionRecord",
    "CorrectionCluster",
    "normalize_gist",
    "is_project_scoped",
    "cluster_corrections",
    "build_correction_distill_prompt",
    "MIN_OCCURRENCES",
]

# How many significant tokens make the gist. See the module docstring for the
# measured effect of moving it — this is the one knob.
_GIST_TOKENS = 4

# A cluster must be seen this many times before it is a lesson rather than a
# one-off nudge. 1 is noise by construction: the supervisor corrects local,
# disposable mistakes far more often than durable ones.
MIN_OCCURRENCES = 2

# How many verbatim corrections are quoted as evidence in the prompt and the
# stored body. The cluster's size is what matters; three examples is enough for
# a human to check the claim.
_MAX_EXAMPLES = 3
_MAX_EXAMPLE_CHARS = 220

# Words that carry no signal about WHICH mistake was corrected. Deliberately
# generic English + the supervisor prompt's own vocabulary; nothing repo- or
# employer-specific belongs here.
_STOP_WORDS = frozenset("""
the a an and or but if then than that this these those it its is are was were
be been being to of in on at for with from by as not no do does did done have
has had will would should could can may you your yours agent must need needs
needed run running ran use using used make makes made get gets got just now
still yet only before after when while which what where who how why into out
up down over under again more most some any all each other same such own too
very via per etc instead rather also here there they them their we our us
""".split())

# Tokens are words of 4+ characters. Shorter ones are almost all stop words or
# noise; digits and punctuation are dropped entirely so a path, a line number,
# a pid or a timestamp can never be what makes two corrections differ.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z_-]{3,}")


def normalize_gist(message: str | None, *, tokens: int = _GIST_TOKENS) -> str:
    """The clustering key for one correction message.

    Lowercased significant words, de-duplicated in order of first appearance,
    the first *tokens* of them, then SORTED so the same leading words in a
    different arrangement share a key. Returns ``""`` for a message with
    nothing significant in it — callers treat that as unclusterable.

    Pure and corpus-independent on purpose, and POSITIONAL as a consequence:
    it groups repeats, not paraphrases. See the module docstring for what that
    costs and what was measured.
    """
    seen: list[str] = []
    for word in _TOKEN.findall(message or ""):
        low = word.lower()
        if low in _STOP_WORDS or low in seen:
            continue
        seen.append(low)
        if len(seen) >= tokens:
            break
    return " ".join(sorted(seen))


@dataclass(frozen=True)
class CorrectionRecord:
    """One persisted supervisor ``correct`` decision.

    ``message`` is the correction text as emitted — already truncated to 200
    characters by ``Orchestrator.emit``'s ``message[:200]``, which is the only
    form that was ever stored. ``project`` is the task's ``repo_path``.
    """

    task_id: str
    project: str | None
    message: str
    ts: float = 0.0


@dataclass
class CorrectionCluster:
    """A recurring correction: same project, same gist, seen N times."""

    project: str | None
    gist: str
    records: list[CorrectionRecord] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def task_ids(self) -> list[str]:
        """Distinct tasks the correction was raised on, oldest first. A cluster
        spanning several tasks is a much stronger lesson than one session
        tripping on the same thing repeatedly — the human sees both."""
        out: list[str] = []
        for r in self.records:
            if r.task_id and r.task_id not in out:
                out.append(r.task_id)
        return out

    def examples(self, limit: int = _MAX_EXAMPLES) -> list[str]:
        """Distinct verbatim correction texts, bounded, for evidence."""
        out: list[str] = []
        for r in self.records:
            text = " ".join((r.message or "").split())[:_MAX_EXAMPLE_CHARS]
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out


def is_project_scoped(record: CorrectionRecord) -> bool:
    """True when this correction can be attributed to a repository.

    A task with no ``repo_path`` produces corrections with ``project=None``,
    and a proposal written with ``project=None`` is a GLOBAL memory:
    ``Store.list_memories`` matches it with ``(project = ? OR project IS
    NULL)``, so it is injected into EVERY project's rules. Two unrelated
    repo-less tasks could also merge into one cluster under the ``(None, gist)``
    key, inventing a recurrence that never happened in any single repo —
    something B1 cannot do, because its proposals are keyed on a task's own
    ``repo_path``.

    So repo-less corrections are excluded rather than clustered. The
    conservative direction: the cost of excluding them is a lesson nobody
    learns, and the cost of including them is a rule nobody asked for applying
    everywhere, shown to the confirming human without its blast radius.
    """
    return bool((record.project or "").strip())


def cluster_corrections(
    records: list[CorrectionRecord], *, min_occurrences: int = MIN_OCCURRENCES,
) -> list[CorrectionCluster]:
    """Group corrections by ``(project, gist)`` and keep only the recurring ones.

    Returns clusters sorted by size (largest first), then by gist so the order
    is deterministic for equal sizes.

    Two kinds of record are dropped before grouping, both counted and logged
    rather than silently capped:

    * an empty gist — nothing to cluster ON, and a key of ``""`` would collapse
      every such correction into one meaningless "cluster";
    * an unattributable project (:func:`is_project_scoped`) — a proposal with
      ``project=None`` is a GLOBAL rule injected into every repository.
    """
    buckets: dict[tuple[str | None, str], CorrectionCluster] = {}
    dropped_global = dropped_empty = 0
    for rec in records or []:
        if not is_project_scoped(rec):
            dropped_global += 1
            continue
        gist = normalize_gist(rec.message)
        if not gist:
            dropped_empty += 1
            continue
        key = (rec.project, gist)
        cluster = buckets.get(key)
        if cluster is None:
            cluster = buckets[key] = CorrectionCluster(rec.project, gist)
        cluster.records.append(rec)
    if dropped_global or dropped_empty:
        log.info(
            "supervisor corrections not clustered: %d unattributable to a "
            "repository (would become a global rule), %d with no significant "
            "content", dropped_global, dropped_empty)
    return sorted(
        (c for c in buckets.values() if c.count >= min_occurrences),
        key=lambda c: (-c.count, c.gist),
    )


def build_correction_distill_prompt(cluster: CorrectionCluster) -> str:
    """The bounded, single-turn prompt handed to the utility tier.

    Same four-line contract as B1's review distiller, so the SAME parser
    (``learning.queue.parse_review_lesson``) reads both replies — one shape to
    test, one shape to change.
    """
    examples = "\n".join(f"  - {e}" for e in cluster.examples())
    tasks = len(cluster.task_ids)
    return (
        "A supervisor watching an autonomous coding agent issued the SAME "
        "correction repeatedly while it worked in this repository. It fired "
        f"{cluster.count} times across {tasks} separate task(s). Examples of "
        f"what it said:\n{examples}\n"
        "\nDistill ONE durable lesson a FUTURE task in THIS repository should "
        "carry, so the agent does not have to be corrected for this again. "
        "Describe the repository's standing requirement or the recurring "
        "mistake — not one session's incident.\n"
        "Reply in EXACTLY these four lines, under 600 characters in total:\n"
        "TYPE: rule|anti_pattern   (rule = a requirement this repo imposes; "
        "anti_pattern = a mistake to avoid)\n"
        "TITLE: <one line, <=80 chars>\n"
        "LESSON: <<=300 chars, concrete and checkable — name the file, API or "
        "command involved>\n"
        "TAGS: <2-4 comma-separated lowercase keywords that would appear in a "
        "future task's text>"
    )
