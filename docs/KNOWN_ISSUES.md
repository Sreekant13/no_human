# Known issues

Defects that are real, reproduced, and not yet fixed. Each entry says what was
measured, what was ruled out, and what a fix would have to prove. An entry
leaves this file when the defect is fixed, not when it stops being convenient.

---

## KI-1 — concurrent tasks can crash a `Store` commit

**Status:** open. Deselected in CI (`.github/workflows/ci.yml`), so the badge
is honest rather than red on a third of pushes.

**Symptom**

```
sqlite3.OperationalError: cannot commit transaction - SQL statements in progress
  src/no_human/core/db.py:518 in update_attempt   (await self.db.commit())
  <- src/no_human/core/orchestrator.py:2706 in _run_attempt
```

**This is a product defect, not a test defect.** The traceback is entirely in
shipped code — `Orchestrator._run_attempt` calling `Store.update_attempt` — and
the condition that triggers it, two tasks running at once against one `Store`,
is a supported configuration (`concurrency.enabled: true` with `max_workers`
above 1). A user running two tasks in parallel can lose an attempt to this. The
deselect below keeps the CI badge truthful; it does not make anyone safer. Until
KI-1 is fixed, `max_workers: 1` is the configuration with no known exposure.

The affected test is
`tests/test_scheduler.py::test_two_repos_run_concurrently_in_worktrees`, the
Phase 7 definition-of-done for two tasks in two repos running through the pool
at once. It is the only test that drives two orchestrators against one `Store`
concurrently, which is why it is the only one that trips this.

**Measured failure rate** (2026-07-30, macOS/Darwin 25.5.0 arm64,
Python 3.12.13, aiosqlite 0.22):

| condition                             | failures | measured by             |
| ------------------------------------- | -------- | ----------------------- |
| this test alone, serial, no xdist     | 3 / 8    | this note               |
| this test alone, serial, no xdist     | 1 / 3    | the branch review       |
| whole suite, `-n 4`                   | 1 / 3    | the branch review       |

**It is not an xdist problem.** It fails with no xdist at all, and it fails
running the one test on its own. Lowering the worker count does not help, and
any description of it as "intermittent under `-n 4`" is wrong. The concurrency
that matters is *inside* the test — two `asyncio` tasks sharing one `Store` —
not between pytest workers.

**Mechanism, as far as it has been established**

`Store` holds a single `aiosqlite.Connection`, and `aiosqlite` drives one
`sqlite3` connection from one worker thread. Every coroutine in the process
shares it, including the implicit transaction that `sqlite3`'s legacy
transaction handling opens before a DML statement. When one coroutine issues
`COMMIT` while another statement on that connection is still active, SQLite
refuses the commit with the message above.

**Ruled out.** The obvious candidate — a `SELECT` cursor left unexhausted
across an `await`, of the form `cur = await db.execute(...)` then
`row = await cur.fetchone()` — was instrumented (patching
`aiosqlite.Connection.execute` / `Cursor.fetchone` / `fetchall` / `close` plus a
`weakref.finalize` per cursor) and the set of live read cursors was **empty** at
every failing commit across four captured failures. So the culprit statement is
not one the store code is still holding a Python reference to.

**Lead, not a fix.** Opening the connection in autocommit mode
(`aiosqlite.connect(path, isolation_level=None)`, a one-line change at
`db.py:47`) took the isolated test from 3/8 failures to **0/12**. That is a
strong signal about where the problem lives, but it is not a fix that can be
adopted on that evidence: it removes multi-statement atomicity from every write
path in the product (`create_attempt`'s `UPDATE` + `INSERT` pair, `_migrate`,
and others), and twelve green runs of one test say nothing about crash
consistency. It is recorded here so the next person does not have to rediscover
it.

**What a fix has to prove**

1. The concurrency test passes at least 10 consecutive serial runs and 10
   consecutive `-n 4` runs. One green run proves nothing about a flake.
2. The full suite stays green.
3. If the fix changes transaction semantics, it says which multi-statement
   writes lose atomicity and why that is acceptable — or it keeps them atomic.

Until then the test is deselected in CI and should be run locally, repeatedly,
by anyone touching `core/db.py` or the scheduler.

---

## KI-2 — `nh init` has no non-interactive mode

