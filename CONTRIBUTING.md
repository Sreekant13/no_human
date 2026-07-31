# Contributing to no_human

Thanks for taking the time. This page covers setup, the test suites, the
conventions the codebase holds to, and how a change gets merged.

Before you write code, know the constraints the project treats as correctness
requirements rather than preferences. A change that violates one of them will
be rejected even if the tests pass:

- The agent never merges. It opens a PR and stops; merging is always a human
  action, and there is no auto-merge anywhere.
- The stack stays lean: SQLite only, a single Claude backend through the Agent
  SDK, no vector database.
- Review is evidence-based — an independent fresh-context reviewer producing a
  pass/fail checklist with cited evidence. Never a numeric self-score.
- Only verifiable signals are trusted: a net reduction in test count or
  assertion count is blocked.
- Task loops are bounded, and a blocker is never resolved by weakening tests,
  expanding scope, or faking "done".
- Credentials are never read from or written to anywhere in the repo.

## Before you open a PR

`main` is protected. Nobody pushes to it directly, and no automation merges to
it. Every change lands through a pull request that the maintainer reviews and
merges by hand. That includes changes written by no_human itself: the agent
opens a PR and stops.

Practical consequence: open an issue first for anything larger than a bug fix.
A rejected design costs you less as a paragraph than as a branch.

## Setup

