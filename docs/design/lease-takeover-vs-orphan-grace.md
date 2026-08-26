# Design: the 600s lease-takeover / row-orphan divergence (follow-up to ec907fb00)

Status: decided. Approach (c) is implemented; this document is the decision
record `_LEASE_ORPHAN_DIVERGENCE_S` and the takeover log line in
`_claim_pool_lease` point back to.

## §1 The contradiction

`Scheduler` (`src/no_human/core/scheduler.py`) has two timing constants that
both read the same heartbeat/row data but answer different questions:

- `_HEARTBEAT_STALE_S = 300.0` — used at the two gating sites in
  `_claim_pool_lease` (the initial read and the post-CAS re-check after a
  lost race) to decide whether a *different* pid/host's heartbeat is fresh
  enough to refuse a takeover (`SiblingSchedulerRunning`).
- `_STRANDED_GRACE_S = 900.0` — used in `_row_is_live`/`_recover_orphans` to
  decide whether a mid-run row (status in `_ORPHANABLE`) is young enough to
  leave untouched rather than requeue.

Because `900.0 != 300.0`, there is a 600-second band — from age 300s to age
900s — in which a holder that is *alive but quiet* (wedged, suspended under a
debugger, or simply slow) has:

- **lost its lease** — a new process's `_claim_pool_lease` treats the
  heartbeat as stale and takes over, and
- **rows that are still protected** — the same process's mid-run rows are
  younger than `_STRANDED_GRACE_S`, so `_row_is_live` still reports them live
  and `_recover_orphans` leaves them alone.

For those 600 seconds the system holds two contradictory beliefs about the
same process: too dead to keep the lease, too alive to have its rows
recovered.

## §2 What is not the question

Raising `_HEARTBEAT_STALE_S` from 300 to 900 (or lowering `_STRANDED_GRACE_S`
to 300) is not on the table. The existing comment on `_HEARTBEAT_STALE_S`
already makes the correct case for why it is short: a stale value there means
an operator who killed a process and immediately restarted it waits out the
whole window before the replacement boots, and that failure mode is loud (an
explicit refusal to start) rather than silent — so erring toward a *shorter*
wait costs less. Symmetrically, `_STRANDED_GRACE_S` is long because a false
"still stranded" only delays recovery, while requeueing a row a live sibling
still owns is destructive (incident 6408aba0). Neither number is wrong for
the question it answers; the two questions simply are not the same question,
and closing the gap by moving either number just trades one already-argued
cost for the other.

## §3 Candidates weighed

**(a) Require positive evidence the holder is gone before takeover.**
`_lease_sibling_is_dead` (via `pid_alive`/`process_start_token`) already does
exactly this for a *same-host* pid. There is no cross-host equivalent
available to this process: it has no transport to the other host, and the
shared SQLite row is the only channel the two processes have. Inventing one
(an "are-you-alive" probe row, an SSH/HTTP health check) adds a new failure
mode, and what to do when that check is inconclusive (host unreachable —
assume dead and take over, or assume alive and refuse?) is exactly the
question the intake for this task flagged as human-gated and not
self-answerable. Not implemented.

**(b) Mark the displaced holder so its rows orphan immediately.** This closes
the gap from the other end: instead of waiting out `_STRANDED_GRACE_S`, a
takeover would flag the old holder's rows as orphan-eligible right away. The
intake answer for this option is explicit about what it would require: either
a way to notify the displaced holder to stop before its rows are cleaned
(closing the race where a "marked" row is still being actively worked), or a
second, separate active-work row state distinct from orphan-eligibility so a
row cannot be cleaned until both conditions hold. Either shape is new
persisted state on the hottest recovery path in the scheduler, and its
failure mode is the destructive one this whole area exists to avoid
(incident 6408aba0: requeueing a row a live sibling still owns). Trading a
loud, bounded refusal for a new class of silent clobber is the wrong
direction. Not implemented; no new DB column, table, or row state is added.

**(c) Accept the window, document it, and pin it with tests.** The window is
not a bug — it is two different questions with two different, already-argued
costs. The residual risk is bounded and benign: during the 600s band the new
process holds the lease, but `_row_is_live` still protects the old process's
rows from both processes (the new one never touches them, because they still
read as live). Both halves of the contradiction err toward *not destroying
live work* — the "too dead" half costs only a loud refusal-to-boot on a
retry, and the "too alive" half costs only a delayed recovery. Nothing about
this is silent: `_claim_pool_lease` now logs the takeover explicitly (see
§5), naming the window and what stays true about the displaced holder's rows.

## §4 Decision: (c), accepted window

The 600-second divergence between `_HEARTBEAT_STALE_S` and
`_STRANDED_GRACE_S` is accepted, not deferred. What is missing was never a
fix — it is a name, a log line, and a test that pins the behavior so a future
change to either constant cannot silently move the window without a test
failing and pointing back here.

Stated plainly, the residual risk: during the band, a new scheduler process
runs while an old, quiet-but-live one may still be mid-run on rows the new
process will not touch (because they still read as live). The old process
loses the lease; on its next heartbeat-refresh attempt it hits the
`ec907fb00` refresh-failure path (a failed refresh now stops dispatch rather
than being swallowed), so it will not keep dispatching new work under a lease
it no longer holds.

## §5 What pins it

- `Scheduler._LEASE_ORPHAN_DIVERGENCE_S` (`src/no_human/core/scheduler.py`,
  next to `_HEARTBEAT_STALE_S`) — the 600s divergence as a derived,
  first-class value (`_STRANDED_GRACE_S - _HEARTBEAT_STALE_S`), never
  hand-typed, so the two source constants cannot drift apart from their
  documented relationship without the derived value moving with them.
- `tests/test_scheduler_lease_orphan_window.py` — pins both constants, the
  derived divergence, and the behavior at all three age bands (below the
  stale threshold, inside the 600s window, past the stranded grace).
- The `log.warning(...)` takeover line in `_claim_pool_lease` — every
  takeover of a non-fresh sibling heartbeat now names the sibling pid/host,
  the heartbeat age, `_HEARTBEAT_STALE_S`, and `_LEASE_ORPHAN_DIVERGENCE_S`
  at WARNING, so the window is a discoverable runtime signal, not only a
  comment or a doc.

## §6 What would reopen it

- A genuinely cross-host deployment, where `_lease_sibling_is_dead` cannot
  even attempt the same-host `pid_alive` check and every non-fresh takeover
  is a pure age guess. That is the scenario approach (a) was rejected for
  lacking a cross-host mechanism to answer, not for being conceptually wrong.
- A real incident in which a quiet-but-live holder's rows were damaged
  *inside* the 600s band — i.e. evidence the "protected until
  `_STRANDED_GRACE_S`" half of the current bargain is not actually holding.
  Absent such an incident, the window is a documented, tested trade-off, not
  an open question.
