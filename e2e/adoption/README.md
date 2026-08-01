# The daily adoption test

```bash
e2e/adoption/run.sh                 # ~6 min, no spend, no credential
e2e/adoption/run.sh --mode full     # + real task execution (real money)
```

Exit code 0 means every blocking assertion held. Exit 1 means at least one
regressed, and `out/FRICTION_LOG.md` says which.

Cron:

```
0 6 * * *  cd /path/to/no_human && e2e/adoption/run.sh >> /var/log/nh-adoption.log 2>&1
```

## What this is

Five people at a startup adopt no_human on a Monday, having read only the public
docs. A CTO installs it. A senior developer files a sprint of aviation-analytics
work. A developer wires it into Jira, Slack and GitHub. A DevOps engineer wires
the Jenkins and CircleCI gates the website advertises. A fifth reviews what came
back. The harness *is* those five people, and its output is every place they got
stuck.

## Why it exists

Everything about this product had been verified by people who already knew the
answers. That is not a criticism of anyone; it is a structural property of
checking your own work, and it has a recognisable signature — every check passes
and the product is unusable. Two examples were live in `main` on the day this
was written:

- `uv sync`, the README's install command, failed on **every** clean clone. The
  wheel force-included `web/dist`, `web/dist` is gitignored, and `uv sync`
  builds an editable wheel. Nobody saw it because nobody had a clone without a
  `web/dist` in it.
- `nh task add PROJ-42` was documented in the quickstart, in `adapters.md`, and
  in the CLI's own `--help`. Its adapter had been deleted. The live command
  returned `not a recognized task URL/id`.

Both are caught by one honest persona run, in the first ninety seconds, because
installing and filing a ticket are the first two things anybody does.

## The constraint that makes the result mean anything

The personas get only what those people would actually have. Every one of these
is enforced by construction rather than by intention:

| | |
|---|---|
| `HOME` | a fresh temp directory, so `~/.no_human` starts empty. The operator's real one is never opened, and the harness proves it two ways: it asks the product's own process where its home is, and it checks that no new entry appeared in the real directory. |
| `PATH` | a shim directory of symlinks to the tools a Mac developer has — git, uv, node, npm, claude — and nothing else. **`nh` is asserted absent.** On the author's machine `nh` is globally installed, and that single fact is what hid the quickstart's bare-`nh` bug. |
| the repo | a real `git clone`. Not a worktree, not the tree you are sitting in. The install bug above exists *only* in a clone. |
| the target | a SkyLine repo generated from scratch, with two real planted bugs and a working `uv run pytest -q`. |
| CI | local Jenkins and CircleCI servers on 127.0.0.1, driving the **real** adapters. No live CI instance is ever contacted. |
| secrets | none. No credential is read, written or requested. |

If a documented step fails, the harness records the failure and — only where a
real person could plausibly have found the workaround themselves — continues
with the workaround **recorded as friction**. It never quietly does the right
thing on the persona's behalf. Every step carries the doc section it is
following; a step with no `doc_ref` is itself a finding.

## The integration boundary, stated plainly

| system | how it is exercised | live? |
|---|---|---|
| Jenkins | a local HTTP fake at a configured `base_url`, driving the **real** `JenkinsCI` adapter over its real `curl` transport. Green, red, 401, 503 and never-finishing. | **no** |
| CircleCI | a local HTTP fake, driving the **real** `CircleCICI` adapter with only the module-level `_API` constant redirected. Same five outcomes. | **no** |
| Jira | a local protocol-faithful fake: ADF descriptions, `/rest/api/3/search/jql`, transitions, comments, HTTP Basic | **no** |
| Slack | a local incoming-webhook receiver | **no** |
| GitHub | a *real* push through the product's real VCS layer to a local bare remote — the `local` backend `adapters.md` documents. `gh pr create` is **not** exercised. | **no** |

A fake proves our request shape, auth scheme, URL construction, parsing and
write-back logic. It proves nothing about the vendor: not their auth, not their
rate limits, not their field semantics, not their deprecations. A green run here
is compatible with a completely broken live integration, and every such result
is labelled `live: false` in both the JSON and the friction log. See
`fakes.py`.

The CI fakes are the most faithful of the set, because both adapters can be
pointed at them with almost no seam: Jenkins speaks over `curl` to a configured
`base_url`, and CircleCI over httpx to a single module-level constant. What they
still cannot show is whether a real Jenkins accepts our crumb flow or a real
CircleCI token carries the scopes we assume. **A live smoke test against one
real instance of each remains unperformed**, and it is the obvious next step
before quoting the website's CI claim as verified.

## The backlog

