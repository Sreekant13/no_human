#!/usr/bin/env python3
"""One-shot memory-triage runbook (refile of e6cf94c4, rescoped).

PURPOSE. A standalone, throwaway-copy-safe front end over the LANDED memory
lifecycle-C mechanism (`src/no_human/learning/retire.py`): the 45-day
auto-archive sweep of stale UNCONFIRMED proposals
(`Store.archive_unconfirmed_older_than`, wrapped by `sweep_unconfirmed`).
This script does not reimplement that policy -- it only calls it, measures
before/after, and writes a receipt. Nothing here archives a CONFIRMED row;
that guarantee lives in the SQL the landed mechanism runs (`confirmed = 0`
is a literal equality in `archive_unconfirmed_older_than`'s WHERE clause).

MODES (mutually exclusive; exactly one is required -- a bare invocation is a
usage error):

    --dry-run DB_PATH     Read-only. Opens DB_PATH read-only and backs it up
                          (SQLite's `.backup()` API, not `shutil.copy`, so a
                          WAL-mode source is captured consistently) into a
                          throwaway `tempfile.TemporaryDirectory()` copy,
                          which is where `Store.connect()` -- and its
                          `_migrate()`, which can itself ALTER TABLE --
                          actually runs. Nothing is ever written to DB_PATH
                          or its directory, and no repair_event.json is
                          emitted. Prints the per-class counts and the ids
                          that WOULD be archived.

    --execute DB_PATH     Archives the stale-unconfirmed set in place via
                          `sweep_unconfirmed(..., dry_run=False)` and writes
                          `repair_event.json` (fixed filename, in the
                          working directory, unless --event-path overrides
                          it) with before/after per-class counts, start/end
                          timestamps and the policy label. The write is
                          REPLACING, not appending -- copy the artifact off
                          before re-running, or pass --event-path.

--templated-only          Composable with EITHER --dry-run or --execute
                          (not a third mode). Ignores age and --days
                          entirely -- archives (or, under --dry-run,
                          reports) exactly the rows passing
                          `is_templated_success_proposal` among unconfirmed
                          proposals, at ANY age. Policy label
                          `TRIAGE-templated-success-proposal-any-age`.
                          Same reversal shape as the windowed sweep (sets
                          `archived = 1`, never deletes) plus, since this
                          mode's rows may be much older than one run, the
                          emitted `repair_event.json` carries the exact
                          restoring `reversal_sql` for this run's ids
                          rather than relying on a caller to hand-write it.
                          Still governed by the same `--limit` cap and the
                          same live-database guard below.

USAGE:

    python scripts/memory_triage_runbook.py --dry-run /path/to/a/copy.db
    python scripts/memory_triage_runbook.py --execute /path/to/a/copy.db \\
        [--days 45] [--limit 500] [--event-path /tmp/repair_event.json]
    python scripts/memory_triage_runbook.py --templated-only --execute \\
        /path/to/a/copy.db [--limit 500] [--event-path /tmp/repair_event.json]

POLICY REFERENCE: LANDED-lifecycle-C-45day --
`src/no_human/learning/retire.py:sweep_unconfirmed` /
`src/no_human/core/db.py:Store.archive_unconfirmed_older_than`. `--days`
defaults to `no_human.learning.retire.DEFAULT_ARCHIVE_UNCONFIRMED_DAYS`
(imported here, never a literal 45) so a product-side policy change moves
this script with it. `--templated-only` uses a separate policy label,
`TRIAGE-templated-success-proposal-any-age`, and does not read `--days` as
a selector -- see above.

REVERSAL. Every archive here only ever sets `archived = 1` -- nothing is
ever deleted, and `superseded_by` lineage on unrelated rows is left alone.
To undo, per row: `await store.unarchive_memory(id)`, or directly,
`UPDATE memories SET archived = 0, superseded_by = NULL WHERE id IN (...)`.

LIVE-DATABASE GUARD. Both modes refuse (exit 1, before opening anything)
when the given path resolves (symlinks followed) inside the operator's live
`NO_HUMAN_HOME` (env override, default `~/.no_human`). A coder attempt must
never mutate the operator's live `~/.no_human` database -- this script
cannot even read it. LIVE EXECUTION AGAINST THE REAL DATABASE, AND
COMMITTING THE REPAIR EVENT IT PRODUCES, IS PERFORMED BY THE
OPERATOR-DELEGATED SUPERVISING SESSION AFTER THIS SCRIPT LANDS -- no coder
attempt runs `--execute` against anything but a throwaway copy.

EXIT CODES: 0 clean (including "zero rows eligible", a legitimate already-
clean result -- printed explicitly, not silently); 1 refusal/failure
(missing/unreadable/foreign DB, the live-home guard, a bad --days, an event
-write failure); 2 argparse usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from no_human.core.db import SOURCE_PROPOSED, Store
from no_human.learning.retire import (
    DEFAULT_ARCHIVE_UNCONFIRMED_DAYS,
    is_templated_success_proposal,
    sweep_unconfirmed,
)

POLICY_LABEL = "LANDED-lifecycle-C-45day"
TEMPLATED_POLICY_LABEL = "TRIAGE-templated-success-proposal-any-age"
TEMPLATED_ARCHIVE_REASON = (
    "templated per-success proposal (flood source), no evidence -- "
    "reversible"
)

# Stable print/iteration order for the seven documented classes.
CLASS_ORDER = (
    "total",
    "confirmed_active",
    "unconfirmed_pending",
    "unconfirmed_stale_over_window",
    "templated_success_proposal",
    "archived",
    "superseded",
)

_TRUNCATE_PRINT_N = 15


class DbRefusal(Exception):
    """Raised by `validate_db_path` -- a path that fails the live-DB guard
    or basic sanity checks. Nothing is opened for real before this is
    resolved; the exception message names the exact refusal."""


def _live_home() -> Path:
    raw = os.environ.get("NO_HUMAN_HOME")
    base = Path(raw) if raw else (Path.home() / ".no_human")
    return base.expanduser().resolve()


def validate_db_path(raw_path: str) -> Path:
    """Fail-closed gate every mode runs before opening *raw_path* for real.

    Refuses (raises `DbRefusal`, opens nothing):
    - the path resolves (symlinks followed -- `Path.resolve()`) inside the
      live `NO_HUMAN_HOME` (env override, default `~/.no_human`) -- the
      operator's live database (intake answer #2).
    - the path does not exist, or is not a regular file.
    - the file is not a readable SQLite database, or has no `memories`
      table -- an absent/unreadable/foreign DB is a FAILURE here, not a
      zero-count "clean" run.
    """
    target = Path(raw_path).expanduser()
    resolved = target.resolve()
    home = _live_home()
    if resolved == home or home in resolved.parents:
        raise DbRefusal(
            f"refusing: {resolved} resolves inside the live NO_HUMAN_HOME "
            f"({home}) -- this script must never touch the operator's live "
            f"database; point it at a throwaway copy instead")
    if not resolved.exists():
        raise DbRefusal(f"refusing: {resolved} does not exist")
    if not resolved.is_file():
        raise DbRefusal(f"refusing: {resolved} is not a regular file")
    try:
        # `immutable=1`, not just `mode=ro`: see `_backup_to_temp_copy` for
        # why a plain read-only connection to a WAL-mode database still
        # creates -shm/-wal next to it the moment it is read -- this sanity
        # check must be as write-free as the dry-run path it gates.
        con = sqlite3.connect(
            f"file:{resolved}?mode=ro&immutable=1", uri=True)
        try:
            con.execute("PRAGMA schema_version")
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'memories'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise DbRefusal(
            f"refusing: {resolved} is not a readable SQLite database "
            f"({exc})") from exc
    if row is None:
        raise DbRefusal(
            f"refusing: {resolved} has no 'memories' table -- not a "
            f"no_human database")
    return resolved


async def _scalar(store: Store, sql: str, params: tuple = ()) -> int:
    row = await store._fetchone(sql, params)
    return int(row["n"]) if row else 0


async def class_counts(store: Store, *, days: int) -> dict:
    """The seven documented classes, each a predicate over `memories`. Used
    by BOTH modes so before/after are literally the same measurement.

    - `confirmed_active`: `confirmed = 1 AND (archived IS NULL OR = 0)`.
    - `unconfirmed_pending`: unconfirmed, `SOURCE_PROPOSED`, unarchived --
      the queue-visibility contract's own pending set.
    - `unconfirmed_stale_over_window`: as above, plus the same SQL-side
      `datetime(created_at) <= datetime('now', ?)` cutoff
      `archive_unconfirmed_older_than` uses -- the sweep-eligible set.
    - `templated_success_proposal`: rows passing `is_templated_success_
      proposal` (the killed per-success template class), read via
      `list_memories` rather than raw SQL since the predicate is Python.
    - `archived`, `superseded`, `total`: so "nothing was deleted" is
      checkable from the event alone (`after["total"] == before["total"]`).
    """
    total = await _scalar(store, "SELECT COUNT(*) AS n FROM memories")
    confirmed_active = await _scalar(
        store,
        "SELECT COUNT(*) AS n FROM memories WHERE confirmed = 1 "
        "AND (archived IS NULL OR archived = 0)",
    )
    unconfirmed_pending = await _scalar(
        store,
        "SELECT COUNT(*) AS n FROM memories WHERE confirmed = 0 "
        "AND source = ? AND (archived IS NULL OR archived = 0)",
        (SOURCE_PROPOSED,),
    )
    unconfirmed_stale_over_window = await _scalar(
        store,
        "SELECT COUNT(*) AS n FROM memories WHERE confirmed = 0 "
        "AND source = ? AND (archived IS NULL OR archived = 0) "
        "AND created_at IS NOT NULL AND created_at != '' "
        "AND datetime(created_at) IS NOT NULL "
        "AND datetime(created_at) <= datetime('now', ?)",
        (SOURCE_PROPOSED, f"-{days} days"),
    )
    archived = await _scalar(
        store, "SELECT COUNT(*) AS n FROM memories WHERE archived = 1")
    superseded = await _scalar(
        store,
        "SELECT COUNT(*) AS n FROM memories WHERE superseded_by IS NOT NULL",
    )
    unconfirmed_rows = await store.list_memories(
        confirmed=False, include_archived=False)
    templated_success_proposal = sum(
        1 for row in unconfirmed_rows if is_templated_success_proposal(row))
    return {
        "total": total,
        "confirmed_active": confirmed_active,
        "unconfirmed_pending": unconfirmed_pending,
        "unconfirmed_stale_over_window": unconfirmed_stale_over_window,
        "templated_success_proposal": templated_success_proposal,
        "archived": archived,
        "superseded": superseded,
    }


async def templated_targets(store: Store, *, limit: int) -> list[dict]:
    """The exact set `--templated-only` archives: unconfirmed rows passing
    `is_templated_success_proposal`, at ANY age. Reuses the identical
    `list_memories(confirmed=False, include_archived=False)` call
    `class_counts` uses for its `templated_success_proposal` count, so the
    reported count and the actual archive target set can never diverge.

    Deterministic sort (oldest `created_at` first, id tiebreak) so
    `--limit` truncation is reproducible run to run."""
    rows = await store.list_memories(confirmed=False, include_archived=False)
    matched = [row for row in rows if is_templated_success_proposal(row)]
    matched.sort(key=lambda row: (str(row.get("created_at") or ""), row["id"]))
    return matched[:limit]


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def _reversal_sql(ids: list[str], reason: str) -> str:
    """Single executable statement undoing `archive_memory`'s writes for
    *ids* -- both the `archived` flag and the `\\n\\n[archived: reason]`
    content suffix it appended (`core/db.py:2721`). `Store.unarchive_memory`
    resets the flag/`superseded_by` and deliberately leaves the suffix as an
    audit trail; this reversal goes further because full-state restoration
    is the point of a repair event. `-- nothing to reverse` (a comment,
    still executable) when *ids* is empty."""
    if not ids:
        return "-- nothing to reverse"
    suffix = f"\n\n[archived: {reason}]"
    id_list = ", ".join(f"'{_sql_quote(mem_id)}'" for mem_id in ids)
    return (
        "UPDATE memories SET archived = 0, superseded_by = NULL, "
        f"content = replace(content, '{_sql_quote(suffix)}', '') "
        f"WHERE id IN ({id_list});"
    )


def _backup_to_temp_copy(source: Path, dest: Path) -> None:
    """Copy *source* into *dest* via SQLite's backup API rather than
    `shutil.copy` -- captures a WAL-mode source consistently, and creates no
    -wal/-shm sidecar next to SOURCE (the copy's own sidecars, if any, live
    only in the caller's temp dir).

    `immutable=1`, not just `mode=ro`: measured directly against a WAL-mode
    database, a plain read-only connection still creates -shm/-wal next to
    SOURCE the moment it is read (SQLite needs them to serve a consistent
    WAL-mode read even when it will never write). `immutable=1` tells
    SQLite the file will not change for the connection's lifetime, which
    skips that bookkeeping entirely -- verified empty afterwards. This
    script never reaches this path against the live database (the guard
    above refuses first), so the one thing `immutable` gives up -- seeing
    frames from a WAL that is not yet checkpointed into SOURCE -- is not a
    risk here: a throwaway copy the caller handed us has no other writer.
    """
    src_con = sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True)
    dest_con = sqlite3.connect(dest)
    try:
        src_con.backup(dest_con)
    finally:
        dest_con.close()
        src_con.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _dry_run_async(
    path: Path, *, days: int, limit: int, templated_only: bool = False,
) -> int:
    with tempfile.TemporaryDirectory(prefix="nh-triage-dryrun-") as tmp_dir:
        copy_path = Path(tmp_dir) / "dry_run_copy.db"
        _backup_to_temp_copy(path, copy_path)
        store = await Store(copy_path).connect()
        try:
            counts = await class_counts(store, days=days)
            if templated_only:
                targets = await templated_targets(store, limit=limit)
                archived_ids = [row["id"] for row in targets]
            else:
                report = await sweep_unconfirmed(
                    store, days=days, limit=limit, dry_run=True)
                archived_ids = report.archived_ids
        finally:
            await store.close()
    if templated_only:
        _print_templated_dry_run(
            path, days=days, limit=limit, counts=counts,
            archived_ids=archived_ids)
    else:
        _print_dry_run(path, days=days, limit=limit, counts=counts,
                        archived_ids=archived_ids)
    return 0


def _print_class_counts(counts: dict) -> None:
    print("per-class counts:")
    for cls in CLASS_ORDER:
        print(f"  {cls}: {counts[cls]}")


def _print_dry_run(
    path: Path, *, days: int, limit: int, counts: dict,
    archived_ids: list,
) -> None:
    print(f"DRY RUN -- database: {path}")
    print("(opened read-only via a throwaway temp copy; nothing written "
          "to the source path or its directory)")
    print(f"policy: {POLICY_LABEL} (days={days}, limit={limit})")
    print()
    _print_class_counts(counts)
    print()
    print(f"would archive {len(archived_ids)} unconfirmed proposal(s):")
    for mem_id in archived_ids[:_TRUNCATE_PRINT_N]:
        print(f"  {mem_id[:8]}")
    if len(archived_ids) > _TRUNCATE_PRINT_N:
        print(f"  ... and {len(archived_ids) - _TRUNCATE_PRINT_N} more")
    if len(archived_ids) >= limit:
        print(f"WARNING: hit --limit={limit} -- more may still be "
              f"eligible; re-run (or raise --limit) to continue")
    print()
    print("reversal, per archived id: "
          "UPDATE memories SET archived = 0, superseded_by = NULL "
          "WHERE id = '<id>';  -- or `await store.unarchive_memory(id)`")
    print()
    print("DRY RUN -- nothing was written")


def _print_templated_dry_run(
    path: Path, *, days: int, limit: int, counts: dict,
    archived_ids: list,
) -> None:
    print(f"DRY RUN (--templated-only) -- database: {path}")
    print("(opened read-only via a throwaway temp copy; nothing written "
          "to the source path or its directory)")
    print(f"policy: {TEMPLATED_POLICY_LABEL} (limit={limit})")
    print(f"days={days} (reporting only; not a selector in "
          "--templated-only)")
    print()
    _print_class_counts(counts)
    print()
    print(f"Identified {len(archived_ids)} templated success proposals "
          "ready for archival")
    for mem_id in archived_ids[:_TRUNCATE_PRINT_N]:
        print(f"  {mem_id[:8]}")
    if len(archived_ids) > _TRUNCATE_PRINT_N:
        print(f"  ... and {len(archived_ids) - _TRUNCATE_PRINT_N} more")
    if len(archived_ids) >= limit:
        print(f"WARNING: hit --limit={limit} -- more may still be "
              f"eligible; re-run (or raise --limit) to continue")
    print()
    print("reversal SQL for this run's ids: "
          + _reversal_sql(archived_ids, TEMPLATED_ARCHIVE_REASON))
    print()
    print("DRY RUN -- nothing was written")


def _write_event_atomic(event_path: Path, event: dict) -> None:
    tmp_path = event_path.with_name(event_path.name + ".tmp")
    tmp_path.write_text(json.dumps(event, indent=2, sort_keys=True))
    os.replace(tmp_path, event_path)


def _print_execute_summary(
    path: Path, *, before: dict, after: dict, archived_ids: list,
    event_path: Path,
) -> None:
    print(f"EXECUTE -- database: {path}")
    print(f"policy: {POLICY_LABEL}")
    print()
    print("before:")
    _print_class_counts(before)
    print("after:")
    _print_class_counts(after)
    print()
    if archived_ids:
        print(f"archived {len(archived_ids)} row(s)")
    else:
        print("archived 0 rows -- already clean")
    print(f"repair event written: {event_path}")


def _print_templated_execute_summary(
    path: Path, *, before: dict, after: dict, archived_ids: list,
    refused_ids: list, limit: int, event_path: Path,
) -> None:
    print(f"EXECUTE (--templated-only) -- database: {path}")
    print(f"policy: {TEMPLATED_POLICY_LABEL}")
    print()
    print("before:")
    _print_class_counts(before)
    print("after:")
    _print_class_counts(after)
    print()
    if archived_ids:
        print(f"archived {len(archived_ids)} row(s)")
    else:
        print("archived 0 rows -- already clean")
    if refused_ids:
        print(f"WARNING: {len(refused_ids)} target(s) refused by "
              f"archive_memory (legacy NULL archived) -- ids: "
              f"{', '.join(refused_ids)}")
    if len(archived_ids) >= limit:
        print(f"WARNING: hit --limit={limit} -- more may still be "
              f"eligible; re-run (or raise --limit) to continue")
    print(f"repair event written: {event_path}")


async def _execute_async(
    path: Path, *, days: int, limit: int, event_path: Path,
    templated_only: bool = False,
) -> int:
    start = _now_iso()
    store = await Store(path).connect()
    try:
        before = await class_counts(store, days=days)
        if templated_only:
            targets = await templated_targets(store, limit=limit)
            reason = TEMPLATED_ARCHIVE_REASON
            archived_ids: list[str] = []
            refused_ids: list[str] = []
            for row in targets:
                ok = await store.archive_memory(row["id"], reason)
                (archived_ids if ok else refused_ids).append(row["id"])
            truncated = len(targets) >= limit
        else:
            report = await sweep_unconfirmed(
                store, days=days, limit=limit, dry_run=False)
            archived_ids, refused_ids = report.archived_ids, []
            reason = report.reason
            truncated = len(archived_ids) >= limit
        after = await class_counts(store, days=days)
    finally:
        await store.close()
    end = _now_iso()
    event = {
        "before_counts": before,
        "after_counts": after,
        "timestamps": {"start": start, "end": end},
        "policy": TEMPLATED_POLICY_LABEL if templated_only else POLICY_LABEL,
        "archived_ids": archived_ids,
        "reason": reason,
        "days": days,
        "limit": limit,
        "db_path": str(path),
        "truncated": truncated,
    }
    if templated_only:
        event["mode"] = "templated-only"
        event["refused_ids"] = refused_ids
        event["reversal_sql"] = _reversal_sql(archived_ids, reason)
    _write_event_atomic(event_path, event)
    if templated_only:
        _print_templated_execute_summary(
            path, before=before, after=after, archived_ids=archived_ids,
            refused_ids=refused_ids, limit=limit, event_path=event_path)
    else:
        _print_execute_summary(
            path, before=before, after=after, archived_ids=archived_ids,
            event_path=event_path)
    if templated_only and refused_ids:
        return 1
    return 0


def run_dry_run(
    path: Path, *, days: int, limit: int, templated_only: bool = False,
) -> int:
    return asyncio.run(_dry_run_async(
        path, days=days, limit=limit, templated_only=templated_only))


def run_execute(
    path: Path, *, days: int, limit: int, event_path: Path,
    templated_only: bool = False,
) -> int:
    return asyncio.run(_execute_async(
        path, days=days, limit=limit, event_path=event_path,
        templated_only=templated_only))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_triage_runbook.py",
        description=(
            "One-shot memory-triage runbook over the landed lifecycle-C "
            "45-day auto-archive sweep. See the module docstring for the "
            "full usage and policy reference."),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", metavar="DB_PATH",
        help="Read-only: print per-class counts and what would be "
             "archived. Zero database writes.")
    mode.add_argument(
        "--execute", metavar="DB_PATH",
        help="Archive stale unconfirmed proposals via the landed 45-day "
             "sweep and emit repair_event.json.")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_ARCHIVE_UNCONFIRMED_DAYS,
        help="Sweep window in days (default: the landed policy default, "
             f"{DEFAULT_ARCHIVE_UNCONFIRMED_DAYS}).")
    parser.add_argument(
        "--limit", type=int, default=500,
        help="Cap on rows one sweep archives (default: 500).")
    parser.add_argument(
        "--event-path", type=Path, default=None,
        help="Override the repair-event JSON path (default: "
             "./repair_event.json in the working directory). --execute "
             "only.")
    parser.add_argument(
        "--templated-only", dest="templated_only", action="store_true",
        help="Archive exactly the rows passing is_templated_success_"
             "proposal among unconfirmed proposals, at ANY age (the "
             "45-day window and --days do not apply). Composable with "
             "--dry-run/--execute; --limit still caps one run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.days < 1:
        print(f"refusing: --days must be >= 1, got {args.days}",
              file=sys.stderr)
        return 1
    raw_path = args.dry_run if args.dry_run is not None else args.execute
    try:
        path = validate_db_path(raw_path)
    except DbRefusal as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.dry_run is not None:
        return run_dry_run(path, days=args.days, limit=args.limit,
                            templated_only=args.templated_only)
    event_path = (
        args.event_path if args.event_path is not None
        else Path.cwd() / "repair_event.json")
    return run_execute(
        path, days=args.days, limit=args.limit, event_path=event_path,
        templated_only=args.templated_only)


if __name__ == "__main__":
    sys.exit(main())
