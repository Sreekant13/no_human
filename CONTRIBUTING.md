# Contributing to no_human

Thanks for taking the time. This page covers setup, the test suites, the
conventions the codebase holds to, and how a change gets merged.

Read [`CLAUDE.md`](CLAUDE.md) before you write code. It holds the constraints
that the project treats as correctness requirements rather than preferences.
A change that violates one of them will be rejected even if the tests pass.

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

`npm run lint` is currently failing on a missing `react-hooks/exhaustive-deps`
rule definition in `web/eslint.config.mjs`. It is not wired into CI. Fixing the
ESLint config is a welcome PR on its own.

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
binary to measure computed styles in a renderer. Run it locally with a full
install:

```bash
cd desktop
npm ci
npm test          # Node 20 only; on Node 22 use the explicit file list above
```

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
  SDK. No vector database. This is written down in `CLAUDE.md` and it is not
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

CI runs the Python, web, and desktop suites on every push and pull request. It
runs nothing that needs a credential, a model API call, or a push to a real
repository.

## Contributor licence agreement

Your first PR needs you to agree to [`CLA.md`](CLA.md). Say so in the PR body:

```
I have read CLA.md and I agree to it.
```

It matters because the project may be relicensed later and may be offered as a
hosted service. Read it before you agree.

## Security issues

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).

## Conduct

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies to every space this project
uses.
