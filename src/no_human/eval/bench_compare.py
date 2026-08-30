"""Paired, per-spec comparison of two north-star bench runs (bench-v2 · V2).

WHY THIS EXISTS. Two headline numbers cannot tell you whether a change helped.
"90.7% → 90.7%" is compatible with nothing moving at all AND with five specs
breaking while five others got fixed; those are opposite facts about a change,
and the difference of two rates reports them identically. Worse, the rates are
computed over spec sets that need not be the same — a run that quietly lost
three specs to an unresolvable repo compares its survivors against the
baseline's full corpus and the arithmetic never complains.

So this module pairs the runs SPEC BY SPEC on ``task_id`` and reports:

  - the flips, in both directions, with each side's outcome and notes;
  - every spec present in one run and not the other, NAMED — never silently
    dropped into a denominator that then reads as "no change";
  - McNemar's exact test on the discordant pairs, printed BESIDE the counts it
    came from, because below ~6 discordant pairs it cannot reach significance
    at all and a bare p there is decoration (see ``MIN_DISCORDANT_FOR_POWER``).

THIS IS A REPORT, NOT A GATE. The publish/regression gate lives in
``northstar_card.northstar_gate`` / ``publish_refusals`` and is the only thing
allowed to block. Nothing here exits non-zero, writes a baseline, or renders
the tracked report — a second write-path into those is the defect the existing
comments in that module are about.

NO NEW DEPENDENCY. The exact binomial tail is ``math.comb``; the lean-stack
rule forbids pulling scipy in for one number, and this one is exact rather
than approximated, so there is nothing to gain from it either.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .northstar import BenchScore
from .northstar_card import score_ran, score_succeeded

# Below this many discordant pairs, McNemar's exact two-sided test CANNOT
# produce a significant result at α=0.05 for ANY split: the smallest attainable
# p on n discordant pairs is 2/2ⁿ, which is 0.0625 at n=5 and only crosses 0.05
# at n=6. So "p = 0.25, n = 2" is not weak evidence of no change, it is the
# only answer the test is able to give. Every surface prints the counts.
MIN_DISCORDANT_FOR_POWER = 6

# Token columns a score row must carry SOME of before it counts as cost data at
# all. A row with none of these is not "zero cost" — it is a row this module
# never priced (an older results file, a skipped trial) — see `_row_cost`.
COST_TOKEN_KEYS = ("nh_tokens", "nh_cache_tokens", "nh_cache_creation_tokens")

# `top_cost_deltas` default — never silently hides data past this: 0 means
# "show all", never "show nothing".
DEFAULT_COST_TOP = 10

# `Comparison.cost_flagged`'s default threshold, as a RATIO (cost_new /
# cost_old), not a percent delta — a spec has to move cost by at least 10% to
# be worth a human's attention beside a verdict flip.
DEFAULT_COST_FLIP_RATIO = 1.1


# --------------------------------------------------------------------------- #
# Per-spec verdicts
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SpecVerdict:
    """One spec's outcome in ONE run, reduced to a single bit for pairing.

    ``passes``/``trials`` are the raw counts over the trials that RAN, so the
    reduction is always auditable next to the bit it produced.
    """

    task_id: str
    title: str
    passes: int
    trials: int                 # trials that ran (skips excluded)
    # Both taken from the FIRST trial that ran, and both are display context
    # only — no verdict is derived from either. Under --trials the other trials
    # may carry different text; the trial counts beside them say how many.
    outcome_status: str
    notes: str
    # None when the trials that ran carry no cost data at all (see
    # `_spec_cost`) — absent, never a fabricated zero.
    cost: "SpecCost | None" = None

    @property
    def success(self) -> bool:
        """MAJORITY VOTE, ties counted as failure.

        A spec at 2/4 is not a capability, it is a coin flip, and the whole
        point of ``--trials`` was to stop reading one from the other. Rounding
        a tie UP would let a spec that flips half its trials pair as a clean
        success against a baseline that did it every time — which is the
        regression this file exists to surface.
        """
        return self.passes * 2 > self.trials

    @property
    def pass_fraction(self) -> float:
        return self.passes / self.trials if self.trials else 0.0


def _scores(run: dict[str, Any]) -> list[dict[str, Any]]:
    return list(run.get("scores") or [])


# --------------------------------------------------------------------------- #
# Per-spec cost — read, never invented
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SpecCost:
    """One spec's cost, read out of the score row(s) that ran — never derived
    from success/failure, and never a fabricated zero when the row simply
    carries no cost columns (an older results file, a hand-built fixture).

    ``priced_tokens``/``cost_ratio`` reuse ``BenchScore.nh_priced_tokens`` /
    ``.cost_ratio`` — the single canonical weighted-cost expression — via a
    throwaway ``BenchScore`` built from the row; this module does not
    re-derive pricing.
    """

    priced_tokens: float | None      # BenchScore.nh_priced_tokens, or None
    nh_tokens: int | None            # raw fresh-token count, or None
    cost_ratio: float | None         # BenchScore.cost_ratio, or None
    basis: str                       # BenchScore.cost_ratio_basis — always set
    trials_with_cost: int            # of the trials that ran, how many priced


def _row_cost(row: dict[str, Any]) -> SpecCost | None:
    """One score row's cost, or ``None`` if the row carries none of
    ``COST_TOKEN_KEYS`` at all — a row this module never priced, not a row
    that cost nothing.
    """
    if not any(row.get(k) is not None for k in COST_TOKEN_KEYS):
        return None
    score = BenchScore(
        task_id="", title="", outcome_status="", goal_satisfied=None,
        escalated_honestly=False, mergeable=None,
        nh_tokens=int(row.get("nh_tokens") or 0),
        nh_cache_tokens=int(row.get("nh_cache_tokens") or 0),
        nh_cache_creation_tokens=int(row.get("nh_cache_creation_tokens") or 0),
        nh_turns=0, nh_wall_clock_s=0.0,
        orig_tokens=0, orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=0.0, orig_corrections=0,
        nh_role_tokens=dict(row.get("nh_role_tokens") or {}),
        nh_role_models=dict(row.get("nh_role_models") or {}),
    )
    cost_ratio = row.get("cost_ratio")
    return SpecCost(
        priced_tokens=score.nh_priced_tokens,
        nh_tokens=int(row.get("nh_tokens") or 0) if row.get("nh_tokens") is not None else None,
        cost_ratio=float(cost_ratio) if cost_ratio is not None else None,
        basis=score.cost_ratio_basis,
        trials_with_cost=1,
    )


def _spec_cost(rows: list[dict[str, Any]]) -> SpecCost:
    """One spec's cost, MEAN over the trials that ran and carry cost data —
    a sum would let ``--trials N`` inflate apparent cost by N for no reason.

    Rows with no cost data at all are excluded from the mean, not counted as
    zero; if none of the rows carry cost data the result renders every field
    absent (``trials_with_cost=0``).
    """
    costed = [c for c in (_row_cost(r) for r in rows) if c is not None]
    if not costed:
        return SpecCost(priced_tokens=None, nh_tokens=None, cost_ratio=None,
                        basis="", trials_with_cost=0)
    n = len(costed)
    priced = [c.priced_tokens for c in costed if c.priced_tokens is not None]
    nh_tok = [c.nh_tokens for c in costed if c.nh_tokens is not None]
    ratios = [c.cost_ratio for c in costed if c.cost_ratio is not None]
    # basis is a closed-set label, not a number to average — the trials that
    # ran a spec use the same pricing shape, so the first costed trial's basis
    # stands for all of them.
    return SpecCost(
        priced_tokens=(sum(priced) / len(priced)) if priced else None,
        nh_tokens=(round(sum(nh_tok) / len(nh_tok))) if nh_tok else None,
        cost_ratio=(sum(ratios) / len(ratios)) if ratios else None,
        basis=costed[0].basis,
        trials_with_cost=n,
    )


# --------------------------------------------------------------------------- #
# Shape validation — refuse a drifted file, never render one
# --------------------------------------------------------------------------- #

class ResultsSchemaError(ValueError):
    """A results file whose SHAPE cannot be compared, named rather than coped
    with. Raised only by ``validate_results``; the comparison itself stays
    pure and total."""


#: Every score row must carry these KEYS. Presence, not truthiness:
#: ``goal_satisfied`` is legitimately ``None`` when the judge was skipped, and
#: ``outcome_status`` is legitimately "skipped".
REQUIRED_SCORE_KEYS = ("task_id", "outcome_status", "goal_satisfied")


def validate_results(run: Any, source: str = "<results>") -> None:
    """Refuse a results dict this module would otherwise MISREPRESENT.

    WHY THIS IS A REFUSAL AND NOT A TOLERANCE. Every default here fails in the
    confident direction, which is the worst one for a report a human reads to
    decide whether a change shipped:

      - a row with no ``outcome_status`` reads as a row that RAN (the skip test
        is an inequality), so a schema-drifted file's every spec enters the
        pairing;
      - a row with no ``goal_satisfied`` reads as FAILED (``bool(None)``), so
        those specs pair as regressions against a healthy baseline;
      - two files with no ``scores`` key at all compare cleanly: "0.0% of 0
        measured spec(s)", zero flips, p=1.0, exit 0 — a green report over no
        data whatsoever.

    None of those is detectable downstream: the wall of fake regressions looks
    exactly like a real catastrophe, and a reader has no way to tell them
    apart. So the shape is checked ONCE, at load, and a file that fails it is
    named and refused.

    BAD ROWS ARE NOT SKIPPED. Dropping them would silently shrink the corpus —
    the same "filtered slice stands for the corpus" failure the publish gate
    exists to stop — and the resulting comparison would be over a spec set
    nobody chose. One bad row condemns the file.
    """
    if not isinstance(run, dict):
        raise ResultsSchemaError(
            f"{source}: not a results card — expected a JSON object with "
            f"`scores`, got {type(run).__name__}")
    rows = run.get("scores")
    if rows is None:
        raise ResultsSchemaError(
            f"{source}: no `scores` key — this is not a bench results file. "
            f"Two such files compare to '0.0% of 0 measured spec(s)' with zero "
            f"flips and p=1.0, which is a green report over no data")
    if not isinstance(rows, list):
        raise ResultsSchemaError(
            f"{source}: `scores` is {type(rows).__name__}, not a list")
    if not rows:
        raise ResultsSchemaError(
            f"{source}: `scores` is empty — there is nothing to pair, and an "
            f"empty run compared against anything prints a clean result")
    bad: list[str] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            bad.append(f"row {i} is {type(row).__name__}, not an object")
            continue
        missing = [k for k in REQUIRED_SCORE_KEYS if k not in row]
        if missing:
            tid = row.get("task_id")
            who = f"row {i}" if not tid else f"row {i} ({tid})"
            bad.append(f"{who} lacks {', '.join(missing)}")
    if bad:
        shown = "; ".join(bad[:3])
        more = f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""
        raise ResultsSchemaError(
            f"{source}: {len(bad)} of {len(rows)} score row(s) do not carry "
            f"{', '.join(REQUIRED_SCORE_KEYS)} — {shown}{more}. A row missing "
            f"`outcome_status` counts as RAN and a row missing "
            f"`goal_satisfied` counts as FAILED, so a drifted file renders a "
            f"confident wall of regressions that reads exactly like a real "
            f"one. Bad rows are refused, never skipped: dropping them would "
            f"compare a spec set nobody chose")


def spec_verdicts(run: dict[str, Any]) -> tuple[dict[str, SpecVerdict], list[str]]:
    """``({task_id: SpecVerdict}, unmeasured_task_ids)`` for one results dict.

    A spec whose every trial was SKIPPED gets no verdict — it produced no
    measurement, and inventing one (0/0 reads as "ran and failed") is how a
    corpus that stopped resolving turns into a wall of fake regressions. It is
    returned in the second list instead, so the caller can name it.

    Single-trial and multi-trial files take the same path: a single-trial file
    is N specs with one row each, which majority-votes to that row.
    """
    rows: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for s in _scores(run):
        tid = str(s.get("task_id") or "")
        if tid not in rows:
            rows[tid] = []
            order.append(tid)
        rows[tid].append(s)

    verdicts: dict[str, SpecVerdict] = {}
    unmeasured: list[str] = []
    for tid in order:
        group = rows[tid]
        ran = [s for s in group if score_ran(s)]
        if not ran:
            unmeasured.append(tid)
            continue
        passes = sum(1 for s in ran if score_succeeded(s))
        verdicts[tid] = SpecVerdict(
            task_id=tid,
            title=str(group[0].get("title") or ""),
            passes=passes,
            trials=len(ran),
            outcome_status=str(ran[0].get("outcome_status") or ""),
            notes=str(ran[0].get("notes") or ""),
            cost=_spec_cost(ran),
        )
    return verdicts, unmeasured


def trial_flip_count(a_rows: list[dict[str, Any]],
                     b_rows: list[dict[str, Any]]) -> tuple[int, int]:
    """``(differing, paired)`` over trials matched on the ``trial`` index.

    The majority vote above answers "did this spec's VERDICT move"; this
    answers "how much did its individual trials disagree between the runs",
    which is the number that separates a spec that genuinely broke from one
    that was always a coin flip. Trials present on only one side are not
    paired, and ``paired`` says how many actually were.
    """
    a_by = {int(s.get("trial") or 0): s for s in a_rows if score_ran(s)}
    b_by = {int(s.get("trial") or 0): s for s in b_rows if score_ran(s)}
    common = sorted(set(a_by) & set(b_by))
    differing = sum(1 for t in common
                    if score_succeeded(a_by[t]) != score_succeeded(b_by[t]))
    return differing, len(common)


def _rows_by_spec(run: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for s in _scores(run):
        out.setdefault(str(s.get("task_id") or ""), []).append(s)
    return out


# --------------------------------------------------------------------------- #
# McNemar
# --------------------------------------------------------------------------- #

def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on the discordant pairs, pure stdlib.

    Under "the change did nothing", each discordant pair is an independent coin
    flip, so the count in one direction is Binomial(b+c, ½)::

        p = min(1, 2 · Σ_{i=0}^{min(b,c)} C(n, i) / 2ⁿ)      n = b + c

    The ``min(1, ·)`` is not a fudge: at b == c the doubled tail legitimately
    exceeds 1 (it double-counts the centre), and a probability above 1 is not a
    number to print. ``b + c == 0`` returns 1.0 — no spec moved, so there is no
    evidence of a change; this is the ONE case where p=1.0 is a real statement
    rather than an absence of power, and even then only about the specs paired.

    NOT the chi-square approximation with or without continuity correction: at
    the handful of discordant pairs this benchmark actually produces, the
    approximation is wrong in the anticonservative direction, and the exact
    version costs one call to ``math.comb``.
    """
    if b < 0 or c < 0:
        raise ValueError(f"discordant counts must be non-negative, got {b}, {c}")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2.0 * tail / (2 ** n))


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CostDelta:
    """One spec's cost movement between two runs — a pure reader over
    ``SpecCost``, never a scorer. ``a``/``b`` are each ``None`` only when
    NEITHER run's rows for this spec carried cost data; a spec costed on one
    side and not the other still gets a ``SpecCost`` with every field
    ``None`` from ``_spec_cost``, so ``a``/``b`` here are the per-run
    ``SpecVerdict.cost`` values as-is.
    """

    task_id: str
    title: str
    a: SpecCost | None
    b: SpecCost | None
    flipped: bool = False       # this spec's success verdict moved A→B

    @property
    def token_delta(self) -> float | None:
        """``b.priced_tokens - a.priced_tokens``, or ``None`` if either side
        lacks priced tokens."""
        if self.a is None or self.b is None:
            return None
        if self.a.priced_tokens is None or self.b.priced_tokens is None:
            return None
        return self.b.priced_tokens - self.a.priced_tokens

    @property
    def abs_token_delta(self) -> float | None:
        d = self.token_delta
        return None if d is None else abs(d)

    @property
    def cost_ratio_delta(self) -> float | None:
        """``b.cost_ratio - a.cost_ratio``, or ``None`` if either is absent."""
        if self.a is None or self.b is None:
            return None
        if self.a.cost_ratio is None or self.b.cost_ratio is None:
            return None
        return self.b.cost_ratio - self.a.cost_ratio

    @property
    def movement_ratio(self) -> float | None:
        """``b.priced_tokens / a.priced_tokens`` — how much B's cost moved
        relative to A's, as a RATIO (matching the module's ``cost_ratio``
        convention: new / old). ``None`` when either side lacks priced
        tokens, or when A's cost is exactly zero (never divide toward
        infinity)."""
        if self.a is None or self.b is None:
            return None
        if self.a.priced_tokens is None or self.b.priced_tokens is None:
            return None
        if self.a.priced_tokens == 0:
            return None
        return self.b.priced_tokens / self.a.priced_tokens

    @property
    def basis_changed(self) -> bool:
        """Whether the two sides priced this spec under different bases
        (tier-weighted vs. cache-weighted) — a caveat, not a verdict: a
        movement_ratio computed across two bases is not apples to apples."""
        if self.a is None or self.b is None:
            return False
        return bool(self.a.basis) and bool(self.b.basis) and self.a.basis != self.b.basis