`backlog.py` — fourteen tickets for **SkyLine**, an AI analyst agent for
aviation and real-estate questions ("which New England airports are candidates
for terminal expansion?"). Features, two planted bugs, two refactors, an
investigation, a design document, a chore.

The important field is `expectation`:

- `deliver` — specified well enough that a reviewed PR is the only honest
  outcome.
- `escalate` — genuinely ambiguous (*"make the answers more trustworthy"*) or
  under-specified (*"add voice"*). The correct result is a parked task asking
  **one** specific question. **A PR here is scored as a failure**, because it
  means the agent guessed and spent the team's money on its guess.
- `either` — a reasonable agent could go either way; scored as neither.

A backlog of only well-formed tickets measures typing speed. Scoring a guess as
a win builds a harness that rewards guessing, which then optimises the product
in exactly the wrong direction.

## Modes

**`smoke`** (default) — install, docs fidelity, CLI contract, adapters against
fakes. No credential, no spend, a few minutes. This is the daily job.

**`full`** — additionally drains the staged backlog for real and measures
delivery, honest stops, reviewer rejections and cost. It takes a credential
**only** from `NH_ADOPTION_OAUTH_TOKEN`, exported deliberately for the
invocation; the harness never reads `~/.no_human/.env` and never prompts.

```bash
NH_ADOPTION_OAUTH_TOKEN="$(cat /path/to/token)" e2e/adoption/run.sh --mode full
```

Unmeasured quantities are reported as **not measured**, never as zero. A
dashboard that renders an unmeasured number as `0` is worse than one that
renders nothing.

Cost uses the product's own indicative model (`costOf` in `web/src/cost.js`),
mirrored and pinned by a test, so a number here and a number on the board cannot
disagree. Throughput is compared against the team's own **estimates** in
`backlog.py`, and the word "estimate" travels with the number everywhere it is
printed. Nothing measured here goes to the README or the site without a real,
cited run.

## What a daily run asserts

Deliberately about **mechanism**, not about counts. "No more than 12 findings"
drifts upward one finding at a time and never fails; "the README's install
command exits 0 on a clean clone" either holds or it does not.

Three of them are **derived from the documentation** rather than pinned to a
sentence, which matters because the first version was pinned and went stale
within an hour of the first fix. They parse the quickstart's own code fences
and the configuration table, so they keep holding whatever the docs currently
say. All three are mutation-tested: a clone with the defects reintroduced fails
exactly those three and nothing else.

| assertion | why it is the one that matters |
|---|---|
| `readme_install_works_on_clean_clone` | it was false when this was written. Nothing else matters if this is red. |
| `quickstart_commands_runnable_as_printed` | every `nh` line in a code fence is resolved against the persona's PATH. A doc whose commands do not run costs trust in every other doc. |
| `no_doc_promises_an_intake_form_that_does_not_work` | a documented command that errors is worse than a missing feature |
| `unsupported_intake_input_fails_with_an_actionable_message` | a bare ticket key is the first thing a developer types regardless of the docs; the error has to name what *is* accepted |
| `freeform_intake_works` | the one intake path that always has to work |
| `onboard_derives_and_proves_on_conventional_repo` | without a profile nothing ever runs, and it fails silently |
| `onboard_confirm_yields_a_usable_profile` | same |
| `every_documented_env_key_is_read_by_something` | a credential name the docs hand you that nothing reads fails silently: no error, no integration, no clue |
| `slack_config_path_delivers` | the notify path that does work must keep working |
| `jira_adapter_parses_real_shaped_payload` | ADF descriptions + JQL search against the fake |
| `vcs_push_and_pr_path_works_offline` | branch, commit and push through the real VCS layer |
| `persona_home_is_the_temp_dir_not_the_operators` | safety, measured positively from inside the product's own process |
| `no_new_entries_in_operator_real_no_human` | safety, measured negatively |
| `ci_backends_and_credentials_are_documented` | the site advertises Jenkins and CircleCI; every backend identifier, required key and credential must be findable in the public docs |
| `ci_verdict_tracks_the_pipeline_result` | green must read as passed and red must not — asserted as a **pair**, since a backend that failed everything would satisfy the red half alone |
| `broken_or_unreachable_ci_never_reads_as_green` | 401, 503 and a job that never finishes must never produce a passing verdict |
| `no_new_high_or_fatal_findings` | the ratchet against `baseline.json` |

The ratchet is what stops this drifting. `baseline.json` records the findings
known on the day it was written; the run fails on a **new** fatal or high one,
and reports resolved ones as a non-blocking note. Refresh it deliberately, in
the commit that fixes something, never to make a red run green.

## Files

| | |
|---|---|
| `run.sh` | the one command |
| `adoption_run.py` | environment construction, assertions, scoring, report |
| `personas.py` | the four people and what each of them does |
| `backlog.py` | the fourteen SkyLine tickets and their expectations |
| `fakes.py` | local Jira and Slack, and the boundary they do not cross |
| `baseline.json` | the known-findings ratchet |
| `out/` | the run's report (git-ignored) |
