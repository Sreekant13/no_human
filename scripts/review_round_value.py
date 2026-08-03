#!/usr/bin/env python3
"""Do later review rounds add signal, or only noise? (R8 — measurement only.)

The review gate can run several times on one task: the reviewer fails it, the
coder responds, the reviewer runs again. Nothing in the product decides how many
rounds are worth running, and the argument for capping them has always been an
anecdote — a task whose rounds 13-17 asked for the removal of the very thing
rounds 8-12 demanded. This script replaces the anecdote with a count over data
the product already stores.

WHAT IT READS (read-only, always):

  * ``tasks.context`` -> ``review_history``: the compact per-round record the
    orchestrator appends in ``_append_review_history`` — round number, the
    pass/fail verdict, up to 5 blocking findings ("label — evidence head") and
    up to 5 advisory labels.
  * ``task_events`` rows of kind ``review_citation_demoted``: the reviewer-side
    false-positive channel. A blocking finding whose cited file:line does not
    exist is demoted rather than allowed to block, and the demotion is emitted
    as an event. Attributed to a round by counting ``review`` events for the
    same task in timestamp order, since the event itself carries no round.

WHAT IT REPORTS, per round index:

  ``raised``     blocking findings recorded that round.
  ``new``        blocking findings whose label was not raised in an EARLIER
                 round of the same task. This is the signal column: a round
                 that only re-raises what round 1 already said has told the
                 coder nothing new.
  ``repeat``     the complement of ``new`` — an EXACT label match against an
                 earlier round of the same task.
  ``similar``    a weaker, explicitly heuristic second reading of the same
                 question: a finding whose label shares most of its significant
                 words with an earlier round's, i.e. a re-worded repeat that
                 ``repeat`` cannot see. Counted independently of ``new`` /
                 ``repeat``, never added to them.
  ``demoted``    blocking findings demoted that round because their cited
                 location does not exist. Reviewer false positives, verified.
  ``advisory``   advisory (non-blocking) labels recorded that round.

HONEST LIMITS, so nobody reads more out of the table than is in it:

  * ``review_history`` is TRUNCATED by the orchestrator to the last
    ``_REVIEW_HISTORY_ROUNDS * 2`` records per task. Tasks that ran more rounds
    than that are missing their earliest ones, which biases early-round counts
    DOWN. The script reports how many tasks sit at the cap.
  * Each record keeps at most 5 blocking and 5 advisory items, so a round with
    more findings is undercounted. Same direction for every round index, so
    comparisons between rounds survive it; absolute totals do not.
  * "new" is decided by LABEL EQUALITY after normalization, not by any finding
    identity the product tracks. A re-worded repeat counts as new, so ``repeat``
    alone is a LOWER bound on repetition — which on the real database reads as
    exactly zero across every task that raised findings in more than one round.
    ``similar`` is the partial mitigation and is what the verdict corrects with,
    but it is a word-overlap heuristic in BOTH directions: it misses a repeat
    phrased in fresh vocabulary, and it can flag two genuinely different
    findings that happen to share the same nouns. Neither column is a finding
    identity, and the product does not record one.
  * ``demoted`` is the only verification-of-a-finding this data contains. A
    finding that is not demoted is not thereby confirmed correct — it is only
    a finding whose citation resolved. There is no column for "the coder agreed
    it was real", because nothing records that.

WHAT THIS SCRIPT MUST NOT BE USED FOR: changing the round budget as a side
effect. R8 may only ever REDUCE rounds, never add, and even a reduction is a
separate decision taken after reading this — not something to fold into the
commit that measures it. This script writes nothing and imports nothing from
the product.

USAGE

    python scripts/review_round_value.py                 # ~/.no_human/no_human.db
    python scripts/review_round_value.py --db PATH
    python scripts/review_round_value.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("NO_HUMAN_HOME", Path.home() / ".no_human")) / "no_human.db"

# The orchestrator's cap, restated rather than imported: this script is a
# standalone reader and must run against a DB written by any version. If the
# product's value changes, this constant only affects the "at the cap" note.
HISTORY_CAP = 12


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the DB strictly read-only. A measurement may never write."""
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path} — pass --db PATH")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _label(entry: str) -> str:
    """The finding's label, without its evidence head.

    Records are stored as ``f"{label} — {evidence[:160]}"``. Splitting on the
    em-dash separator keeps repeat-detection from being defeated by evidence
    that shifts by a line number between rounds.
    """
    head = entry.split(" — ", 1)[0]
    return " ".join(head.lower().split())