Prerequisites: Python 3.12 (see [`.python-version`](.python-version) and the
`requires-python` field in [`pyproject.toml`](pyproject.toml)), git, and
[uv](https://github.com/astral-sh/uv). Node 20 and Node 22 are needed only if
you touch the web board or the desktop shell.

```bash
git clone <your-fork-url> no_human && cd no_human
uv sync --frozen
uv run nh --help
```

`uv sync --frozen` installs from the committed [`uv.lock`](uv.lock) without
re-resolving. `uv.lock`, `web/package-lock.json`, and
`desktop/package-lock.json` are all tracked. If you change a dependency, commit
the updated lockfile in the same PR. You can check that the lock still matches
`pyproject.toml` with:

```bash
uv lock --check
```

You do not need a Claude credential to develop or to run the test suites. The
suites are hermetic: `tests/conftest.py` installs an autouse fixture that
replaces `ClaudeBackend` everywhere the orchestrator constructs one, so no test
reaches the model API unless you set `NH_TESTS_LIVE_SDK=1`. You only need a
credential to run the product end to end. See
[`docs/quickstart.md`](docs/quickstart.md) for that.

## Running the tests

### Python

```bash
uv run pytest -q
```

About 2,980 tests. On a 4-core machine `-n 4` brings a full run to roughly four
minutes:

```bash
uv run pytest -q -n 4
```

Do not use `-n auto`. It has wedged repeatedly on this repo. Pick an explicit
worker count.

Run it through `uv run`, or with the virtualenv activated — not with a bare
`pytest` from an unactivated shell. Four tests shell out to `pytest` as a
subprocess, because that is the test command the product itself infers for a
plain Python repo, and a subprocess resolves it from `PATH`:

```
tests/test_base_tree_gate.py::test_coder_introduced_import_breakage_fails_the_attempt
tests/test_holdout_gate.py::test_passing_held_out_does_not_block
tests/test_onboard.py::test_onboard_python_pytest_end_to_end
tests/test_onboard.py::test_onboard_writes_yaml_and_round_trips
```

With `pytest` missing from `PATH` they fail on `/bin/sh: pytest: command not
found`, and the assertion you see is about a task that escalated, which does not
point at the cause. `uv run pytest` puts the virtualenv's `bin` on `PATH`, which
is why CI uses it. This is recorded because nothing else in the repo says so.

Two markers are declared in `pyproject.toml`:

- `slow` for tests over 10 seconds. Skip them with `-m "not slow"`.
- `real_backend` for tests that exercise the real `ClaudeBackend` class over a
  mocked SDK client. They are exempt from the hermetic stub and still make no
  network call.

Two tests need a running Windsurf IDE on the same machine and fail everywhere
else, including CI:

```
tests/test_scheduler.py::test_reanalysis_maybe_run_produces_result
tests/test_scheduler.py::test_reanalysis_dedup_across_runs
```

They read local IDE transcripts through `no_human.history.extractor`, which
scans running processes for a language server. CI deselects both. If you see
`IDENotRunningError` locally, that is why.

CI deselects a third test for a different reason — a real, open defect:

```
tests/test_scheduler.py::test_two_repos_run_concurrently_in_worktrees
```

It is the only test that drives two orchestrators against one `Store` at once,
and about a third of runs die on `cannot commit transaction - SQL statements in
progress`. It is not an xdist flake: it reproduces serially and on its own.
[`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) has the repro data, the
hypothesis already ruled out, and what a fix has to prove. Run it locally,
several times, if you touch `core/db.py` or the scheduler.

There is also [`scripts/run_tests.sh`](scripts/run_tests.sh) with `fast`,
`slow`, and `full` modes. It uses `-n auto`, so prefer the direct `pytest`
invocation above.

### Web board

Node 20. The `npm test` script is `node --test src/`, and Node 22 changed how
`--test` resolves a directory argument, so a directory path no longer works
there. CI runs this job on Node 20 for that reason.

```bash
cd web
npm ci
npm test
```

538 tests. These are `node --test` unit tests over the board's pure helpers,
theme variables, and accessibility logic.

`npm run lint` is broken, and it is broken twice over. It is not wired into CI,
so neither failure blocks anything today.

On **Node 20** — the version CI pins for this job, and therefore the version you
are most likely to have installed for it — eslint 10.7.0 lints fine and then
dies printing the result:

```
ESLint: 10.7.0
TypeError: util.styleText is not a function
    at .../eslint/lib/cli-engine/formatters/stylish.js:22:9
```

`util.styleText` arrived in Node 22. The `stylish` formatter reaches for it once
per result it prints, so on Node 20 eslint crashes while formatting its own
output — but only when it has something to print. On a file with no findings it
exits 0. That is the worse failure mode of the two: the tool looks fine right up
to the moment it has a problem to tell you about.

On **Node 22** eslint gets far enough to print, and then reports the problem
underneath:

```
web/src/Integrations.jsx
  283:5  error  Definition for rule 'react-hooks/exhaustive-deps' was not found
```

`eslint-plugin-react-hooks` is not in `web/package.json`. The config does not
name that rule either — `web/eslint.config.mjs` enables exactly one, `no-undef`.
What names it is an `// eslint-disable-next-line react-hooks/exhaustive-deps`
comment at `web/src/Integrations.jsx:283`, and eslint errors on a disable
directive for a rule it cannot find.

Fixing this is a welcome PR, and it is two independent fixes. Adding
`eslint-plugin-react-hooks` (or deleting the disable comment) clears the error,
and because the crash only fires when there is a result to format, that alone
makes `npm run lint` exit 0 on Node 20 today. It does not make it *work* there —
the next real lint finding brings the crash straight back. So also pin eslint to
a version whose formatter runs on Node 20, pick a formatter that does not use
`util.styleText`, or run lint on Node 22 — `npm test` is what needs Node 20 here,
not lint, and the two do not have to share a runtime.

### Desktop shell

Node 22 (`desktop/package.json` sets `engines.node` to `>=22.12`).

```bash
cd desktop
npm ci --ignore-scripts
node --test $(ls *.test.mjs | grep -v '^uiPages.test.mjs$')
```

167 tests. `--ignore-scripts` skips Electron's postinstall, which downloads a
platform binary of about 100 MB. The suite does not need it: Electron is
stubbed through `desktop/testing/electronLoader.mjs`.

The one exception is `desktop/uiPages.test.mjs`, which spawns the real Electron
binary to measure computed styles in a renderer. It needs a full install, so run
it on its own:

```bash
cd desktop
npm ci
node --test uiPages.test.mjs
```

Do not use `npm test` here. The script is `node --test .`, and a directory
argument stopped being walked in Node 22 — the same change described under the
web board above. It works only on Node 20, which is below this package's own
`engines.node` floor of `>=22.12`. Stay on Node 22 and name the files.

### Playwright end-to-end

Not part of CI. These need a browser download and, for some suites, a running
server. Run them before a UI change.

Board e2e, driven from Python (see [`e2e/README.md`](e2e/README.md)):

```bash
uv sync --group e2e
uv run playwright install chromium
cd web && npm install && npm run build && cd ..
uv run python e2e/serve_demo.py 8488 &
NH_E2E_BASE=http://127.0.0.1:8488 uv run python e2e/board_e2e.py
```

Browser suites over the built bundle (see `web/e2e/run-all.mjs`):

```bash
cd web
npm run build
npm run e2e       # the live-flows suite needs a server on :8420
```

## Coding conventions

- Python 3.12, standard library first. The dependency list in `pyproject.toml`
  is short on purpose.
- Do not add to the stack. SQLite only. One Claude backend through the Agent
  SDK. No vector database. This is a standing project constraint and it is not
  negotiable in a PR.
- Tests ship with the module they cover. A PR that adds behaviour and no test
  will be sent back.
- A test must observe an artifact, not recompute the expected value from the
  code under test. If you can break the wiring and the test still passes, the
  test proves nothing.
- Never reduce test count or assertion count without saying so in the PR body
  and explaining why. There is a tamper guard in the product that blocks this,
  and the same standard applies to humans.
- Do not test UI behaviour with a regex over source text. Measure it in a
  renderer. That mistake has cost this repo whole review rounds.
- Comments explain why, not what. Prefer a sentence about the failure mode a
  line prevents over a restatement of the line.
- Credentials are never read from or written to anywhere in the repo. They live
  in `~/.no_human/.env` at `chmod 600`.

## Proposing a change

1. Open an issue describing the problem. For a bug, include the repro.
2. Fork, and branch from `main`.
3. Make the change. Keep it to one concern.
4. Run the suites that your change touches. Paste the command and its output in
   the PR body. "Looks done" is not evidence here.
5. Open the PR against `main` and fill in the template.
6. The maintainer reviews and merges. There is no auto-merge on this repo.

CI runs the Python, web, and desktop suites on every pull request, and on
pushes to `main` — not on pushes to a branch that has no PR open yet. It
runs nothing that needs a credential, a model API call, or a push to a real
repository.

## Contributor licence agreement

Your first PR needs you to agree to [`CLA.md`](CLA.md). It matters because the
project may be relicensed later and may be offered as a hosted service. Read it
before you agree — that is the whole point of it.

Agreement is **recorded in git**, not in a signing service and not as a
tick-box. In your first pull request, add one file named after your GitHub
handle, in lower case:

```
contributors/<your-github-handle>.md
```

containing the version of `CLA.md` you agree to and the date. The template is in
[`contributors/README.md`](contributors/README.md). You do it once, ever.

The `CLA ledger` job in CI asks GitHub who authored the commits in your PR and
fails if any of them has no such file. It checks only that the file exists — it
cannot verify a signature and does not try to. The maintainer's own commits and
bot accounts are skipped.

## Trademark

The licence covers the code, not the name. [`TRADEMARK.md`](TRADEMARK.md) says
what you may do with "no_human" and the logo. In short: describe accurately what
your thing is or works with, and do not imply this project endorses it. Forks
are welcome; give yours its own name.

## Third-party notices

[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) records attribution and the
obligations a *packaged binary* carries — Electron, Chromium, and the
PyInstaller bootloader exception. The source tree vendors no third-party code,
so it matters only if you build and hand someone an installer. If you touch
`packaging/`, read it first.

## Security issues

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).

## Conduct

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies to every space this project
uses.
