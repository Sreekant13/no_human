"""Where a remote rule lands — and, far more importantly, where it does NOT.

**T0: a remote rule never enters the ``memories`` table.** This is not tidiness,
it is the load-bearing control of the whole client. ``memories`` is wired to
three things a remote author must not reach:

  * ``Orchestrator._format_active_memories()`` feeds ONE string into the coder,
    the supervisor AND the reviewer, and ``review/reviewer.py`` turns it into a
    numbered ``RULE ADHERENCE`` review pass — so a row in ``memories`` is a
    supply chain into the independent review gate, which is the single thing
    this product's trustworthiness rests on;
  * ``learning/triggers.py`` injects an UNTAGGED memory unconditionally, and a
    malformed tag list degrades to untagged;
  * the coder is told it may run ``nh recall`` from Bash, and ``nh recall``
    lists memories without a ``confirmed=`` filter.

Writing remote rules into a table of their own makes every one of those an
explicit act somebody has to write, rather than a default nobody noticed.

WHY THESE TABLES ARE CREATED IN PYTHON AND NOT AS A ``migrations/*.sql`` FILE.
They live in the product's existing SQLite database — invariant L2 forbids a
second store — but they are created here, by this package, on first enabled use.
``core/db.py`` already documents the same runtime-DDL pattern for the same
reason, and it keeps ``src/no_human/brain/`` deletable: remove the directory and
the product still runs, with three unused tables it will never read again.

Plain ``sqlite3``, not the product's async ``Store``: this is called from a
foreground CLI command and from synchronous prompt assembly, and pulling an
event loop into either would be a new moving part for no gain.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS brain_rules (
        rule_id          TEXT PRIMARY KEY,
        version          INTEGER NOT NULL,
        scope            TEXT NOT NULL DEFAULT 'GLOBAL',
        type             TEXT NOT NULL DEFAULT 'rule',
        title            TEXT NOT NULL DEFAULT '',
        content          TEXT NOT NULL DEFAULT '',
        visibility       TEXT NOT NULL DEFAULT 'coder',
        status           TEXT NOT NULL DEFAULT 'active',
        approvals        TEXT NOT NULL DEFAULT '[]',
        manifest_version INTEGER,
        -- 1 only when this row was the verified SUBJECT of a signed manifest
        -- AND passed every screen. Everything else is stored inert, with the
        -- reason, so `nh brain list` can say why nothing was injected.
        injectable       INTEGER NOT NULL DEFAULT 0,
        refused_reason   TEXT,
        synced_at        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brain_manifests (
        version     INTEGER PRIMARY KEY,
        sha256      TEXT NOT NULL,
        prev_sha256 TEXT,
        envelope    TEXT NOT NULL,
        verified_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brain_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # A local veto the cloud cannot clear. Separate from brain_rules on purpose:
    # a resync wipes rules, and a block must survive one.
    """
    CREATE TABLE IF NOT EXISTS brain_blocks (
        rule_id    TEXT PRIMARY KEY,
        blocked_at TEXT NOT NULL
    )
    """,
)

TRUST_OK = "ok"
TRUST_UNTRUSTED = "untrusted"

#: Two timeouts, because there are two callers and only one of them is allowed
#: to wait. `nh brain sync` and `nh brain list` are foreground commands a human
#: typed, so they may block for a contended write lock. A TASK may not: L5 says
#: "no task may block, slow, or fail because of the brain", and a task that
#: stalls ten seconds behind another writer has been slowed by the brain
#: whatever it eventually returns.
#:
#: This shipped as ONE timeout of 10 seconds on a connect that also ran four
#: `CREATE TABLE IF NOT EXISTS` statements — DDL, which needs a WRITE lock —
#: against the product's live database, once per attempt and once per
#: implement-prompt build. Measured under another connection holding
#: `BEGIN EXCLUSIVE`: 10.43s, and an empty block at the end of it. The reason it
#: shipped is that the L5 tests black-holed the NETWORK and never the DATABASE.
WRITE_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 0.25


@dataclass(frozen=True)
class BrainRule:
    rule_id: str
    version: int
    title: str
    content: str
    visibility: str
    status: str
    approvals: list
    manifest_version: int | None
    injectable: bool
    refused_reason: str | None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open the product's database and ensure the brain tables exist.

    THE WRITE PATH. It issues DDL, so it takes a write lock, so it is only ever
    called from a foreground `nh brain ...` command a human typed. A task's path
    calls ``connect_readonly`` instead — see the timeout constants above.
    """
    conn = sqlite3.connect(str(db_path), timeout=WRITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.commit()
    return conn


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """THE TASK PATH. No DDL, no writes, and a quarter-second of patience.

    Three separate properties, and each one is load-bearing for L5:

      * **no DDL.** Creating the tables is the sync command's job. A task reads
        what a sync left behind, or it reads nothing; it never migrates the
        product's live database on the coder's critical path.
      * **`PRAGMA query_only`.** Structural, not a promise: a future edit that
        adds a write here fails loudly instead of quietly taking a write lock
        in front of a running task.
      * **a short timeout.** If another writer holds the lock, the honest answer
        for a task is "no remote rules", delivered immediately. Waiting cannot
        produce a better answer, and the closed state IS the current product.

    Raises rather than returning None — every caller is already inside the
    total ``except Exception`` that makes an absent brain indistinguishable from
    a deleted one.
    """
    conn = sqlite3.connect(str(db_path), timeout=READ_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    # A connection-level setting; it touches no page and takes no lock.
    conn.execute("PRAGMA query_only = ON")
    return conn


# --- state ------------------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM brain_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO brain_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)))
    conn.commit()


def watermark(conn: sqlite3.Connection) -> int:
    try:
        return int(get_state(conn, "watermark", 0) or 0)
    except (TypeError, ValueError):
        return 0


def trust(conn: sqlite3.Connection) -> str:
    return str(get_state(conn, "trust", TRUST_OK) or TRUST_OK)


def mark_untrusted(conn: sqlite3.Connection, reason: str) -> None:
    """Quarantine EVERY remote rule, permanently, until a human resets trust.

    Not just the offending version: a chain that has been tampered with says
    nothing reliable about the entries BEFORE the break either. There is no
    automatic recovery, ever — an auto-retry after a signature failure is
    indistinguishable from an attacker retrying.
    """
    conn.execute("UPDATE brain_rules SET injectable = 0, "
                 "refused_reason = COALESCE(refused_reason, 'brain-untrusted')")
    set_state(conn, "trust", TRUST_UNTRUSTED)
    set_state(conn, "trust_reason", reason)
    conn.commit()


#: The highest manifest version this machine has EVER verified. It survives the
#: automatic full resync a 409 triggers, and only an explicit human
#: `nh brain trust --reset` clears it.
#:
#: Without it the 409 branch's own comment was false. That branch wiped the
#: watermark AND ``chain_head_sha256``, which is precisely the state in which a
#: control plane can answer the refetch with a genuinely-signed but OLDER chain
#: prefix: every signature verifies, the chain links, and a rule the team
#: withdrew comes back. The comment claimed the failure mode "degrades to FEWER
#: rules, not staler ones"; it could produce staler ones. This is the mark that
#: makes the sentence true.
_HIGH_WATER = "chain_high_water"

#: Keys ``reset`` never deletes. ``disabled`` is the developer's local off
#: switch and a trust reset is not them changing their mind.
_RESET_KEEPS = ("disabled",)


def chain_high_water(conn: sqlite3.Connection) -> int:
    try:
        return int(get_state(conn, _HIGH_WATER, 0) or 0)
    except (TypeError, ValueError):
        return 0


def note_verified_version(conn: sqlite3.Connection, version: int) -> None:
    """Raise the high-water mark. It never goes backwards."""
    if int(version) > chain_high_water(conn):
        set_state(conn, _HIGH_WATER, int(version))


def chain_top(conn: sqlite3.Connection) -> int:
    """How far the chain this machine currently HOLDS reaches, from signatures.

    This is the operand the replay guard compares against ``chain_high_water``,
    and the reason it exists is that the obvious operand — ``watermark`` — is
    written by the adversary. ``watermark`` is a plain JSON field of the sync
    response. It appears in no signed manifest, so a control plane serving a
    genuinely-signed OLDER chain prefix could report any number it liked and
    walk straight past a guard phrased as ``watermark < high_water``: report the
    real top, serve half of it, and the comparison the guard makes is one the
    server chose the answer to. Nothing has to be forged for that to work.

    Every number here is signed. ``chain_head_sha256`` is the digest of the last
    manifest that VERIFIED — signature, team, version, chain link and subject —
    and ``brain_manifests`` gains a row only on the same path. So this returns
    the version of a document this machine checked a signature over, and a
    server that serves less than it served before cannot claim otherwise.

    Zero when there is no head, and zero when the head names a manifest this
    store does not hold: both mean "this machine can prove nothing", which
    fails the comparison closed rather than open.
    """
    head = get_state(conn, "chain_head_sha256")
    if not head:
        return 0
    row = conn.execute(
        "SELECT version FROM brain_manifests WHERE sha256 = ?", (head,)
    ).fetchone()
    try:
        return int(row["version"]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def last_verified_sync(conn: sqlite3.Connection) -> float:
    try:
        return float(get_state(conn, "last_verified_sync", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


# --- rules ------------------------------------------------------------------


def upsert_rule(conn: sqlite3.Connection, rule: dict, *, injectable: bool,
                refused_reason: str | None, manifest_version: int | None,
                approvals: list | None) -> None:
    conn.execute(
        """
        INSERT INTO brain_rules (rule_id, version, scope, type, title, content,
                                 visibility, status, approvals, manifest_version,
                                 injectable, refused_reason, synced_at)
        VALUES (:rule_id, :version, :scope, :type, :title, :content, :visibility,
                :status, :approvals, :manifest_version, :injectable,
                :refused_reason, :synced_at)
        ON CONFLICT(rule_id) DO UPDATE SET
            version = excluded.version, scope = excluded.scope,
            type = excluded.type, title = excluded.title,
            content = excluded.content, visibility = excluded.visibility,
            status = excluded.status, approvals = excluded.approvals,
            manifest_version = excluded.manifest_version,
            injectable = excluded.injectable,
            refused_reason = excluded.refused_reason,
            synced_at = excluded.synced_at
        """,
        {
            "rule_id": str(rule.get("rule_id") or ""),
            "version": int(rule.get("version") or 0),
            "scope": str(rule.get("scope") or "GLOBAL"),
            "type": str(rule.get("type") or "rule"),
            "title": str(rule.get("title") or ""),
            "content": str(rule.get("content") or ""),
            # ALWAYS 'coder'. See screen.effective_visibility: the first
            # increment has no reviewer-visible path at all, so the stored value
            # is the one that will be honoured, not the one the server sent.
            "visibility": "coder",
            "status": str(rule.get("status") or "active"),
            "approvals": json.dumps(approvals or []),
            "manifest_version": manifest_version,
            "injectable": 1 if injectable else 0,
            "refused_reason": refused_reason,
            "synced_at": _now(),
        })
    conn.commit()


def _int_or_none(value) -> int | None:
    """An integer column, read as an integer or as nothing.

    ``manifest_version`` is interpolated into the coder prompt and this function
    used to pass it through raw while casting ``version`` beside it — so a row
    written by anything other than ``upsert_rule`` handed the renderer whatever
    the column held. SQLite's INTEGER affinity converts a NUMERIC string on the
    way in and leaves a non-numeric one as TEXT, which is exactly the value that
    would have been rendered. Dropping to None loses one line of provenance;
    carrying text would lose the property.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _row_to_rule(row: sqlite3.Row) -> BrainRule:
    try:
        approvals = json.loads(row["approvals"] or "[]")
    except ValueError:
        approvals = []
    return BrainRule(
        rule_id=row["rule_id"], version=_int_or_none(row["version"]) or 0,
        title=row["title"] or "", content=row["content"] or "",
        visibility=row["visibility"] or "coder", status=row["status"] or "",
        approvals=approvals if isinstance(approvals, list) else [],
        manifest_version=_int_or_none(row["manifest_version"]),
        injectable=bool(row["injectable"]),
        refused_reason=row["refused_reason"],
    )


def all_rules(conn: sqlite3.Connection) -> list[BrainRule]:
    rows = conn.execute(
        "SELECT * FROM brain_rules ORDER BY version ASC").fetchall()
    return [_row_to_rule(r) for r in rows]


def injectable_rules(conn: sqlite3.Connection, *, up_to_version: int) -> list[BrainRule]:
    """Rules that may reach a coder prompt, AS OF a pinned watermark.

    ``up_to_version`` is T5: the watermark is pinned at task start and every
    prompt in that attempt reads rules as of that version. The cloud translation
    of the base-branch rule — an admission that lands mid-task cannot change the
    judgement of a task already under way.

    Blocked rules are excluded by a LEFT JOIN rather than by a flag on the row,
    so a sync (which rewrites rows) can never clear a local block.
    """
    rows = conn.execute(
        """
        SELECT r.* FROM brain_rules r
        LEFT JOIN brain_blocks b ON b.rule_id = r.rule_id
        WHERE r.injectable = 1 AND r.version <= ? AND b.rule_id IS NULL
        ORDER BY r.version ASC
        """, (int(up_to_version),)).fetchall()
    return [_row_to_rule(r) for r in rows]


def block(conn: sqlite3.Connection, rule_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO brain_blocks (rule_id, blocked_at) VALUES (?, ?)",
        (rule_id, _now()))
    conn.commit()


def blocked_ids(conn: sqlite3.Connection) -> list[str]:
    return [r["rule_id"] for r in
            conn.execute("SELECT rule_id FROM brain_blocks ORDER BY rule_id")]


# --- manifests --------------------------------------------------------------


def record_manifest(conn: sqlite3.Connection, version: int, sha256: str,
                    prev_sha256: str | None, envelope: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO brain_manifests "
        "(version, sha256, prev_sha256, envelope, verified_at) VALUES (?,?,?,?,?)",
        (int(version), sha256, prev_sha256, json.dumps(envelope), _now()))
    conn.commit()


def manifest_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM brain_manifests").fetchone()
    return int(row["n"]) if row else 0


# --- reset ------------------------------------------------------------------


def reset(conn: sqlite3.Connection, *, keep_high_water: bool = False) -> None:
    """Discard all synced state and clear the untrusted mark.

    Local BLOCKS survive: they are the developer's veto over the vendor, and a
    trust reset is not the developer changing their mind.

    ``keep_high_water`` is the difference between the automatic callers and the
    human one. It is half of the replay defence; the other half is
    ``chain_top``, which is what the mark is COMPARED against, and which had to
    stop being the sync response's ``watermark`` because the control plane
    writes that field itself. The AUTOMATIC resync a 409 triggers passes True:
    it is the server saying "refetch", and a server that then serves a shorter
    chain than this machine already verified is replaying, not recovering. The
    HUMAN's explicit ``nh brain trust --reset`` passes False — a person deciding
    to start over is allowed to start over, and without that this guard would be
    a trap with no exit.
    """
    keeps = _RESET_KEEPS + ((_HIGH_WATER,) if keep_high_water else ())
    placeholders = ", ".join("?" for _ in keeps)
    conn.execute("DELETE FROM brain_rules")
    conn.execute("DELETE FROM brain_manifests")
    conn.execute(f"DELETE FROM brain_state WHERE key NOT IN ({placeholders})",
                 keeps)
    conn.commit()
