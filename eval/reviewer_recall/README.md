# Reviewer-recall corpus — HELD OUT FROM ALL TUNING

Method: `docs/REVIEWER_RECALL_METHOD.md`. Read it before touching anything here.

Each `cases/<id>/` holds `base.ref` (SHA in this repo), `change.diff` (what the
reviewer sees — for seeded cases the defect is planted, unmarked), and
`truth.json` (ground truth — never shown to the reviewer).

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

Current denominator: 8 seeded (2 logic, 2 security, 2 test-tamper,
2 spec-miss) + 2 controls. Target per the method doc: 16–20 seeded, ≥4
controls, ≥3 per class.
