"""The team-brain client. OFF BY DEFAULT, inert when off, deletable always.

READ THIS BEFORE ADDING ANYTHING HERE.

The hosted team brain lives in a separate repository, on purpose, so the two
stacks cannot bleed into each other. The project's lean-stack rule permits that
stack to use whatever it needs and then binds the half that matters here:
*nothing the hosted tier adds may become a dependency of the local product*. This package is
allowed to exist only because it drags none of it across, and each of the five
invariants below has a test that FAILS if it is violated
(``tests/test_brain_invariants.py``):

  L1  zero new packages — ``httpx`` was already declared; nothing else.
  L2  zero new stores or processes — the product's own SQLite database, no
      daemon, no thread, no scheduler, no second file but the credential.
  L3  no AWS surface at all — no SDK, no ARN, no region, no table or bucket
      name anywhere under ``src/no_human/``. The client knows ONE base URL and
      the service's shape is deliberately unlearnable from it.
  L4  inert and deletable when off — with ``team_brain.enabled`` false, prompts
      are BYTE-IDENTICAL to a build with this directory deleted.
  L5  never on a critical path — no task may block, slow, or fail because of
      the brain. There is no network call anywhere near a running task: sync is
      an explicit command a human typed.

WHAT THIS FIRST INCREMENT DOES: read-only, manually-triggered, coder-only,
signature-verified sync of confirmed GLOBAL rules. No write path — nothing but a
sign-in token ever leaves the machine, which is the asymmetry that makes this
the right half to ship first: a bug here cannot leak the customer's IP.

Nothing is imported at module import time beyond the standard library. The two
entry points below import what they need when they are called, and both return
harmlessly when the feature is off — which is what keeps L4 true at the source
level rather than by promise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["BrainContext", "coder_context", "pin_watermark"]


@dataclass(frozen=True)
class BrainContext:
    block: str = ""
    rule_ids: list = field(default_factory=list)


def _db_path(config: Any):
    from pathlib import Path
    path = getattr(config, "db_path", None)
    if path is not None:
        return path
    return Path(((config.get("database") or {}).get("path")
                 or "~/.no_human/no_human.db")).expanduser()


def _readable(path) -> bool:
    """Does the database file already exist?

    Asked before opening, because ``sqlite3.connect`` CREATES a missing file and
    a task's path may not create anything at all (L4: "no file is created").
    A machine that has never synced has nothing to read, and that is not an
    error — it is the closed state, which is the current product.
    """
    try:
        from pathlib import Path
        return Path(path).is_file()
    except Exception:  # noqa: BLE001
        return False


def pin_watermark(config: Any) -> int | None:
    """The brain version this attempt will read rules as of, or None.

    Pinned ONCE at attempt start and honoured by every prompt in the attempt.
    This is the cloud translation of the base-branch rule the review design
    demands: an admission that lands mid-task cannot change the judgement of a
    task already under way.

    Returns None — and touches nothing — when the feature is off.

    ``connect_readonly``, never ``connect``: this runs once per ATTEMPT, on the
    product's live database, and ``connect`` issues DDL. See
    ``store.READ_TIMEOUT_SECONDS``.
    """
    from .settings import read
    cfg = read(config)
    if not cfg.enabled:
        return None
    try:
        from . import store
        path = _db_path(config)
        if not _readable(path):
            return None
        conn = store.connect_readonly(path)
        try:
            return store.watermark(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — L5: the brain never fails a task.
        return None


def coder_context(config: Any, watermark: int | None) -> BrainContext:
    """The remote-rules block for the CODER prompt, and the ids that fed it.

    Coder only. There is no reviewer, supervisor or planner path in this
    increment and adding one is a design decision, not a refactor — see
    ``screen.effective_visibility``.

    Every failure mode returns an empty context. That is L5 in code: a missing
    database, a corrupt row, a deleted package, an untrusted brain and a stale
    cache all degrade to today's behaviour, which is zero remote rules.
    """
    empty = BrainContext()
    try:
        from .settings import read
        cfg = read(config)
        if not cfg.enabled or watermark is None:
            return empty

        import time

        from . import render, store
        path = _db_path(config)
        if not _readable(path):
            return empty
        # Read-only, short-timeout, no DDL. This is called on every
        # implement-prompt build; see store.READ_TIMEOUT_SECONDS.
        conn = store.connect_readonly(path)
        try:
            if store.get_state(conn, "disabled"):
                return empty
            if store.trust(conn) != store.TRUST_OK:
                return empty
            # A withdrawn rule must not live forever on a laptop that stopped
            # syncing. Past the ceiling, remote rules stop being injected until
            # a sync succeeds — fewer rules, never staler ones.
            last = store.last_verified_sync(conn)
            if not last or (time.time() - last) > cfg.max_stale_days * 86400:
                return empty
            # The replay guard, on the READ side, where it holds however sync
            # exited. `sync.run` applies a whole page and only THEN decides the
            # chain was a replayed prefix; a crash, a kill or an unexpected
            # exception between those two points leaves the replayed rows in the
            # database marked injectable. The same statement is true here and
            # needs no cooperation from the writer: the chain this machine holds
            # must reach at least as far as one it has already verified, or it
            # is staler than what it replaced and nothing may be injected.
            #
            # `chain_top`, not `watermark`, for the reason store.chain_top's
            # docstring gives: `watermark` is a field of the sync response and
            # the control plane writes it, so a guard phrased over it lets the
            # adversary pick its own answer. Both operands here are versions
            # this machine verified a signature over.
            if store.chain_top(conn) < store.chain_high_water(conn):
                return empty
            rules = render.select(
                store.injectable_rules(conn, up_to_version=int(watermark)))
            block = render.coder_block(rules)
            if not block:
                return empty
            return BrainContext(block=block,
                                rule_ids=[r.rule_id for r in rules])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — L5, again, and deliberately total.
        return empty