**Status:** open. Found 2026-08-01 by the adoption harness (`e2e/adoption/`),
persona "Dana, CTO", step `nh-init-noninteractive`.

**Symptom**

```
$ uv run nh init < /dev/null
...
3. Authentication
  How will this install pay for Claude?
    Choice (1, 2) [1]: Aborted!
```

`nh init` is the only documented way to produce a working `~/.no_human`, and it
is interactive-only: no `--yes`, no `--auth-mode`, no `--non-interactive`, no
config-from-file. Any path without a TTY — a provisioning script, a Dockerfile,
CI, a `curl | sh` onboarding one-liner, or the daily adoption harness itself —
cannot use it.

**Why it matters more than it looks.** The scenario this was found in is a
startup putting the same install on several developers' machines. The CTO's
first instinct is to script it once so the team is set up identically; the tool
has no answer, so each developer runs a wizard by hand and the installs diverge
in exactly the settings (`git.agent_identity_*`, `llm.auth_mode`, `server.port`)
where divergence is most confusing later.

**Workaround.** Write `~/.no_human/config.yaml` and `~/.no_human/.env` directly;
`load_config` fills every unset key from `DEFAULT_CONFIG`, so a partial file is
enough. This is what `e2e/adoption/adoption_run.py::Ctx.seed_config` does, and
it is the shape a `--non-interactive` mode should produce.

**What a fix has to prove**

1. `nh init --non-interactive --auth-mode subscription` produces a
   `~/.no_human` that `nh doctor` accepts, with stdin closed.
2. It still refuses to invent a credential: no token means an explicit,
   non-zero, named failure, never a config that silently cannot run.
3. Re-running it is still safe — `nh init`'s existing "never overwrites
   existing config, secrets, or data" property holds.

---

## KI-3 — `nh serve` cannot drain-and-exit

**Status:** open. Found 2026-08-01 by the adoption harness, persona "Sam, senior
developer", step `serve-help`.

**Symptom.** `nh serve`'s only option is `--max-workers`. It runs until
interrupted. There is no `--once` / `--drain` / `--until-empty`, and therefore
no exit code that says whether the queue drained, whether anything failed, or
whether it stopped early.

**Why it matters.** `docs/quickstart.md` §8 recommends leaving `nh serve`
running overnight, which is fine for a person at a laptop and unusable from
anything automated. To run "work the queue, then stop" — a nightly cron, a CI
job, a benchmark, or an adoption test — the caller has to background the
process, poll `nh status --json` on a timer, and send it a signal. Every
consumer reimplements the same supervisor, and each one invents its own
definition of "done".

**Workaround.** `e2e/adoption/adoption_run.py::run_full_mode` does exactly
that supervision loop and is a working reference for it.

**What a fix has to prove**

1. `nh serve --until-empty` exits 0 when the pending and running lanes reach
   zero, without a signal.
2. It exits non-zero when it stops for any other reason (budget, timeout,
   crash), with the reason on stderr.
3. In-flight tasks still drain on Ctrl-C exactly as they do today.

---

## KI-4 — `nh onboard` reports a failed command without its output

**Status:** open. Found 2026-08-01 by the adoption harness, persona "Sam",
step `onboard-proving-opacity`.

**Symptom**

```
proving (running each candidate):
  ✗ [FAILED] test: pytest -q  (from python/pytest, exit 1)
...
test command NOT proven — profile is not usable until it runs clean. Nothing
faked; fix the repo or its declarations and re-run.
```

The exit code is shown; not one line of the command's own stdout or stderr is.
The user cannot tell whether the cause was a missing dependency, an import
error, a collection error, or a genuinely failing test.

**Why it matters.** Onboarding is the first thing every user does after
install, and refusing to confirm an unproven test command is *correct* — the
message even says so well. But a correct refusal with no diagnostic is where a
new user stops. In the run that found this, the cause was a one-line fixture
problem that the captured stderr would have named immediately.

**What a fix has to prove**

1. A failed proving candidate prints the last N lines of its combined output,
   attributed to the command.
2. Output is truncated, not unbounded — a 10,000-line pytest failure must not
   flood the terminal.
3. Nothing secret is echoed: the proving subprocess inherits the process env,
   which by then holds loaded `.env` values.

---

