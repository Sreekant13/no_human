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
