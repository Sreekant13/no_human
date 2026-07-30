# Reviewer-recall corpus — HELD OUT FROM ALL TUNING

Method: `docs/REVIEWER_RECALL_METHOD.md`. Read it before touching anything here.

Each `cases/<id>/` holds `base.ref` (provenance only — the SHA the case was cut
from), `base/` (the base file content the diff applies to, materialised inside
the case), `change.diff` (what the reviewer sees — for seeded cases the defect
is planted, unmarked), and `truth.json` (ground truth — never shown to the
reviewer).

## The corpus is self-contained (2026-07-30)

`prepare_case_repo` used to rebuild each case with `git archive <base.ref>`.
That pinned the corpus to this repo's history — and no_human ships as a fresh
`git init` with a single commit, so every case would have ERRORed at
`git archive` in the published repo. The base content now lives in
`cases/<id>/base/` and **`base.ref` is never resolved**. Regenerate it for a new
case with `python eval/reviewer_recall/materialize_base.py <case-id>`, run from
a checkout that still has the commit.

Only the files a case's `change.diff` touches are materialised (70 files,
~4.7 MB across all 20 cases). That is the full set the scratch repo is read
for: the runner invokes the reviewer with `diff_override`, which puts it on the
single-turn, no-tools path (`review/reviewer.py`, gate mode) — it never
explores the tree, it only gets the diff. The one place the tree is read is
`_verify_citations`, which demotes a blocking finding whose cited `file:line`
does not exist.

**Behavioural delta, stated plainly:** under `git archive` the whole base tree
was present, so a blocking finding citing a file *outside* the diff still
verified and stayed blocking. It now demotes to advisory, because that file is
not materialised. This can only affect a finding that names a file the reviewer
was never shown — with only the diff in its prompt, such a citation is a
hallucinated location. It cannot change a seeded case's `caught` (the planted
file is always in the diff); it can only make a control *more* likely to score
a clean pass. If a run shows a control passing on demoted off-diff citations,
say so rather than banking the number.

Rules that keep the number honest:

- **Never** use these diffs, truths, or their lessons to tune the reviewer
  prompt, few-shot examples, or intake. A case that motivates a change is
  retired to `burned/` and leaves the denominator (see the method doc).
- **Never** mix these into the north-star corpus, or vice versa.
- The review checkout for a case must not contain history descending from
  `base.ref` (shallow/filtered), or git archaeology finds the plant.
- Every seeded case's `change.diff` must keep the diff's own tests green —
  a plant the test suite catches measures pytest, not review skill. Verify
  before adding a case.
- Controls (`class: control`) are real merged diffs, unmodified; specificity
  over them is reported alongside recall, always.

Current denominator: 12 seeded (3 logic, 3 security, 3 test-tamper,
3 spec-miss) + 4 controls. Target per the method doc: 16–20 seeded, ≥4
controls, ≥3 per class.
