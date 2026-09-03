# The pull request no_human opens

Every PR no_human opens has the same shape. It is generated from a template
the coder cannot author a top-level section in — its own headings are
demoted below the template's, and every model-written cell is neutralised
before it is interpolated. The shape is deliberately short: a reviewer meets
the gate results first, and most reference-grade detail is folded behind a
`<details>` disclosure — still in the body, one click away, never dropped.
The one exception is the raw command log ("How I verified this" below): it
does not fold into the body at all any more — it moves to this attempt's own
artifact file and a PR comment, and the body keeps only a pointer to it and
the recorded-command count; each test layer's own final result still renders
exactly once, in the **Evidence** table above.

## What is in it, top to bottom

**Ticket** — the tracker issue this PR answers, linked when intake recorded
its URL, plain text otherwise. Absent when the task has no ticket.

**Evidence** — one table of mechanical facts, gathered once from the
attempt's gate outputs:

| Check | What the cell is |
|---|---|
| Independent review | the fresh-context reviewer's verdict on *this* commit and the number of rounds it took. A round that judged a different commit of the task is not counted. Findings earlier rounds raised, and the coder addressed, are folded under the table. |
| Verifiers | present only when at least one project verifier rule (`.no_human/verifiers.yaml`) was selected for this commit's changed files — `"N of N satisfied"` or `"K of N failed — id1, id2"` (`core/pr_evidence.py`'s `verifiers_pin()`), never re-derived from the per-rule list. Every rule's verdict, not only the failures, is folded under the table: `✅ id — n files` for a pass, `❌ id — file:line — comment` for a fail. A failing verifier blocked the reviewer from running at all this round — see [verification.md](verification.md#verifiers--a-recorded-verdict-per-rule). |
| Test-change guard | present only when the tamper guard fired and an independent adjudicator waived it as required by the ticket; its reasoning is printed under the table. |
| Tests | the orchestrator's own run of the project's tests on the final tree — counts, or `NOT RUN` with the reason. Failing test names are folded under the table. |
| CI | the forge's CI state for the branch, when one is known. |
| Merge policy | the repo's merge-ready policy verdict for *this* commit — ✅ or ❌ plus the policy's own summary sentence, computed once by `core/merge_policy.py` and rendered nowhere else. `⚠️` replaces `✅`/`❌` when this diff itself edited `.no_human/merge_policy.yaml` (a coder cannot author its own merge gate — this also forces the verdict to `ready: false`) or when the policy file failed to load (broken/oversized, falling back to the default); either case prints a visible warning line *above* the fold, never only inside it. Every rule's name, pass/fail, and detail is folded under the table. **This row is advisory to the human; nothing in this repo merges on it** — see [verification.md](verification.md#merge-ready-policy). |

Nothing in this table is written by the coder. A sentence can appear here
only if the evidence object that backs it exists — a test pins that.

**Acceptance criteria** — the task's own, verbatim.

