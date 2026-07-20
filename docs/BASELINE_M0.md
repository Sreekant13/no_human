# M0.5 — the cost baseline

Every later milestone is measured against this. Captured 2026-07-10 from the
operational DB, before any M1–M5 work landed.

Reproduce with:

    sqlite3 -readonly ~/.no_human/no_human.db < docs/baseline_queries.sql

## The one number that matters

`no_human has never opened an authored PR.`

```
$ sqlite3 -readonly ~/.no_human/no_human.db \
    "SELECT COUNT(*) AS attempts_with_pr_url FROM attempts WHERE pr_url IS NOT NULL;"
0
```

This is the M0 exit criterion, and it is currently zero. It stays the headline
number until an authored PR exists.

## The flagship run — task `84251cb2` (INTEGRATION-GATE integration-test pipeline)

Status `blocked`. Stopped by budget, not by a defect: two real reviewer findings
away from a PR. Work preserved on `scratch/dev/84251cb2-5`
(1 commit ahead of `origin/dev`).

```
$ sqlite3 -readonly ~/.no_human/no_human.db \
   "SELECT attempt_number, status, turns_used, tokens_used, cache_read_tokens,
           COALESCE(review_passed,'-') rev
    FROM attempts WHERE task_id LIKE '84251cb2%' ORDER BY attempt_number;"

attempt  status       turns  tokens  cache_read  review
1        in_progress  41     58743   3145988     -
2        failed       17     6016     981499     -
3        failed       23     8840    1249705     -
4        failed       22     8896    1146606     -
5        failed       21     6568    1028431     -
6        in_progress  22     9200    1272586     -
7        failed       20     7948    1072746     0
8        failed       41     26955   3230320     0
9        in_progress   0        0          0     -
```

Wall clock **3.09 h**, **865 events**, **9 attempts**.

Two observations the numbers force:

- `bounds.max_attempts` is 3, yet nine attempts exist. Each `nh reply` / resume
  starts a *fresh* bounded loop, so the bound is per-run, not per-task. Nothing
  caps a task's lifetime spend.
- Attempts 1, 6 and 9 are still `in_progress`. An attempt row is never closed
  when the process is killed, so `status` on `attempts` cannot be trusted as a
  completion signal.

## Where the money goes — per-role token burn

```
$ sqlite3 -readonly ~/.no_human/no_human.db "
  SELECT json_extract(data,'\$.source') AS role,
         SUM(COALESCE(json_extract(data,'\$.cache_read_tokens'),0)) AS cache_read,
         SUM(COALESCE(json_extract(data,'\$.tokens_used'),0))       AS tokens,
         SUM(COALESCE(json_extract(data,'\$.num_turns'),0))         AS turns
  FROM task_events
  WHERE task_id LIKE '84251cb2%' AND json_extract(data,'\$.kind')='result'
  GROUP BY role ORDER BY cache_read DESC;"

role                    cache_read   tokens   turns
agent (coder)           13,127,881   133,166   207
planner:risk-first         348,113    32,506    14
planner:minimal-first      259,137    19,180    16
planner:test-first          59,668    23,016     6
reviewer                    15,230    25,243     2
aggregator                  15,230    10,850     1
                        ----------
total                   13,825,259
```

**The coder is 95.0% of all cache-read tokens.** That is the M3 target, and it
is the reason the dead `_distill_large_chunks` path was never worth fixing: it
addressed under 1% of the burn.

Cost of this run: **~$620** of a $1000 Claude Enterprise budget. Two bugs since
fixed inflated it — duplicate execution (`2f2b229`) ran two orchestrators, and
the zero-diff bug (`8034df1`) burned four attempts that produced nothing.

## Derived denominators for later milestones

| Metric | M0 baseline |
| --- | --- |
| PRs opened | 0 |
| PRs merged | 0 |
| Coder cache-read per attempt | ~1.64 M (13.13 M / 8 sessions) |
| Coder share of cache-read | 95.0 % |
| Attempts per task (flagship) | 9 |
| Wall clock (flagship) | 3.09 h |

M3's exit criterion — coder cache-read per attempt ≤ 50% of baseline — means
**≤ ~820 K per attempt**, with review-pass and repro-gate outcomes unchanged.

---

# M0.9 re-snapshot (2026-07-10, post gate-severity fix) — the real M3 denominator

The M0.5 numbers above were captured while the review gate was arithmetically
unpassable, so they measure a system spending tokens against a wall. This
snapshot is the honest denominator: same queries, same DB, after the severity
gate, review continuity, lifetime budget, and the post-PR CI watch landed.

## The one number that mattered — no longer zero

```
attempts_with_pr_url = 4
```

PR #531 exists and has passed the gate three times: attempt 21 (first reviewed
PR in the project's history), attempt 22 (CPS `MethodTooLargeException` fix,
operator-rejected round), attempt 23 (the first fully autonomous post-PR CI
fix round — the WakeWatcher saw the red Jenkins checks, injected the console
log, and resumed the coder with no human in the loop).

## Flagship totals (task `84251cb2`, cumulative)

| Metric | M0.5 | M0.9 |
| --- | --- | --- |
| Attempts | 9 | 23 |
| Review passes | 0 | 4 (attempts 20–23) |
| PRs opened | 0 | 1 (PR #531, draft) |
| Wall clock | 3.09 h | 17.73 h |
| Events | 865 | 2,645 |
| Coder cache-read | 13.13 M | 64.32 M |
| Coder share of cache-read | 95.0 % | **93.1 %** |

Coder cache-read per attempt-with-coder-activity: **~3.06 M** (64.32 M over 21
attempts that ran a coder session). The number *rose* after the gate fix —
passing attempts run longer than attempts dying against a broken gate (the
three passing attempts averaged 5.6 M each). M3's ≤ 50% exit is therefore
**≤ ~1.53 M per attempt** against this snapshot, not the M0.5 one.

The structural fact is unchanged: the coder session is 93% of all burn.
Planner fan-out, reviewer, and aggregator together are under 7% — M3's levers
(transcript diet, repo-map seed, segmentation) all point at the coder.
