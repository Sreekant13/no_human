# Memory lifecycle C: retirement + flood control

Part C of the memory-lifecycle design (research report 2026-08-12). This
document is the operator-facing runbook: how the 45-day auto-archive sweep
works, how supersede-on-confirm works, how to run the one-time flood-source
triage, and how to reverse any of it.

Everything here is **reversible**. Nothing in this feature deletes a row —
"archived" is a flag (`archived = 1`), never a `DELETE`.

## Background

Measured against a `cp` of the operator's live database (never the live
file, which a running server holds open): 487-488 pending proposals vs 53
active rules. The flood source is the per-success templated skill proposal
(`learning/queue.py`'s `_build`, the `AWAITING_APPROVAL`/`DONE` branch) — it
writes a proposal on *every* successful task, carrying no evidence beyond "a
task finished to a reviewable PR", and produced roughly 394 of the
NULL-origin pending rows.

Two structural gaps let the backlog accumulate silently:

1. Unconfirmed proposals never expired — a proposal from six months ago sits
   in the queue exactly like one from this morning.
2. `archived` is a real, honored, reversible state (rejecting a
   batch-regenerated proposal already archives it — `ARCHIVE_ON_REJECT` in
   `learning/queue.py`) but **zero rows were archived** because the only
   producer of archived rows was `reject()`, and the curator that could have
   used it (`nh learnings-curate`) is a manual CLI nobody runs.

## What this feature adds

| Mechanism | Where | Behaviour |
|---|---|---|
| 45-day auto-archive sweep | `Store.archive_unconfirmed_older_than`, `learning/retire.py:sweep_unconfirmed`, `RetirementSweepJob` | Unconfirmed (`confirmed = 0`) `source="proposed"` rows older than 45 days are archived on a daily tick (and once at every server boot — `_last_run = 0.0` at construction). Reversible. |
| Supersede-on-confirm | `Store.supersede_memory`, `LearningQueue.confirm` | Confirming a proposal that is a near-duplicate of an existing active rule (same normalized title+content, same scope) archives the OLD row with `superseded_by` pointing at the new one — oldest match only, bounded. |
| Retire? suggestions | `learning/retire.py:retirement_candidates`, `LearningQueue.retire_candidates`/`retire`, `GET /api/learnings/retire-candidates`, `POST /api/learnings/{id}/retire`, `nh learnings --stale`/`--retire <id>` | Confirmed rules unused (`last_used_at`) for 90+ days surface as suggestions. **Never auto-archived** — a human must click Retire / run `--retire <id>` / POST the endpoint. |
| Flood-source gate | `learning/queue.py`'s `PROPOSE_ON_SUCCESS_DEFAULT` / `LearningQueue(propose_on_success=...)`, `config.yaml`'s `learning.propose_on_success` | The per-success templated proposal only fires when explicitly enabled (default **off**). The code is gated, not deleted — reversible by flipping one config value. |
| One-time triage | `learning/retire.py:is_templated_success_proposal`, `nh learnings --triage-templated [--apply] [--limit]` | Finds and (with `--apply`) archives the pending rows the flood source already wrote, before this change shipped. |

## Running the one-time triage

```bash
nh learnings --triage-templated              # dry run: counts + first 15 titles, zero writes
nh learnings --triage-templated --apply       # archives, writes a JSON receipt
```

The predicate (`is_templated_success_proposal`, `learning/retire.py`) matches
ALL of:

- `confirmed == 0` (an already-confirmed row is never a target),
- `source == "proposed"`,
- `type == "skill"`,
- `origin` NULL/empty (any of the five named origins — review, supervisor,
  history, reply, curator — is evidence-bearing and must never match),
- title starts with `"Approach that worked: "`, or content contains
  `"Consider capturing the successful approach as a reusable skill."`
  (the exact literals `_build` writes),
- `evidence` absent or its `kind` is `"task_outcome"`.

`--apply` archives each matched row with reason `"one-time triage
2026-08-12: templated per-success proposal (flood source), no evidence —
reversible"` and writes `$NO_HUMAN_HOME/receipts/learning-triage-<epoch>.json`
listing the archived ids and the before/after pending counts. That receipt,
plus the archive reason stamped onto each row's content, **is** the repair
record — this repo has no memory-scoped event table, and this feature does
not invent one (`save_events` is task-scoped and needs a task id).

**This was NOT run against the operator's live database as part of this
change.** The report's exact rule set was not available to the implementer
(no match under `docs/`, `*.md`, or history in this repo); the predicate
above is derived from the code's own literals, not from the report. Running
the triage against `~/.no_human/no_human.db` is a human-gated action — copy
the file first (a running server holds the live file open), verify the dry
run's counts and titles look right, then `--apply`.

```bash
cp ~/.no_human/no_human.db /tmp/triage.db
# then point a throwaway config/db at /tmp/triage.db and run the two
# commands above against it before ever touching the live file.
```

## Reversing any of this

Every archive this feature performs is a plain `UPDATE`, reversible with the
matching `UPDATE ... SET archived = 0`:

```sql
-- Undo a sweep or triage archive for one id:
UPDATE memories SET archived = 0 WHERE id = '<id>';

-- Undo a supersede (also clears the pointer):
UPDATE memories SET archived = 0, superseded_by = NULL WHERE id = '<id>';

-- Undo a whole triage run, from its receipt's archived_ids:
UPDATE memories SET archived = 0 WHERE id IN (<ids from the receipt>);
```

Reversing never loses the dedupe key (`file_path` is never touched by any
operation in this feature) — a restored row rejoins `pending()`/`active()`
exactly as if it had never been archived.

## What this feature explicitly does not do

- Auto-archive, auto-retire, or bulk-mutate any `confirmed = 1` row. The only
  door for a confirmed row to become archived is a human's explicit
  `--retire`/`POST /retire` (retirement) or a fresh confirm superseding it
  (supersede) — never a scheduled job.
- Touch the 20 stranded `confirmed=0, source<>'proposed'` rows documented in
  `core/db.py` (`add_memory`'s docstring, ~:2110-2178) — the sweep's
  `source = ?` clause cannot reach them, and cleaning them up is a separate,
  operator-owned decision.
- Delete the per-success proposal code — it is gated, not removed, so
  `propose_on_success: true` restores the old behaviour in one config edit.