@dataclass(frozen=True)
class Flip:
    """One spec whose verdict moved between the two runs."""

    task_id: str
    title: str
    direction: str              # "regressed" (A✓→B✗) or "fixed" (A✗→B✓)
    a: SpecVerdict
    b: SpecVerdict
    trial_flips: int = 0        # trials that disagreed, paired on trial index
    trials_paired: int = 0
    cost: "CostDelta | None" = None


@dataclass
class Comparison:
    label_a: str = ""
    label_b: str = ""
    created_a: str = ""
    created_b: str = ""
    # Verdict-level rates over each run's OWN measured specs — the number the
    # per-spec pairing below is built from, and deliberately NOT the published
    # headline (see `headline_caveat`).
    rate_a: float = 0.0
    rate_b: float = 0.0
    specs_a: int = 0
    specs_b: int = 0
    # 2x2 paired table. `both_pass`/`both_fail` are concordant; b and c are the
    # only cells McNemar looks at, and the only cells a change can move.
    both_pass: int = 0
    both_fail: int = 0
    b_regressed: int = 0        # A pass → B fail
    c_fixed: int = 0            # A fail → B pass
    regressions: list[Flip] = field(default_factory=list)
    fixes: list[Flip] = field(default_factory=list)
    # Never silently dropped: named, not just counted.
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    unmeasured_a: list[str] = field(default_factory=list)
    unmeasured_b: list[str] = field(default_factory=list)
    p_value: float = 1.0
    # Every PAIRED spec's cost movement (not just the flips) — sorted by
    # `abs_token_delta` descending (None deltas last), task_id as tiebreak, so
    # `top_cost_deltas` needs no further sorting of its own.
    cost_deltas: list[CostDelta] = field(default_factory=list)

    @property
    def paired(self) -> int:
        return self.both_pass + self.both_fail + self.b_regressed + self.c_fixed

    @property
    def discordant(self) -> int:
        return self.b_regressed + self.c_fixed

    @property
    def has_power(self) -> bool:
        return self.discordant >= MIN_DISCORDANT_FOR_POWER

    @property
    def unpaired(self) -> int:
        """DISTINCT specs that could not be paired — the honest denominator gap.
        A run compared against a baseline it shares half a corpus with is not a
        paired comparison, and this is the number that says so.

        A set union, not a sum of the four lists: a spec measured in A and
        skipped in B is BOTH ``only_in_a`` and ``unmeasured_b``, and summing
        the lists reports one spec as two. The four lists are each true and
        each printed — they answer "which specs, and why" — but the count of
        the gap has to be over specs.
        """
        return len(set(self.only_in_a) | set(self.only_in_b)
                   | set(self.unmeasured_a) | set(self.unmeasured_b))

    @property
    def aggregate_token_delta(self) -> float | None:
        """SUM of every paired spec's ``token_delta`` — not an average or
        median: a regression that doubled one expensive spec's cost is not
        diluted by nine unrelated specs that did not move. ``None`` only when
        NO paired spec carries a token_delta at all (nothing to sum)."""
        deltas = [d.token_delta for d in self.cost_deltas if d.token_delta is not None]
        return sum(deltas) if deltas else None

    @property
    def specs_costed(self) -> int:
        """Paired specs where BOTH sides carry a token_delta."""
        return sum(1 for d in self.cost_deltas if d.token_delta is not None)

    @property
    def specs_missing_cost(self) -> int:
        """Paired specs where at least one side carries no cost data at
        all — named, not silently folded into a delta of zero."""
        return len(self.cost_deltas) - self.specs_costed

    def cost_flagged(self, threshold: float = DEFAULT_COST_FLIP_RATIO) -> list[CostDelta]:
        """Specs that FLIPPED success while their cost moved by at least
        ``threshold`` in either direction (``movement_ratio >= threshold`` or
        ``movement_ratio <= 1/threshold``) — the case a bare success/fail
        pairing cannot see: a spec that flipped for free vs. one that flipped
        and got 3x more expensive read identically as "1 regression"."""
        out = []
        for d in self.cost_deltas:
            if not d.flipped:
                continue
            m = d.movement_ratio
            if m is None:
                continue
            if m >= threshold or m <= 1.0 / threshold:
                out.append(d)
        return out

    def top_cost_deltas(self, n: int = DEFAULT_COST_TOP) -> list[CostDelta]:
        """The first ``n`` of ``cost_deltas`` (already sorted by
        ``abs_token_delta`` descending). ``n <= 0`` returns every entry —
        never silently hides data."""
        return list(self.cost_deltas) if n <= 0 else self.cost_deltas[:n]