**Changes** — the coder's final report. Its mandated `CRITERION: … — MET —
evidence: …` lines are rendered as a compact list with the verdict first; a
`NOT-MET` line is always visible — a paragraph carrying one is moved to the
top of the section, ahead of the coder's own order. Whole paragraphs are kept
visible up to 1,500 characters and the remainder is folded, so a long report
is delivered whole without burying the evidence above it. Coder-to-harness dialogue is filtered out and the
removal is marked. If everything else in the body (Evidence, the verification
pointer, the footer — never Acceptance criteria, which is the task's own
text) would still put the body over a hard 6,000-visible-character budget
even after that ordinary fold, **Changes is trimmed further**, with an
explicit `(trimmed further to keep the PR body under its size budget)`
marker — it is the only section this budget ever shrinks.

**Assumptions** — folded behind a one-line count: the questions the intake
step answered on the requester's behalf, the assumptions it recorded, and the
original wording of any criterion it sharpened. An unresolved blocker or an
open question is printed *above* the fold, as a callout.

**How I verified this** — a pointer, not a log (2026-08-31: the operator's
"receipts out of the PR body" directive). The full command log — every
command line a hook saw the coder's session submit to the shell and the text
the harness returned; the model does not author an entry and cannot edit
one — no longer lives in the body. It is written, in full, to this ATTEMPT's
own artifact file (`~/.no_human/artifacts/<task-id>/verification-attempt-
<n>.md` — attempt-scoped, so a later attempt's write can never overwrite the
file an earlier, still-open PR body points a reader at) and posted, in full,
as its own PR comment (below); the body carries one line naming that file —
rendered `~`-relative, never as an absolute path that would leak the
operator's local account name — plus `nh logs <task-id>`, which prints that
same path and tails the file, and the recorded-command count. Per-layer test
results are not repeated here: they render exactly once, in the **Evidence**
table above. The log itself is still capped the same way it always was (40
entries listed, 12 with output, 1,200 characters per excerpt, 200 receipts
per attempt) and still says so when a cap bit, and it still ends with six
sentences on what it cannot attest — the full list is below; only its
LOCATION moved.

**UI evidence** — present only on a task whose diff touched `web/`/`desktop/`
(or a repo-declared `ui_evidence.ui_paths` glob) AND left a coder-authored
`.no_human/ui_evidence.json` walk. After tests pass, the harness (never the
coder) drives a real headless browser through that walk and, when it captured
at least one screenshot, delivers everything to a `nh-evidence/<task-id>`
SIDE branch — never the task branch itself (`git merge --squash` would carry
an unclassified directory on the task branch straight into main). On a
GitHub remote, up to 6 screenshots are embedded inline via that branch's raw
URLs, plus one video link; on any other remote the branch is still pushed
(so a human can look directly) but nothing embeds — building a raw-content
URL for GitLab or a GitHub Enterprise host is out of scope today. This
section is exempt from the 6,000-visible-character body budget above: it is
capped on its own terms (≤6 shots + 1 video link), not folded against
`## Changes`. It also names which server it walked: "Dev server booted by
the harness for this walk (`{start_cmd}`), stopped afterwards." when the
harness started it itself, or "Dev server was already running at {base_url}
before the walk; the harness did not start it and did not verify which
checkout it serves." when something answered there beforehand.

**Footer** — attempt number, branch pair, and the standing rule: no_human
never merges. A human reviews and merges, or runs `nh approve <task>`.

## The review checklist comment

The PR body's **Evidence** table carries one row for the independent
review — a verdict and a round count. The full checklist behind that row
(every finding the fresh-context reviewer recorded on the delivered commit,
each with its severity and `file:line`) is posted once as its own PR
comment, right after the "How I verified this" comment, marked with
`<!-- no_human:review-checklist -->` so a second run never duplicates it.
Blocking and failed findings are listed first, then passed checks, then
advisory (`low`/`nit` — never blocking) findings folded behind a
`<details>` disclosure. Every model-authored cell — label, severity, file,
note — goes through the same neutralising pass as the rest of this
document before it is interpolated, so a finding cannot render a live
heading or break out of its table row. Controlled by
`review.post_checklist_comment` (default on) — see
[configuration.md](configuration.md). Like every other PR comment here,
posting never blocks delivery: a forge error or a duplicate is logged and
the PR stands regardless.

## Why there is no PASS/FAIL badge on the command log

Deciding whether an exit status belongs to the program that was checked
means parsing bash — `pytest -q | tail -3` exits with `tail`'s status — and
six independent reviews found a way past every attempt to do it. Measured
over 292 real receipts, a badge could have justified a PASS for 6 of them
(2.1%) and would have read UNKNOWN on the rest — not worth the trust a badge
invites. So the log shows what ran and what came back, and the table
above it carries the verdicts no_human *can* establish: the reviewer's and
its own test run's. See `agent/verification_receipts.py`.

This is now the artifact file's rationale, not the body's: the body never
shows a command's raw output at all any more, so there is nothing in it a
badge could be attached to. The reasoning still applies in full to the
artifact file (`~/.no_human/artifacts/<task-id>/verification-attempt-<n>.md`)
and to the PR comment, which carry the log this section describes.