## KI-5 — a misconfigured CI backend silently becomes "no CI gate"

**Status:** open, and the most consequential entry in this file. Found
2026-08-01 by the adoption harness (`e2e/adoption/`), persona "Marco, DevOps",
step `ci-misconfig-is-not-silently-no-gate`.

**Symptom.** `ci_from_config` returns `None` — not an error — when `ci.enabled`
is true but the selected backend's required key is absent. Measured, by calling
the real function:

```
gitlab_missing_project        -> None (NO GATE)
jenkins_missing_job           -> None (NO GATE)
circleci_missing_slug         -> None (NO GATE)
github_actions_missing_repo   -> None (NO GATE)
typo_in_backend_name          -> raises ValueError
disabled                      -> None   (correct: the operator said no)
```

`Orchestrator._run_attempt` reads `self.ci_runner is None` as "no remote CI is
wired for this repo" and proceeds with the local suite as the only gate. The
`ValueError` case is worse: `orchestrator.py` catches it into a `log.warning`,
so a misspelled `ci.backend` produced nothing on any surface a user looks at.

**Why this is the one that matters.** getnohuman.com advertises "Jenkins &
CircleCI — test layers can run on your CI, and the results gate the loop." A
user who sets `ci.enabled: true`, gets one key wrong — easy, since until today
the per-backend keys were undocumented — receives no error, no blocker, and no
event, and their tasks open PRs having never been gated on CI. They believe
they have a gate. The failure is invisible precisely to the person relying on
it.

**What was fixed, and what deliberately was not.** The silence is fixed, and
by a wider fix than this entry originally described. `ci_backend_unavailable`,
the event this branch added, is gone: it was guarded on `prof.ci.get("enabled")`
and nothing in `onboard.py` or `profile.py` ever writes an `enabled` key, so it
could not fire for anybody. `Orchestrator._resolve_ci_runner` (2026-08-02)
replaced it. A source that asks for CI and cannot produce a backend now emits
an `advisory` naming the origin and the reason — counted by `nh doctor` under
`advisory_degradations` — and `doctor.py::ci_config_problems()` reports the
same condition statically, which matters because this failure mode leaves no
events at all on a run that never happens. The `ci_skipped` event no longer
claims "no remote CI configured" — false in exactly this case — but says "no
remote CI ran".

That fix also closed the wider hole this entry used to be bounded by: the
global `ci:` block documented in `docs/configuration.md` was read by nothing,
so a user who configured CI the documented way got no gate and no warning.
Both production routes to a backend were inert. They are wired now, with
stated precedence (injection > profile > global config).

Whether this should **escalate** rather than proceed is not fixed here, on
purpose. It is a change to the gate itself; it would turn currently-completing
runs into parked tasks for anyone with a half-configured `ci` block, and it
deserves its own review rather than being folded into a documentation pass.

**What a fix has to prove**

1. `ci.enabled: true` with an unbuildable backend does not reach `open_pr`. It
   escalates with a blocker naming the missing key.
2. `ci.enabled: false` still proceeds silently on the local suite — the
   operator declining CI is not an error and must not become one.
3. An unknown `ci.backend` is rejected at config load, not at attempt time, so
   the user hears about a typo before a run is spent.
4. The existing zero-config path (no `ci` block at all) is untouched: it still
   emits `ci_skipped` and proceeds.

**Verified to work, in the same run** (real adapters, local fakes — see the
boundary note below): for both Jenkins and CircleCI, a green pipeline yields
`passed=True`, a red one `passed=False`, a 401 sets `access_failure` with the
correct `.env` key named (`JENKINS_USER` / `CIRCLECI_TOKEN`), and a 503 sets
`infra_failure`. None of the failure modes ever produced a passing verdict. So
the gate's *verdict* logic is sound; it is the *wiring* that can vanish.

**Boundary.** All of the above was measured against local HTTP fakes on
127.0.0.1, driving the real `JenkinsCI` and `CircleCICI` adapters (Jenkins over
its real `curl` transport at a configured `base_url`; CircleCI with only the
module-level `_API` constant redirected). **No live Jenkins or CircleCI
instance was contacted.** These results say nothing about either vendor's real
auth, scopes, rate limits or payload shapes. A live smoke test against one real
instance of each remains unperformed and is the obvious next step.