def compare_runs(run_a: dict[str, Any], run_b: dict[str, Any]) -> Comparison:
    """Pair two loaded results dicts spec by spec. Pure — reads no files.

    ``run_a`` is the BASELINE and ``run_b`` the run under test, so "regressed"
    means A passed and B failed. Passing them the other way round is not an
    error the data can detect, which is why the CLI prints both labels and
    creation dates above the table.
    """
    va, unmeasured_a = spec_verdicts(run_a)
    vb, unmeasured_b = spec_verdicts(run_b)
    rows_a, rows_b = _rows_by_spec(run_a), _rows_by_spec(run_b)

    out = Comparison(
        label_a=str(run_a.get("label") or ""),
        label_b=str(run_b.get("label") or ""),
        created_a=str(run_a.get("created_at") or ""),
        created_b=str(run_b.get("created_at") or ""),
        specs_a=len(va), specs_b=len(vb),
        rate_a=(sum(1 for v in va.values() if v.success) / len(va)) if va else 0.0,
        rate_b=(sum(1 for v in vb.values() if v.success) / len(vb)) if vb else 0.0,
        only_in_a=sorted(set(va) - set(vb)),
        only_in_b=sorted(set(vb) - set(va)),
        unmeasured_a=sorted(unmeasured_a),
        unmeasured_b=sorted(unmeasured_b),
    )

    cost_deltas: list[CostDelta] = []
    for tid in sorted(set(va) & set(vb)):
        a, b = va[tid], vb[tid]
        flipped = a.success != b.success
        cost_deltas.append(CostDelta(task_id=tid, title=a.title or b.title,
                                     a=a.cost, b=b.cost, flipped=flipped))
        if a.success and b.success:
            out.both_pass += 1
            continue
        if not a.success and not b.success:
            out.both_fail += 1
            continue
        flips, paired = trial_flip_count(rows_a.get(tid, []), rows_b.get(tid, []))
        flip = Flip(task_id=tid, title=a.title or b.title,
                    direction="regressed" if a.success else "fixed",
                    a=a, b=b, trial_flips=flips, trials_paired=paired,
                    cost=cost_deltas[-1])
        if a.success:
            out.b_regressed += 1
            out.regressions.append(flip)
        else:
            out.c_fixed += 1
            out.fixes.append(flip)

    # Sorted by absolute token delta descending — entries with no delta at
    # all (either side missing cost data) sort last, never hidden, just not
    # competing for the top of a cost-ordered list they carry no number for.
    out.cost_deltas = sorted(
        cost_deltas,
        key=lambda d: (d.abs_token_delta is None,
                       -(d.abs_token_delta or 0.0), d.task_id))
    out.p_value = mcnemar_exact_p(out.b_regressed, out.c_fixed)
    return out


