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

from .northstar_card import score_ran, score_succeeded

# Below this many discordant pairs, McNemar's exact two-sided test CANNOT
# produce a significant result at α=0.05 for ANY split: the smallest attainable
# p on n discordant pairs is 2/2ⁿ, which is 0.0625 at n=5 and only crosses 0.05
# at n=6. So "p = 0.25, n = 2" is not weak evidence of no change, it is the
# only answer the test is able to give. Every surface prints the counts.
MIN_DISCORDANT_FOR_POWER = 6


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
class Flip:
    """One spec whose verdict moved between the two runs."""

    task_id: str
    title: str
    direction: str              # "regressed" (A✓→B✗) or "fixed" (A✗→B✓)
    a: SpecVerdict
    b: SpecVerdict
    trial_flips: int = 0        # trials that disagreed, paired on trial index
    trials_paired: int = 0


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

    for tid in sorted(set(va) & set(vb)):
        a, b = va[tid], vb[tid]
        if a.success and b.success:
            out.both_pass += 1
            continue
        if not a.success and not b.success:
            out.both_fail += 1
            continue
        flips, paired = trial_flip_count(rows_a.get(tid, []), rows_b.get(tid, []))
        flip = Flip(task_id=tid, title=a.title or b.title,
                    direction="regressed" if a.success else "fixed",
                    a=a, b=b, trial_flips=flips, trials_paired=paired)
        if a.success:
            out.b_regressed += 1
            out.regressions.append(flip)
        else:
            out.c_fixed += 1
            out.fixes.append(flip)

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


def flaky_canary(runs: list[dict[str, Any]],
                 min_flips: int = MIN_CANARY_FLIPS) -> list[CanarySpec]:
    """Specs whose verdict flipped across ≥ ``min_flips`` consecutive run-pairs.

    Takes the run history ORDERED BY ``created_at`` — and sorts by it here
    anyway, stably, because a caller that globs a directory gets filesystem
    order and a canary computed over shuffled history is noise measuring
    itself. Runs with no ``created_at`` keep their given order (a stable sort
    leaves equal keys alone).

    A spec ABSENT from a run (or unmeasured in it) breaks the chain rather than
    counting as a failure: the dominant absence here is a repo that did not
    resolve, and scoring that as a flip would flag the whole corpus every time
    a checkout moved. Its neighbours are not bridged across the gap either —
    two runs either side of an absence are not consecutive observations.

    Fewer than two runs returns [] — there is no pair to flip across.
    """
    if len(runs) < 2:
        return []
    ordered = sorted(runs, key=lambda r: str(r.get("created_at") or ""))
    per_run = [spec_verdicts(r)[0] for r in ordered]

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
