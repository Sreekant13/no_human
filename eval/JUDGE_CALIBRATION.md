# Goal-judge calibration — what has and has not been done

The north-star bench scores "did the agent satisfy the request?" with an LLM
judge (`GoalJudge`, `src/no_human/eval/judge.py`), not with a human. Every
headline number in `docs/NORTH_STAR_BENCH.md` inherits that judge's mistakes.
This file records what the judge has actually been checked against, so nobody
has to reconstruct it from memory — and, more importantly, so the gap between
what was checked and what a calibration would need stays visible.

**Bottom line, stated first: the judge has NOT been calibrated against a human.
One model evidence-audit exists. It is not a substitute, and the target below
is still open.**

## The target this is measured against

Cohen's κ between the judge's binary verdict and an independent human label,
over the specs a run actually judged:

    κ = (p_o − p_e) / (1 − p_e)

with `p_o` observed agreement and `p_e` chance agreement from each rater's
marginal satisfied / not-satisfied rate. "Can't tell" rows are excluded and
recorded separately as unscoreable — never counted as either agreement or
disagreement. **κ ≥ 0.8** before the judge's verdict is treated as a scoring
oracle for bench-v2. The labeling sheet the human fills lives with the
operator, outside this repo; its human slots are empty.

## What was done — model evidence-audit, 2026-08-03

Run `opus5-2026-07-26-post12merges` (54 specs, created 2026-07-26). Of the 54,
**21 were judge-scored** — reached the human gate, were not an
`expected_escalation` / skipped / crashed spec, and got a verdict. The other 33
are auto-scored or gate-missed and are out of scope for κ.

Protocol, two passes:

1. **Blind pass** over a rebuilt sheet with every judge verdict and every piece
   of judge evidence stripped out — request text and outcome status only.
2. **Evidence audit** over the full sheet, asking of each verdict whether the
   evidence it cites actually supports it. Final labels come from this pass.

One spec (`ns-01c3d46d`) was un-blinded during a format check and is excluded,
leaving 20 included pairs.

| | |
|---|---|
| included pairs | 20 (18 satisfied, 2 not-satisfied — on both sides) |
| observed agreement `p_o` | 1.00 (20/20) |
| chance agreement `p_e` | 0.82 |
| **κ (audit)** | **1.00** |
| blind-pass-only subset | 7/7 agree (7 cases callable without evidence) |

### Why κ = 1.00 is not the good news it looks like

The audit labels were formed **partly from the judge's own cited evidence**, so
agreement is inflated by construction. A rater reading the defendant's own
account of events is not an independent rater. What the audit does establish,
and it is worth having:

* every one of the 21 verdicts is backed by evidence that is specific,
  internally coherent, and — where the citation was checkable from the sheet —
  accurate;
* the judge issues not-satisfied verdicts when they are warranted (2 of 21),
  including against honest-refusal behavior, so it is not a rubber stamp.

### The limitation that keeps the target open

The rater **shares a model family with the judge** (both Opus tier). This is a
model evidence-audit, not the human calibration the κ ≥ 0.8 gate calls for. It
can clear the judge of internal incoherence. It cannot clear it of a blind spot
the two share — which is exactly the failure mode a same-family rater is worst
placed to see. The human label slots remain open and the gate remains unmet.

## The one finding worth acting on

**The judge applies two different standards to specs whose inputs do not exist
in the bench environment.**

* `ns-01c3d46d` — the handover file named by the request is absent. Judged
  **satisfied**; an empty diff was credited as the correct deliverable for an
  investigation that found nothing to fix.
* `ns-f5cb4cb0` — the two review targets named by the request are on hosts that
  do not resolve. Judged **not satisfied**; the refusal was called correct
  behavior, but the requested deliverable did not exist.

Each verdict is defensible on its own. Together they are two standards for one
situation, and which one a spec gets is not something the judge is told how to
decide. **Bench-v2 V4 must either give the judge an explicit missing-input rule
or reclassify such specs `expected_escalation`** — where a stop is scored as
the right outcome by construction, and the judge is not asked to invent the
policy per spec.

## Status

| | |
|---|---|
| model evidence-audit | done, 2026-08-03, κ_audit = 1.00 over 20 pairs |
| human calibration (κ ≥ 0.8) | **not done** — sheet with the operator, slots empty |
| missing-input rule for the judge | done — explicit policy paragraph in `build_goal_prompt`, `src/no_human/eval/judge.py` (pinned by `tests/test_northstar.py::test_goal_prompt_states_the_missing_input_rule`) |

Until the middle row changes, treat "goal satisfied" in any published bench
report as one model's evidence-backed opinion, audited by another model of the
same family, and not as ground truth.