def headline_caveat() -> str:
    """Why the two rates above the table are not the published headline.

    ``northstar_card.spec_mean_success_rate`` averages per-spec pass FRACTIONS
    (a 2/3 spec contributes 0.667); pairing needs one bit per spec, so the
    rates here majority-vote first (that same spec contributes 1). The two
    agree exactly on single-trial runs and can differ by a few points on
    multi-trial ones. Stating it is cheaper than having someone discover that
    two of our own numbers disagree.
    """
    return ("rates above are MAJORITY-VOTE per spec, over each run's own "
            "measured specs — not the published headline (mean of per-spec "
            "pass fractions); they coincide on single-trial runs")


def cost_caveat() -> str:
    """Why the cost section can be silent, or partial, for a real run.

    Cost is read from ``nh_tokens``/``nh_cache_tokens``/
    ``nh_cache_creation_tokens`` on the score rows that RAN; a spec whose rows
    carry none of them contributes no delta, not a zero delta — an absence
    this module refuses to invent, same as an unmeasured spec's verdict.
    """
    return ("cost deltas are read straight off each run's score rows — a "
            "spec whose rows carry no token columns at all contributes no "
            "delta, never a fabricated zero; specs missing cost on either "
            "side are counted, not silently dropped")


def interpretation(cmp: Comparison) -> str:
    """One honest sentence about what the p-value can and cannot support."""
    n = cmp.discordant
    if n == 0:
        return ("no spec flipped in either direction, so there is nothing for "
                "a test to weigh — p=1.0 here means 'no movement among the "
                f"{cmp.paired} paired spec(s)', not 'the change is safe'")
    if not cmp.has_power:
        return (f"{n} discordant pair(s) — the exact test CANNOT reach p<0.05 "
                f"below {MIN_DISCORDANT_FOR_POWER} (its smallest possible "
                f"two-sided p on n pairs is 2/2^n = "
                f"{2.0 / (2 ** n):.4f} here), so read the flips and the "
                f"counts, not the p")
    return (f"{n} discordant pair(s) — enough for the test to be able to "
            f"reach significance; read it alongside the flip list, which says "
            f"WHICH specs moved")