# Words carried by almost every review finding; they make two unrelated labels
# look alike and are dropped before similarity is measured.
_STOPWORDS = frozenset((
    "the", "and", "for", "with", "that", "this", "from", "into", "when",
    "does", "not", "but", "are", "was", "were", "has", "have", "its", "it's",
    "test", "tests", "code", "file", "files",
))
_SIMILARITY = 0.6


def _tokens(label: str) -> frozenset[str]:
    return frozenset(
        w for w in "".join(c if c.isalnum() else " " for c in label).split()
        if len(w) > 3 and w not in _STOPWORDS
    )


def _is_similar(tokens: frozenset[str], earlier: list[frozenset[str]]) -> bool:
    """Heuristic re-wording detector, deliberately kept separate from ``new``.

    Exact-label matching finds essentially no repeats in real data — the
    reviewer re-words a finding every round — so a table with only a ``repeat``
    column would report "no repetition" when what it means is "no repetition
    THIS INSTRUMENT CAN SEE". This is the second reading, over the significant
    words of the label: it is a heuristic, it is reported in its own column,
    and it is never folded into ``new``/``repeat``.
    """
    if not tokens:
        return False
    for prior in earlier:
        if not prior:
            continue
        overlap = len(tokens & prior) / len(tokens | prior)
        if overlap >= _SIMILARITY:
            return True
    return False


def _demotions_by_round(con: sqlite3.Connection) -> dict[tuple[str, int], int]:
    """Map (task_id, round) -> count of demoted blocking findings.

    ``review_citation_demoted`` carries no round, so rounds are recovered by
    ordering each task's review events by timestamp: the Nth ``review`` event of
    a task is round N, and a demotion event belongs to the round whose ``review``
    event follows it (the orchestrator emits the demotion first).
    """
    per_task: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for task_id, ts, data in con.execute(
        "SELECT task_id, ts, data FROM task_events ORDER BY ts"
    ):
        try:
            kind = json.loads(data).get("kind")
        except (ValueError, TypeError):
            continue
        if kind in ("review", "review_citation_demoted"):
            per_task[task_id].append((ts, kind))

    out: Counter[tuple[str, int]] = Counter()
    for task_id, rows in per_task.items():
        round_n = 1
        pending = 0
        for _ts, kind in sorted(rows):
            if kind == "review_citation_demoted":
                pending += 1
            else:  # a "review" event closes the round the demotions belong to
                if pending:
                    out[(task_id, round_n)] += pending
                    pending = 0
                round_n += 1
        if pending:  # demotions after the last review event (crash, abort)
            out[(task_id, round_n)] += pending
    return out


# Every column a round bucket reports, so a zero is emitted as a zero rather
# than as a missing key. A Counter drops what was never incremented, which made
# `--json` change SHAPE with the data — the reader most likely to be automated
# is the one least able to tell an absent key from a zero.
_COLUMNS = ("tasks", "passed", "failed", "raised", "new", "repeat", "similar",
            "demoted", "advisory")


