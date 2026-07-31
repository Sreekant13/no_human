# Reviewer-recall corpus — HELD OUT FROM ALL TUNING

Method: `docs/REVIEWER_RECALL_METHOD.md`. Read it before touching anything here.

Each `cases/<id>/` holds `base.ref` (provenance only — the SHA the case was cut
from), `base/` (the base file content the diff applies to, materialised inside
the case), `change.diff` (what the reviewer sees — for seeded cases the defect
is planted, unmarked), `truth.json` (ground truth — never shown to the
reviewer), and `base.manifest` (the git blob id of each `base/` file — a
byte-identity pin that outlives the history).

`base.manifest` lines are `<blob id>  <path>`, with an optional third column
`<origin blob id>`. Column 1 always hashes the bytes **on disk**. The third
column appears only on a file the materialiser de-identified after extracting
it, and holds the blob id at `base.ref`, so provenance stays pinned while the
fixture's bytes deliberately differ from it. See "De-identification" below.

A case directory must hold all of `base.ref`, `change.diff` and `truth.json`.
`load_cases` skips a directory with **none** of them (`__pycache__` and the
like) and **raises** on one with only some: quietly dropping a half-built case
would move the denominator with nothing to say so, and would slip past
`HeadlineRefusedError`, which can only refuse over cases that became results.

## The corpus is self-contained (2026-07-30)

`prepare_case_repo` used to rebuild each case with `git archive <base.ref>`.
That pinned the corpus to this repo's history — and no_human ships as a fresh
`git init` with a single commit, so every case would have ERRORed at
`git archive` in the published repo. The base content now lives in
`cases/<id>/base/` and **`base.ref` is never resolved**. Regenerate it for a new
case with `python eval/reviewer_recall/materialize_base.py <case-id>`, run from
a checkout that still has the commit.

Only the files a case's `change.diff` touches are materialised (70 files,
~4.7 MB across all 20 cases), each pinned by blob id in `base.manifest`. That
side effect is worth naming: the old full-tree checkout also shipped every
OTHER case's `truth.json` into the reviewer's scratch repo — up to 16 of them,
in 10 of the 20 cases. It carries none now. That is the full set the scratch repo is read
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
a clean pass.

That is recorded, not just warned about. `ReviewOutcome.demoted_citations`
carries `ReviewDecision.demoted_citations` through scoring; a control that
passed with demotions is marked `clean_pass_relied_on_demotion`, its `reason`
names them, `render_report` prints a ⚠ line under the specificity number
listing each one, and every `runs/<date>/<case>.json` transcript carries both
fields. With 4 controls one flip is 25 points and published specificity has sat
at 2/4 and 0/4, so a caveat nobody can act on is not good enough: read those
lines before quoting a specificity number.

## De-identification (2026-07-31)

A base fixture is cut from a **pre-sweep** commit — that is what makes it a base
— so it can carry vendor/employer terms that the tree-wide de-identification
sweep has since removed from live source. `eval/reviewer_recall/` is classified
`ship` in `EXPORT_CLASSIFICATION.txt`: this corpus is published, so such a term is a public leak.

That happened. On 2026-07-30 one branch swept a term out of live source and
another materialised base fixtures from pre-sweep commits; merged in that order,
`main` went red with 16 hits on exportable paths, plus 6 more that an inherited
`# term-ok` comment had been hiding. Twelve files across eleven cases.

The fix is in the materialiser, not in anyone's memory: `materialize_base.py`
pipes every blob through `scrub()` on the way to disk, using the substitution
list in the private supplement (absent in the export, where this script cannot
run anyway). Re-running it is therefore safe and idempotent. The replacements
are the ones the live sweep chose for the same identifiers, so a fixture still
reads as a realistic file.

**What this must never do is change what the corpus measures.** Verified for
this pass: 22 lines rewritten, every one of them a vendor term inside a comment
or a string, and every one outside every pre-image hunk range in that case's
`change.diff` — so no context line moved and all 20 diffs still apply.
`truth.json` and `change.diff` are untouched in all 20 cases.
`test_prepared_case_repo_matches_the_pinned_base_content` asserts both
directions of the manifest's third column, so neither an undeclared edit to a
fixture nor a stale `scrubbed-from` declaration can pass.

Rules that keep the number honest:

- **Never** use these diffs, truths, or their lessons to tune the reviewer
  prompt, few-shot examples, or intake. A case that motivates a change is
  retired to `burned/` and leaves the denominator (see the method doc).
- **Never** mix these into the north-star corpus, or vice versa.
- The review checkout for a case must not contain history descending from
  `base.ref`, or git archaeology finds the plant. It no longer contains any
  history at all: the scratch repo is seeded from `base/` and gets exactly one
  commit, so there is no ref, remote, ancestor or descendant to dig through.
- Every seeded case's `change.diff` must keep the diff's own tests green —
  a plant the test suite catches measures pytest, not review skill. Verify
  before adding a case.
- Controls (`class: control`) are real merged diffs, unmodified; specificity
  over them is reported alongside recall, always.

Current denominator: 16 seeded (4 logic, 4 security, 4 test-tamper,
4 spec-miss) + 4 controls = 20 cases. Target per the method doc: 16–20 seeded,
≥4 controls, ≥3 per class — met. (This line said "12 seeded (3/3/3/3)" until
2026-07-30; it was stale, not a retirement. `load_cases` reports 20 and
`render_report` breaks out 4/4/4/4.)