# --------------------------------------------------------------------------- #
# Flaky canary — repetition, not isolation, decides
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CanarySpec:
    task_id: str
    title: str
    flips: int                  # verdict changes across consecutive run-pairs
    pairs: int                  # consecutive run-pairs the spec appeared in
    history: list[str] = field(default_factory=list)   # "✓"/"✗"/"—" per run


# One flip is a result; two is a pattern. A spec that flips on a single
# run-pair is exactly what `compare_runs` already prints as a regression or a
# fix, and calling that flaky would relabel every real change as noise.
MIN_CANARY_FLIPS = 2


def undated_run_indices(runs: list[dict[str, Any]]) -> list[int]:
    """Positions of runs carrying no ``created_at``, in the order supplied.

    The canary's whole claim is "consecutive observations", and a run with no
    date cannot be placed among them. It is not dropped — it is ORDERED LAST
    and it is NAMED, so a caller can print the caveat instead of asserting an
    ordering it does not have.
    """
    return [i for i, r in enumerate(runs) if not str(r.get("created_at") or "")]


def order_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological by ``created_at``, with UNDATED runs LAST.

    THE BUG THIS FIXES, found by review: the key used to be
    ``str(r.get("created_at") or "")``, and the empty string sorts BEFORE every
    real timestamp — so an undated run was silently promoted to the FRONT of
    the history, whatever position the caller supplied it in. That is not a
    cosmetic ordering difference: the canary reads consecutive PAIRS, so moving
    one run to the other end of the chain changes which specs are adjacent and
    therefore which specs flip. The reviewer demonstrated the same four runs
    flipping between flagged and not-flagged purely on whether the fourth run
    carried a date. The old docstring claimed undated runs "keep their given
    order", which was true of neither the code nor any caller's expectation.

    Undated runs now sort after every dated one and keep their supplied order
    among themselves (the index is the final tiebreak, so this is a stable
    order without relying on a stable sort's incidental behaviour). ``last`` is
    the honest position for a run whose date is unknown: it makes no claim
    about a run it cannot place, and the caller names it in the output.
    """
    return [r for _, r in sorted(
        enumerate(runs),
        key=lambda ir: ((0, str(ir[1].get("created_at")), ir[0])
                        if str(ir[1].get("created_at") or "")
                        else (1, "", ir[0])))]


def flaky_canary(runs: list[dict[str, Any]],
                 min_flips: int = MIN_CANARY_FLIPS) -> list[CanarySpec]:
    """Specs whose verdict flipped across ≥ ``min_flips`` consecutive run-pairs.

    Takes the run history ORDERED BY ``created_at`` — and re-orders by it here
    anyway, through ``order_runs``, because a caller that globs a directory
    gets filesystem order and a canary computed over shuffled history is noise
    measuring itself. A run with NO ``created_at`` cannot be placed in a
    chronology, so it sorts LAST rather than first, keeping its supplied order
    among other undated runs; ``undated_run_indices`` names them so a caller
    can say so in its output instead of asserting an order it does not have.

    A spec ABSENT from a run (or unmeasured in it) breaks the chain rather than
    counting as a failure: the dominant absence here is a repo that did not
    resolve, and scoring that as a flip would flag the whole corpus every time
    a checkout moved. Its neighbours are not bridged across the gap either —
    two runs either side of an absence are not consecutive observations.

    Fewer than two runs returns [] — there is no pair to flip across.
    """
    if len(runs) < 2:
        return []
    per_run = [spec_verdicts(r)[0] for r in order_runs(runs)]

    every_id: list[str] = []
    seen: set[str] = set()
    for verdicts in per_run:
        for tid in verdicts:
            if tid not in seen:
                seen.add(tid)
                every_id.append(tid)

    out: list[CanarySpec] = []
    for tid in sorted(every_id):
        flips = pairs = 0
        for prev, cur in zip(per_run, per_run[1:]):
            if tid not in prev or tid not in cur:
                continue
            pairs += 1
            if prev[tid].success != cur[tid].success:
                flips += 1
        if flips < min_flips:
            continue
        title = next((v[tid].title for v in per_run if tid in v), "")
        history = ["—" if tid not in v else ("✓" if v[tid].success else "✗")
                   for v in per_run]
        out.append(CanarySpec(task_id=tid, title=title, flips=flips,
                              pairs=pairs, history=history))
    return sorted(out, key=lambda c: (-c.flips, c.task_id))