def collect(con: sqlite3.Connection) -> dict:
    demotions = _demotions_by_round(con)

    rounds: dict[int, Counter] = defaultdict(lambda: Counter({c: 0 for c in _COLUMNS}))
    tasks_seen = 0
    tasks_with_history = 0
    tasks_at_cap = 0
    tasks_blocking_in_multiple_rounds = 0
    max_round = 0

    for task_id, ctx in con.execute("SELECT id, context FROM tasks"):
        tasks_seen += 1
        if not ctx:
            continue
        try:
            history = (json.loads(ctx) or {}).get("review_history") or []
        except (ValueError, TypeError):
            continue
        if not history:
            continue
        tasks_with_history += 1
        if len(history) >= HISTORY_CAP:
            tasks_at_cap += 1

        # "Earlier" means an earlier ROUND, never earlier in the same round:
        # two findings raised together are two findings, not a repetition.
        seen_labels: set[str] = set()
        seen_tokens: list[frozenset[str]] = []
        rounds_with_blocking = 0
        for position, record in enumerate(history, start=1):
            n = int(record.get("round") or position)
            max_round = max(max_round, n)
            bucket = rounds[n]
            bucket["tasks"] += 1
            bucket["passed" if record.get("passed") else "failed"] += 1
            if record.get("blocking"):
                rounds_with_blocking += 1
            this_round: list[tuple[str, frozenset[str]]] = []
            for entry in record.get("blocking") or []:
                bucket["raised"] += 1
                label = _label(entry)
                tokens = _tokens(label)
                if _is_similar(tokens, seen_tokens):
                    bucket["similar"] += 1
                if label in seen_labels:
                    bucket["repeat"] += 1
                else:
                    bucket["new"] += 1
                this_round.append((label, tokens))
            for label, tokens in this_round:
                seen_labels.add(label)
                seen_tokens.append(tokens)
            bucket["advisory"] += len(record.get("advisory") or [])
            bucket["demoted"] += demotions.get((task_id, n), 0)
        if rounds_with_blocking > 1:
            tasks_blocking_in_multiple_rounds += 1

    return {
        "tasks_total": tasks_seen,
        "tasks_with_review_history": tasks_with_history,
        "tasks_at_history_cap": tasks_at_cap,
        "tasks_blocking_in_multiple_rounds": tasks_blocking_in_multiple_rounds,
        "max_round": max_round,
        "rounds": {n: dict(rounds[n]) for n in sorted(rounds)},
        "demotions_total": sum(demotions.values()),
    }


def _verdict(data: dict) -> list[str]:
    """One line on whether rounds beyond N are mostly re-raised findings.

    The cut is the first round index at which NEW blocking findings fall below
    a quarter of what round 1 raised — chosen before looking at the numbers and
    stated here so the reader can disagree with it in the open.
    """
    rounds = data["rounds"]
    if not rounds:
        return ["VERDICT: no review rounds in this database — nothing measured."]

    first_new = rounds[1]["new"] if 1 in rounds else 0
    if not first_new:
        return ["VERDICT: round 1 raised no blocking findings; the comparison "
                "this script exists to make is undefined on this data."]

    cut = None
    for n in sorted(rounds):
        if n > 1 and rounds[n].get("new", 0) < first_new * 0.25:
            cut = n
            break

    # VOLUME and NOVELTY are two different questions and the verdict has to
    # answer both on EVERY path. An earlier version asked about novelty only
    # when volume had already collapsed, which made the no-cut branch blind by
    # construction: a run whose later rounds keep raising findings at full
    # volume and re-word the same one every time would have been reported as
    # "does not support capping rounds" — the exact opposite of what that data
    # says. The window differs between branches; the correction does not.
    window_start = cut if cut is not None else 2
    late_new = sum(r.get("new", 0) for n, r in rounds.items() if n >= window_start)
    late_raised = sum(r.get("raised", 0) for n, r in rounds.items() if n >= window_start)
    late_similar = sum(r.get("similar", 0) for n, r in rounds.items() if n >= window_start)
    # `new` minus `similar` is the strictest available count of findings the
    # earlier rounds did not already make, in either wording.
    genuinely_new = late_new - late_similar
    mostly_novel = bool(late_raised) and genuinely_new >= late_raised * 0.5

    lines = []
    census = (
        f"Rounds {window_start}+ raise {late_raised} blocking finding(s) of "
        f"which {late_new} are new by label and {late_similar} resemble an "
        "earlier round's"
    )
    if cut is None:
        headline = (
            f"VERDICT: new blocking findings never fall below 25% of round 1's "
            f"({first_new}) within {data['max_round']} observed rounds"
        )
        reading = (
            "— volume holds up and so does novelty, so this data does not "
            "support capping rounds."
            if mostly_novel else
            "— but most of what those rounds raise restates a finding an "
            "earlier round already made. Volume is NOT the signal here: the "
            "later rounds are REDUNDANT despite never thinning out, which is "
            "the shape a round cap would address."
        )
    else:
        headline = (
            f"VERDICT: new blocking findings drop below 25% of round 1's "
            f"({first_new}) at round {cut}"
        )
        reading = (
            f"— what falls off after round {cut - 1} is the VOLUME of findings, "
            "not their novelty. On this data a late round is RARE rather than "
            "redundant, and the case for capping rounds is not made here."
            if mostly_novel else
            f"— and most of what rounds {cut}+ raise restates a finding an "
            "earlier round already made. A late round here is REDUNDANT, which "
            "is the shape a round cap would address."
        )
    lines.append(f"{headline}. {census} {reading}")
    tail_tasks = sum(r["tasks"] for n, r in rounds.items() if n >= window_start)
    if tail_tasks < 10:
        lines.append(
            f"CAUTION: only {tail_tasks} task-round(s) sit at round "
            f"{window_start} or beyond. The verdict above is directional, not "
            "a decision."
        )
    return lines