## What the command log cannot attest — the full list

The artifact file and the PR comment — never the body any more — carry
these as six merged sentences. They were sixteen, and the sixteen are kept
here verbatim so nothing the shorter form folds together is lost:

1. no interactive UI check was performed. no_human never drives a browser at your change except `testing/ui_evidence.py`'s walk, which is reported as its own evidence and not as a receipt in this log; the only other page it drives is a CI server's login form, and the board it opens without driving, so any `e2e` entry above is the project's harness printing its result, not a human-style walkthrough

2. an entry shows that a command LINE was submitted to the shell and what came back - never that the check recognised inside it RAN, and never that it was the RIGHT command. `pytest -k test_nothing` selects no tests and prints a clean run; a type check over one file says nothing about the rest

3. the text is the coder's. The session chose the command string, and through `echo`/`printf` it can choose the output too. Both are shown as inert text: what is attested is that this command line was submitted to the shell and that this is what came back, not that any of it is true

4. no entry ASSERTS a pass, a fail, or an exit status, and that is deliberate. Deciding whether a zero exit belongs to the checked program means parsing bash - `pytest -q | tail -3` exits with `tail`'s status - and six independent reviews found a new way past every attempt. Where the captured text below reads `Error: Exit code 1`, that is a line IN THE OUTPUT and not a judgement this section made - and nothing here can tell you whether the harness wrote it or the checked program did. Read the output

5. nothing here checks that these commands exercise the diff; a suite that never touches the changed files reads exactly the same, and no receipt is compared against the files this PR changes

6. commands run inside a spawned subagent are deliberately excluded, so work the coder delegated leaves no receipt here

7. only a command the HARNESS backgrounded leaves no receipt at all - it hands back a task id instead of output, so there was nothing to record. A trailing `&` YOU wrote is NOT that and is NOT excluded: `pytest -q &` is recognised, recorded and headed `test` like any other line, and bash forks it, so that entry names a check that may still have been running when the harness returned

8. a command the harness refused to run (blocked, or permission denied) leaves no receipt, because it never ran

9. only commands recognised as checks are recorded, and recognition reads the command line ONLY - it never looks inside what a command runs. So `bash -c 'uv run pytest -q'` leaves no receipt at all while `make test` leaves one that names `make` and not the recipe it ran

10. recognition is also textual the other way: a check merely NAMED in a heredoc body, or in a quoted string that happens to spell a shell separator, can be recorded as though it ran

11. recognition cannot see CONTROL FLOW either: a recorded command line may name a check the shell never reached, and it is still recorded, still headed by that check's kind, and still counted as a recorded command everywhere above. TEN SHAPES WERE DRIVEN against bash 3.2.57 with the check replaced by a marker-printing stub, and the marker was absent in every one: a failed `&&`, a taken `||`, an `exit`, an `exec`, an `exit` inside a `source`d script, a syntax error that aborts the REST of the line (what came BEFORE it does run), a multi-line `if false`, a `case` that matches nothing, `set -e` aborting an earlier command, and `set -u` on an unset variable. That list is MEASURED, NOT EXHAUSTIVE - recognising any of it means parsing bash, and this module is not bash. So a kind this section does NOT list as missing is a kind some recorded line named, which is not the same as a kind that ran

12. where the harness reported something instead of output - a timeout, an interruption, its own wording of a non-zero exit - that report is appended to the captured text in square brackets; the coder's own output can spell the same thing, so it is text like everything else here

13. the COMMAND and the output are both redacted and bounded before they are stored, so an excerpt is not the full log, a credential-shaped string may have been masked out of either, and a command over 400 characters is shortened in the middle

14. each command is displayed on ONE line: a multi-line command has its newlines folded to spaces, so the string shown may not re-run as written

15. invisible and direction-changing characters are stripped from the command and the output before display, so what is shown can differ by those characters from what ran; look-alike letters are NOT detected

16. no_human's own test run, CI, and the independent review are separate signals - this section covers only the coder session's own commands

