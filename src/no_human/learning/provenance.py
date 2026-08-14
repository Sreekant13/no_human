"""P1 brain hygiene: the write-time provenance + quarantine gate for the
``memories`` table (``core/db.py``), and the read-side inventory scan that
reports how many existing rows would be caught by it.

WHY THIS EXISTS, and why it is a SEPARATE control from
``Orchestrator._screen_memories_for_terms`` / ``_active_memories``
(``core/orchestrator.py:11215-11317``). That screen is READ-side and fails
OPEN on a matcher error, by design: it sits between an already-confirmed rule
and the prompt it would join, and a false hold there silently withholds a
good rule forever with nobody watching. This gate is WRITE-side: it decides
whether a freshly-ingested row is flagged ``quarantined`` before anyone has
acted on it, and the flag is one ``UPDATE`` away from being lifted. Losing a
row to a matcher bug here costs nothing a human cannot recover; admitting one
that should have been caught is the defect this whole task exists to close.
So this gate FAILS CLOSED — see ``quarantine_reason`` below.

THREE NEEDLE CLASSES, deliberately not one flat list:

  * ``NEEDLE_CLASS_VENDOR`` — delegates to ``eval.vendor_terms.find_banned_terms``,
    which already gives case-insensitive letter-boundary matching plus
    camelCase/glue-variant detection, and already carries the employer names
    through the private supplement (``eval/_vendor_terms_private.py``). This
    class is not re-implemented here; it is reused.
  * ``NEEDLE_CLASS_PHRASES`` — ``MEMORY_NEEDLE_HEX`` in the private supplement:
    regex FRAGMENTS (not literal terms) for the separator-variant compound
    forms ``find_banned_terms`` cannot see. Its rule (1) needs a literal space
    in a multi-word term and its camelCase rule needs an actual case change,
    so a two-word vendor phrase glued with an underscore or a hyphen instead
    of a space slips past it — this class closes exactly that gap, for the
    same reason the compound repo-name fragment already proven in
    ``tests/test_deidentify_p1_repo_names.py`` needed one. No example is
    spelled here; see that test file's hex-encoded inventory for the shape.
  * ``NEEDLE_CLASS_PROJECT`` — a memory whose ``project`` is not a prefix
    match of any allowlisted path, when an allowlist is configured. UNSET is
    INERT (the needle classes above still apply): a default that only
    trusted "this repo" would quarantine every legitimate memory on every
    fresh install, and nothing in this task establishes that default.

Spells no employer term anywhere in this file. ``MEMORY_NEEDLE_HEX`` is
loaded from the private supplement optionally-but-fail-closed, exactly like
``eval/vendor_terms.py`` loads ``EXTRA_HEX`` — see that module's docstring
for the split-in-two rationale (this file ships; the supplement does not).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..eval.vendor_terms import find_banned_terms

NEEDLE_CLASS_VENDOR = "employer-vendor-terms"
NEEDLE_CLASS_PHRASES = "memory-needle-phrases"
NEEDLE_CLASS_PROJECT = "non-allowlisted-project"

#: CLASS INDEX is 1-based position in this tuple. The PR body and every
#: forge-visible surface report counts BY INDEX ONLY (operator directive):
#: the flagged term names live in the private supplement and nowhere else.
NEEDLE_CLASSES: tuple[str, ...] = (
    NEEDLE_CLASS_VENDOR, NEEDLE_CLASS_PHRASES, NEEDLE_CLASS_PROJECT,
)

MATCHER_ERROR_SUFFIX = ":matcher-error"

#: The env var that configures the project allowlist for the write-time gate.
#: NOT ``config.yaml``, on purpose: ``add_memory`` (``core/db.py``) is the
#: single write chokepoint every learning writer goes through, and
#: ``core/db.py`` otherwise never loads ``config.yaml`` — doing so here would
#: make every test in the suite that calls ``add_memory`` read (and, on a
#: fresh install, CREATE) ``~/.no_human/config.yaml`` on every write, which
#: ``tests/conftest.py`` already documents as an unclosed hole for exactly
#: this reason ("the suite still writes ... into whatever HOME it is given").
#: An env var is one of the three configuration surfaces the task's own
#: intake resolution names (env var / config file / DB table) and it is the
#: only one of the three that costs no filesystem access on the hot path.
#: A caller that already has a loaded ``Config`` (the CLI's ``nh memories
#: scan``) may still pass ``allowlist=`` explicitly to ``project_allowlist``.
ALLOWLIST_ENV_VAR = "NO_HUMAN_LEARNING_PROJECT_ALLOWLIST"

_t = lambda h: bytes.fromhex(h).decode()  # noqa: E731

try:
    from ..eval._vendor_terms_private import (
        MEMORY_NEEDLE_HEX as _MEMORY_NEEDLE_HEX,
    )
except ImportError as e:
    # `no_human.eval._vendor_terms_private` is the absolute module name a
    # cross-package relative import fails under when the supplement is
    # absent (public export). `_vendor_terms_private` alone covers the same
    # module resolved a second way in some import contexts. Anything else is
    # a real bug in the supplement and must be loud — see `vendor_terms.py`
    # for why a bare `except ImportError` is the wrong shape here too.
    if e.name not in ("no_human.eval._vendor_terms_private",
                       "_vendor_terms_private"):
        raise
    _MEMORY_NEEDLE_HEX: list[str] = []  # public export: supplement not distributed

#: One compiled pattern per fragment. Letter-boundary lookarounds, NOT
#: `re.escape` — the fragments are regex source (character classes like
#: `[ _-]`), the same shape as `store[ _-]app` in
#: `tests/test_deidentify_p1_repo_names.py`. The boundary is what keeps a
#: fragment from matching inside a longer ordinary word.
_PHRASE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"(?<![A-Za-z])(?i:{frag})(?![A-Za-z])")
    for frag in (_t(h) for h in _MEMORY_NEEDLE_HEX)
]


def _vendor_hit(text: str) -> bool:
    return bool(find_banned_terms(text))


def _phrase_indices(text: str) -> set[int]:
    """1-based positions in `_PHRASE_PATTERNS` (== positions in
    `MEMORY_NEEDLE_HEX`) that *text* trips — every one, not just the first."""
    return {i for i, p in enumerate(_PHRASE_PATTERNS, start=1) if p.search(text)}


def _phrase_hit(text: str) -> bool:
    return bool(_phrase_indices(text))


def project_allowlist(config: Any = None) -> list[str] | None:
    """The configured project allowlist, or None when unset (inert).

    ``config`` is optional and, when given, is read as ``learning.
    project_allowlist`` off any object exposing a dict-like ``.get`` (a
    loaded ``Config`` or a plain dict) — for callers that already hold one,
    such as the CLI's ``nh memories scan``. Falls back to the
    ``NO_HUMAN_LEARNING_PROJECT_ALLOWLIST`` env var (comma-separated paths)
    otherwise, which is what the write-time gate in ``add_memory`` uses (see
    ``ALLOWLIST_ENV_VAR`` for why it does not load ``config.yaml``).
    """
    if config is not None:
        try:
            getter = getattr(config, "get", None)
            learning = getter("learning", {}) if callable(getter) else None
            if isinstance(learning, dict):
                allow = learning.get("project_allowlist")
                if allow:
                    return [str(p) for p in allow]
        except Exception:  # noqa: BLE001 — a broken config object is not fatal
            pass
    raw = os.environ.get(ALLOWLIST_ENV_VAR)
    if not raw:
        return None
    paths = [p.strip() for p in raw.split(",") if p.strip()]
    return paths or None


def _project_class_hit(project: str | None, allowlist: list[str] | None) -> bool:
    """True when *project* is set and is NOT a prefix match of any allowlisted
    path. A GLOBAL memory (``project`` is None — no repo scope at all, the
    same convention ``list_memories`` uses for "applies everywhere") is
    exempt: it names no repository to be off the allowlist FROM, and treating
    "no project" as "wrong project" would quarantine most cross-project rules
    the instant any allowlist is configured, which defeats the feature."""
    if not allowlist or not project:
        return False

    def _admits(prefix: str) -> bool:
        # Exact match or a SEPARATOR-BOUNDED prefix — `project.startswith`
        # alone would let `/git/allowed/repo-evil` ride in on an allowlisted
        # `/git/allowed/repo` (round-3 review advisory 1). A bare `.rstrip`
        # keeps `/` an exempt trailing separator on the allowlisted entry
        # itself without treating it as part of the boundary check twice.
        p = prefix.rstrip("/")
        return project == p or project.startswith(p + "/")

    return not any(_admits(prefix) for prefix in allowlist)


def _classes_hit(
    *, title: str | None, content: str | None, project: str | None,
    tags: list[str] | None = None, allowlist: list[str] | None = None,
) -> tuple[set[str], set[int]]:
    """Every needle class *text*/*project* trips — not just the first — plus
    the 1-based `MEMORY_NEEDLE_HEX` positions the phrase class tripped on.
    Used by `scan_memories` so a row matching two classes counts in both
    `per_class`/`per_class_index` buckets while still contributing once to
    `union_total`."""
    text = f"{title or ''}\n{content or ''}\n{' '.join(tags or [])}"
    hits: set[str] = set()
    needle_idx: set[int] = _phrase_indices(text)
    if _vendor_hit(text):
        hits.add(NEEDLE_CLASS_VENDOR)
    if needle_idx:
        hits.add(NEEDLE_CLASS_PHRASES)
    if _project_class_hit(project, allowlist):
        hits.add(NEEDLE_CLASS_PROJECT)
    return hits, needle_idx


def quarantine_reason(
    *, title: str | None, content: str | None, project: str | None = None,
    tags: list[str] | None = None, allowlist: list[str] | None = None,
) -> str | None:
    """A needle-class label, or None when nothing matches.

    FAILS CLOSED: any exception raised while matching returns
    ``NEEDLE_CLASS_VENDOR + ":matcher-error"`` — i.e. quarantine — rather
    than propagating or admitting the row. See the module docstring for why
    this is the deliberate opposite of `_screen_memories_for_terms`'s
    documented fail-OPEN.
    """
    try:
        hits, _needle_idx = _classes_hit(
            title=title, content=content, project=project, tags=tags,
            allowlist=allowlist,
        )
    except Exception:  # noqa: BLE001 — write-side gate: fail closed, not loud
        return NEEDLE_CLASS_VENDOR + MATCHER_ERROR_SUFFIX
    if not hits:
        return None
    for cls in (NEEDLE_CLASS_VENDOR, NEEDLE_CLASS_PHRASES, NEEDLE_CLASS_PROJECT):
        if cls in hits:
            return cls
    return next(iter(hits))  # unreachable in practice; keeps the function total


class InventoryError(Exception):
    """Raised by `scan_memories` when its row source looks like the wrong
    table — the empty-table known-positive control. See `scan_memories`."""


@dataclass
class Inventory:
    total_rows: int
    per_class: dict[str, int] = field(default_factory=dict)
    per_needle_index: dict[int, int] = field(default_factory=dict)
    union_ids: list[str] = field(default_factory=list)

    @property
    def union_total(self) -> int:
        return len(self.union_ids)

    @property
    def per_class_index(self) -> dict[int, int]:
        """`per_class`, keyed by 1-based `NEEDLE_CLASSES` position instead of
        the class label — the only form of this inventory ever reported on a
        forge-visible surface (PR body, CLI console, JSON)."""
        return {
            i: self.per_class.get(cls, 0)
            for i, cls in enumerate(NEEDLE_CLASSES, start=1)
        }


RowSource = Callable[[], Awaitable[list[dict[str, Any]]]]


async def _default_ui_probe(store: Any) -> list[dict[str, Any]]:
    """Confirmed rows of the four types the Rules/Skills UI panels list —
    the "does the UI show anything" side of the empty-table control."""
    from . import TYPE_ANTI_PATTERN, TYPE_FACT, TYPE_RULE, TYPE_SKILL

    rows: list[dict[str, Any]] = []
    for mem_type in (TYPE_RULE, TYPE_ANTI_PATTERN, TYPE_SKILL, TYPE_FACT):
        rows += await store.list_memories(confirmed=True, mem_type=mem_type)
    return rows


async def scan_memories(
    store: Any, *, row_source: RowSource | None = None,
    ui_probe: RowSource | None = None, allowlist: list[str] | None = None,
) -> Inventory:
    """The P1 inventory: per-class + union counts over every memory row.

    ``row_source`` defaults to every row in the live store (quarantined and
    archived included, so nothing already flagged is invisible to a re-scan).
    ``ui_probe`` defaults to `_default_ui_probe`.

    THE KNOWN-POSITIVE CONTROL — the reason this task was refiled: a scan
    that reads 0 rows while the UI-facing probe reports rows is pointed at
    the wrong table (the cancelled spec scanned `brain_rules` /
    `brain_manifests` / `brain_blocks`, which are empty and pass vacuously).
    That combination raises `InventoryError` rather than reporting a clean
    scan.
    """
    rows = await (
        row_source() if row_source is not None
        else store.list_memories(include_quarantined=True, include_archived=True)
    )
    ui_rows = await (ui_probe() if ui_probe is not None else _default_ui_probe(store))
    if len(rows) == 0 and len(ui_rows) > 0:
        raise InventoryError(
            f"scan_memories read 0 rows while the UI lists {len(ui_rows)} — "
            "the scan is pointed at the wrong table"
        )
    per_class = {
        NEEDLE_CLASS_VENDOR: 0, NEEDLE_CLASS_PHRASES: 0, NEEDLE_CLASS_PROJECT: 0,
    }
    per_needle_index: dict[int, int] = {}
    union_ids: list[str] = []
    for row in rows:
        hits, needle_idx = _classes_hit(
            title=row.get("title"), content=row.get("content"),
            project=row.get("project"), allowlist=allowlist,
        )
        if not hits:
            continue
        for cls in hits:
            per_class[cls] = per_class.get(cls, 0) + 1
        for idx in needle_idx:
            per_needle_index[idx] = per_needle_index.get(idx, 0) + 1
        if row.get("id"):
            union_ids.append(row["id"])
    return Inventory(
        total_rows=len(rows), per_class=per_class,
        per_needle_index=per_needle_index, union_ids=union_ids,
    )
