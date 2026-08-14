"""Builder for `testdata/memory_triage_fixture.db`, the fixture
`tests/test_memory_triage_runbook.py` runs against.

Built through `Store` + `add_memory`/`archive_memory`/`supersede_memory`, not
hand-written DDL, so the schema is the product's real one (migrations
included) -- the same reasoning `tests/test_memory_retirement.py` and every
other Store-backed test fixture in this repo follows.

ROWS IT WRITES (one of each, titled so a test can find it without depending
on the random id `add_memory` assigns):

  - 2 CONFIRMED-ACTIVE rules (`TITLE_CONFIRMED_1`, `TITLE_CONFIRMED_2`).
  - 1 confirmed rule already superseded by `TITLE_CONFIRMED_2`
    (`TITLE_SUPERSEDED_OLD`, via `supersede_memory` -- archived, carries a
    non-NULL `superseded_by`). Not counted in "confirmed-active": archived.
  - 3 unconfirmed `SOURCE_PROPOSED` proposals aged past the sweep window
    (`TITLE_STALE_PREFIX` 1-3) -- the sweep-eligible set.
  - 2 unconfirmed `SOURCE_PROPOSED` proposals aged inside the window
    (`TITLE_FRESH_PREFIX` 1-2) -- must never be swept.
  - 1 unconfirmed `SOURCE_PROPOSED` proposal already archived before the
    sweep runs (`TITLE_ARCHIVED`) -- excluded by the sweep's own `archived
    IS NULL OR archived = 0` clause, so it must stay untouched (archived
    stays 1, it does not get counted a second time).
  - 2 templated per-success proposals matching `is_templated_success_
    proposal` (`TITLE_TEMPLATED_PREFIX` 1-2, title-prefixed exactly as the
    killed flood source wrote them) -- aged fresh, so class overlap with the
    stale set is never ambiguous in a test.
  - 1 unconfirmed row whose `source` is NOT `SOURCE_PROPOSED`
    (`TITLE_NON_PROPOSED_SOURCE`) -- must never be swept (the sweep's `source
    = ?` clause excludes it) and must never even ATTEMPT to go through
    `add_memory`: that method raises `ValueError` for exactly this
    `confirmed=False, source != SOURCE_PROPOSED` combination
    (`core/db.py:2313`, the guard closing the door on legacy "stranded
    rows" -- see that docstring). This row is therefore the one write in
    this builder that goes straight through `store.db.execute`, mirroring
    how the real stranded rows got there.
  - 1 unconfirmed `SOURCE_PROPOSED` row with an unparseable `created_at`
    (`TITLE_UNPARSEABLE`) -- must be skipped, fail-closed, same as
    `tests/test_memory_retirement.py::test_sweep_skips_null_created_at`.

THE TIME-BOMB, and its mitigation. A static binary fixture freezes absolute
`created_at` values, but the sweep policy compares against `datetime('now')`
-- so a "fresh" row baked in at build time would silently age past the
window and flip test results months after this file was committed. The
committed `.db` is therefore only the *shape* fixture (ids, types, sources,
titles, the supersede pointer); every AGE-SENSITIVE row is re-stamped
relative to `now` by `stamp_ages()` on the WORKING COPY inside each test --
see `tests/test_memory_triage_runbook.py`'s `work_db` fixture. Ages are
asserted by the tests, never assumed from the committed file's baked-in
timestamps.

REGENERATING THE FIXTURE (only needed if this builder's shape changes):

    uv run python tests/_memory_triage_fixture.py testdata/memory_triage_fixture.db
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from no_human.core.db import Store
from no_human.learning import TYPE_RULE, TYPE_SKILL

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_PATH = REPO_ROOT / "testdata" / "memory_triage_fixture.db"

TITLE_CONFIRMED_1 = "confirmed active rule one"
TITLE_CONFIRMED_2 = "confirmed active rule two"
TITLE_SUPERSEDED_OLD = "confirmed rule superseded by rule two"
TITLE_STALE_PREFIX = "stale unconfirmed proposal "
TITLE_FRESH_PREFIX = "fresh unconfirmed proposal "
TITLE_ARCHIVED = "already archived unconfirmed proposal"
TITLE_TEMPLATED_PREFIX = "Approach that worked: templated proposal "
TITLE_NON_PROPOSED_SOURCE = "legacy stranded unconfirmed row"
TITLE_UNPARSEABLE = "unconfirmed proposal with unparseable created_at"

STALE_COUNT = 3
FRESH_COUNT = 2
TEMPLATED_COUNT = 2

# Well past / well inside `DEFAULT_ARCHIVE_UNCONFIRMED_DAYS` (45).
STALE_AGE_DAYS = 60
FRESH_AGE_DAYS = 5

_UNPARSEABLE_CREATED_AT = "not-a-real-timestamp"
_NON_PROPOSED_SOURCE = "reply"  # a real, non-"proposed" `source` literal
_TEMPLATE_CONTENT_MARKER = (
    "Consider capturing the successful approach as a reusable skill."
)


def default_ages() -> dict[str, int]:
    """The `stamp_ages()` argument every test (and this builder, at build
    time) uses -- every AGE-SENSITIVE row, mapped to how many days old its
    `created_at` should read relative to `now`. Deliberately excludes
    `TITLE_NON_PROPOSED_SOURCE` (age is irrelevant -- excluded by `source`
    regardless) and `TITLE_UNPARSEABLE` (re-stamping it would erase the
    unparseable value under test)."""
    ages = {TITLE_ARCHIVED: STALE_AGE_DAYS}
    for i in range(1, STALE_COUNT + 1):
        ages[f"{TITLE_STALE_PREFIX}{i}"] = STALE_AGE_DAYS
    for i in range(1, FRESH_COUNT + 1):
        ages[f"{TITLE_FRESH_PREFIX}{i}"] = FRESH_AGE_DAYS
    for i in range(1, TEMPLATED_COUNT + 1):
        ages[f"{TITLE_TEMPLATED_PREFIX}{i}"] = FRESH_AGE_DAYS
    return ages


def stamp_ages(db_path: Path, ages: dict[str, int]) -> None:
    """Re-stamp `created_at` (SQLite `datetime()`-parseable, space-separated
    -- the column DEFAULT's format) for the rows named in *ages*, matched by
    exact `title`, to `days` days before `now`. Plain synchronous `sqlite3`
    against the working copy -- no `Store`/migration involved, since the
    schema already exists on a copy of the committed fixture.

    Raises `ValueError` if a title in *ages* matches no row (a typo here
    would otherwise silently leave the fixture's baked-in timestamp in
    place and pass for the wrong reason)."""
    con = sqlite3.connect(db_path)
    try:
        for title, days in ages.items():
            when = (datetime.now(timezone.utc)
                     - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cur = con.execute(
                "UPDATE memories SET created_at = ? WHERE title = ?",
                (when, title))
            if cur.rowcount == 0:
                raise ValueError(
                    f"stamp_ages: no row titled {title!r} in {db_path}")
        con.commit()
    finally:
        con.close()


async def build_fixture(path: Path) -> None:
    """Write the fixture database at *path* (overwriting it if present)."""
    if path.exists():
        path.unlink()
    store = await Store(path).connect()
    try:
        confirmed_1 = await store.add_memory(
            mem_type=TYPE_RULE, title=TITLE_CONFIRMED_1,
            content="Always run the scoped test file before committing.",
            confirmed=True)
        confirmed_2 = await store.add_memory(
            mem_type=TYPE_RULE, title=TITLE_CONFIRMED_2,
            content="Keep functions under fifty lines.", confirmed=True)
        superseded_old = await store.add_memory(
            mem_type=TYPE_RULE, title=TITLE_SUPERSEDED_OLD,
            content="An older phrasing of the same rule as rule two.",
            confirmed=True)
        superseded_ok = await store.supersede_memory(
            superseded_old, confirmed_2,
            reason="fixture: duplicate of rule two")
        if not superseded_ok:
            raise RuntimeError("fixture build: supersede_memory refused")

        for i in range(1, STALE_COUNT + 1):
            await store.add_memory(
                mem_type=TYPE_SKILL, title=f"{TITLE_STALE_PREFIX}{i}",
                content=f"Stale proposal body {i}.", confirmed=False)

        for i in range(1, FRESH_COUNT + 1):
            await store.add_memory(
                mem_type=TYPE_SKILL, title=f"{TITLE_FRESH_PREFIX}{i}",
                content=f"Fresh proposal body {i}.", confirmed=False)

        archived_id = await store.add_memory(
            mem_type=TYPE_SKILL, title=TITLE_ARCHIVED,
            content="Already archived before the sweep ever runs.",
            confirmed=False)
        archived_ok = await store.archive_memory(
            archived_id, "fixture: pre-archived")
        if not archived_ok:
            raise RuntimeError("fixture build: archive_memory refused")

        for i in range(1, TEMPLATED_COUNT + 1):
            await store.add_memory(
                mem_type=TYPE_SKILL, title=f"{TITLE_TEMPLATED_PREFIX}{i}",
                content=_TEMPLATE_CONTENT_MARKER, confirmed=False)

        # Legacy stranded row -- add_memory refuses this exact combination
        # (confirmed=False, source != SOURCE_PROPOSED); see the module
        # docstring. Written directly, mirroring the real stranded shape.
        stranded_id = uuid.uuid4().hex
        await store.db.execute(
            "INSERT INTO memories "
            "(id, type, title, content, tags, source, confirmed) "
            "VALUES (?, ?, ?, ?, '[]', ?, 0)",
            (stranded_id, TYPE_SKILL, TITLE_NON_PROPOSED_SOURCE,
             "A legacy row with a non-proposed source.",
             _NON_PROPOSED_SOURCE))
        await store.db.commit()

        unparseable_id = await store.add_memory(
            mem_type=TYPE_SKILL, title=TITLE_UNPARSEABLE,
            content="This row's created_at cannot be parsed.",
            confirmed=False)
        await store.db.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (_UNPARSEABLE_CREATED_AT, unparseable_id))
        await store.db.commit()

        # Consolidate WAL into the main file before anything reads this
        # file read-only -- see `memory_triage_runbook._backup_to_temp_copy`
        # for why an un-checkpointed WAL-mode file forces sidecar creation
        # on the very next read-only open.
        await store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await store.close()

    # Bake in plausible ages at build time too -- not load-bearing (every
    # test re-stamps via stamp_ages on its own working copy) but keeps a
    # freshly-regenerated fixture inspectable on its own.
    stamp_ages(path, default_ages())
    # stamp_ages runs its own connection after Store.close(); checkpoint
    # once more so the committed file carries no stray -wal/-shm siblings.
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


TITLE_T_TEMPLATED_PREFIX = "Approach that worked: triage templated "
TITLE_T_ORGANIC_PREFIX = "organic unconfirmed proposal "
TITLE_T_CONFIRMED_PREFIX = "confirmed active rule "

T_TEMPLATED_COUNT = 10
T_ORGANIC_COUNT = 5
T_CONFIRMED_COUNT = 3

# The templated rows are FRESH (well inside the 45-day window) -- this is
# what proves "--templated-only archives regardless of age". The organic
# rows are STALE (well past the window) -- the strong control: eligible for
# the windowed sweep, but --templated-only must leave them alone regardless.
T_TEMPLATED_AGE_DAYS = 5
T_ORGANIC_AGE_DAYS = 60


def templated_only_ages() -> dict[str, int]:
    """The `stamp_ages()` argument for `build_templated_only_fixture`'s
    age-sensitive rows -- mirrors `default_ages()` for the other fixture."""
    ages = {}
    for i in range(1, T_TEMPLATED_COUNT + 1):
        ages[f"{TITLE_T_TEMPLATED_PREFIX}{i}"] = T_TEMPLATED_AGE_DAYS
    for i in range(1, T_ORGANIC_COUNT + 1):
        ages[f"{TITLE_T_ORGANIC_PREFIX}{i}"] = T_ORGANIC_AGE_DAYS
    return ages


async def build_templated_only_fixture(path: Path) -> None:
    """Purpose-built fixture for `--templated-only` (distinct from
    `build_fixture`/the committed `.db` -- written fresh to *path* by every
    test, never committed as a binary):

      - `T_TEMPLATED_COUNT` templated per-success proposals matching
        `is_templated_success_proposal`, aged FRESH -- the class
        `--templated-only` must archive regardless of age.
      - `T_ORGANIC_COUNT` organic unconfirmed proposals the predicate
        rejects, aged STALE -- the control that also proves the windowed
        sweep never ran (they are window-eligible but must be untouched).
      - `T_CONFIRMED_COUNT` confirmed, unarchived active rules -- the
        control proving no confirmed row is ever touched.
    """
    if path.exists():
        path.unlink()
    store = await Store(path).connect()
    try:
        for i in range(1, T_TEMPLATED_COUNT + 1):
            await store.add_memory(
                mem_type=TYPE_SKILL, title=f"{TITLE_T_TEMPLATED_PREFIX}{i}",
                content=_TEMPLATE_CONTENT_MARKER, confirmed=False)

        for i in range(1, T_ORGANIC_COUNT + 1):
            await store.add_memory(
                mem_type=TYPE_SKILL, title=f"{TITLE_T_ORGANIC_PREFIX}{i}",
                content=f"Organic unconfirmed proposal body {i}.",
                confirmed=False)

        for i in range(1, T_CONFIRMED_COUNT + 1):
            await store.add_memory(
                mem_type=TYPE_RULE, title=f"{TITLE_T_CONFIRMED_PREFIX}{i}",
                content=f"Confirmed active rule body {i}.", confirmed=True)

        # Consolidate WAL into the main file before anything reads this
        # file read-only -- same reasoning as `build_fixture` above.
        await store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await store.close()

    stamp_ages(path, templated_only_ages())
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURE_PATH
    asyncio.run(build_fixture(target))
    print(f"wrote {target}")