def render(data: dict) -> str:
    out: list[str] = []
    out.append(
        f"tasks: {data['tasks_total']} total, "
        f"{data['tasks_with_review_history']} with a review history, "
        f"{data['tasks_at_history_cap']} at the {HISTORY_CAP}-record cap "
        "(their earliest rounds are missing)"
    )
    out.append("")
    header = (f"{'round':>5}  {'tasks':>5}  {'pass':>5}  {'fail':>5}  "
              f"{'raised':>6}  {'new':>5}  {'repeat':>6}  {'similar':>7}  "
              f"{'demoted':>7}  {'advisory':>8}")
    out.append(header)
    out.append("-" * len(header))
    for n in sorted(data["rounds"]):
        r = data["rounds"][n]
        out.append(
            f"{n:>5}  {r.get('tasks', 0):>5}  {r.get('passed', 0):>5}  "
            f"{r.get('failed', 0):>5}  {r.get('raised', 0):>6}  "
            f"{r.get('new', 0):>5}  {r.get('repeat', 0):>6}  "
            f"{r.get('similar', 0):>7}  "
            f"{r.get('demoted', 0):>7}  {r.get('advisory', 0):>8}"
        )
    out.append("")
    out.extend(_verdict(data))
    repeats = sum(r.get("repeat", 0) for r in data["rounds"].values())
    similar = sum(r.get("similar", 0) for r in data["rounds"].values())
    multi = data["tasks_blocking_in_multiple_rounds"]
    if repeats == 0 and similar == 0 and multi:
        out.append(
            f"NOTE: {multi} task(s) raised blocking findings in more than one "
            "round, and neither exact-label nor word-overlap matching found a "
            "single repetition among them. Read that as 'this instrument sees "
            "no repetition', not as 'the reviewer never repeats itself' — both "
            "matchers work on a 160-char label the reviewer re-words each round."
        )
    if data["demotions_total"] == 0:
        out.append(
            "NOTE: zero citation demotions in this database. The 'demoted' "
            "column is therefore uninformative here — read it as 'not "
            "measured', not as 'no reviewer false positives'."
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"SQLite database to read (default: {DEFAULT_DB})")
    ap.add_argument("--json", action="store_true", help="emit raw counts as JSON")
    args = ap.parse_args(argv)

    con = _connect(args.db)
    try:
        data = collect(con)
    finally:
        con.close()

    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"source: {args.db} (read-only)")
        print(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
