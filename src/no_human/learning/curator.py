"""Learning-store curator (D2 #3, broker `agent/curator.py` pattern).

The pending queue floods (211 unconfirmed at curation time) and rots — a
store nobody can read is a store nobody confirms, so learnings stop
compounding. The curator maintains it under broker' invariants:

- **never deletes** — archive only (recoverable, audit-trailed);
- **pinned exempt** — confirmed memories are the operator's; never touched;
- **the human gate stands** — confirming (applying) a learning remains the
  ONLY way it takes effect; the curator only tidies the unconfirmed queue.

Two passes:
1. deterministic dedupe (free): identical normalized title+content prefix →
   keep the oldest, archive the rest;
2. utility-model batch (advisory tier, injected for tests): propose ARCHIVE
   (noise/stale/transient) and CONSOLIDATE groups. Fail-closed JSON: an
   unparseable reply curates nothing. LLM-proposed actions apply only with
   ``apply=True`` (the CLI's --apply); the dedupe pass is safe to auto-apply.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("no_human.learning")

_JSON_BLOCK = re.compile(r"CURATE_JSON_START\s*(.*?)\s*CURATE_JSON_END", re.DOTALL)


@dataclass
class CurationReport:
    duplicates_archived: int = 0
    llm_archive_proposed: list[dict[str, Any]] = field(default_factory=list)
    llm_consolidate_proposed: list[dict[str, Any]] = field(default_factory=list)
    llm_applied: bool = False
    notes: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def dedupe_key(mem: dict[str, Any]) -> str:
    return f"{_norm(mem.get('title', ''))}|{_norm(mem.get('content', ''))[:160]}"


def build_curate_prompt(memories: list[dict[str, Any]]) -> str:
    lines = [
        f"- id={m['id'][:8]} [{m.get('type', '?')}] {str(m.get('title', ''))[:110]}"
        for m in memories
    ]
    return (
        "You are curating an autonomous coding agent's PENDING learning queue "
        "(unconfirmed proposals a human will review). Propose hygiene actions "
        "ONLY:\n"
        "- archive: noise, transient one-offs (a specific failed run), or "
        "entries too vague to ever act on;\n"
        "- consolidate: near-duplicate groups that should become ONE proposal "
        "(members get archived; the merged text is re-proposed for the human).\n"
        "NEVER propose archiving something that looks like a durable "
        "preference or rule — when unsure, leave it alone.\n\n"
        "Queue:\n" + "\n".join(lines) + "\n\n"
        "Reply exactly:\nCURATE_JSON_START\n"
        '{"archive": [{"id": "<8-char id>", "reason": "..."}], '
        '"consolidate": [{"ids": ["..",".."], "title": "...", "content": "..."}]}\n'
        "CURATE_JSON_END"
    )


def parse_curation(text: str) -> dict[str, Any] | None:
    """Fail closed: no block / bad JSON → None (curate nothing)."""
    m = _JSON_BLOCK.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {"archive": list(data.get("archive") or []),
            "consolidate": list(data.get("consolidate") or [])}


async def curate(
    store: Any, *,
    llm_call: Callable[[str], Awaitable[str]] | None = None,
    apply: bool = False,
    batch: int = 60,
) -> CurationReport:
    report = CurationReport()
    pending = await store.list_memories(confirmed=False)
    if not pending:
        report.notes = "queue empty"
        return report

    # Pass 1 — deterministic duplicate collapse (keep the OLDEST; created_at
    # ascending means the first seen wins). Free, always applied.
    seen: dict[str, str] = {}
    ordered = sorted(pending, key=lambda m: str(m.get("created_at") or ""))
    for m in ordered:
        key = dedupe_key(m)
        if key in seen:
            if await store.archive_memory(
                    m["id"], f"duplicate of {seen[key][:8]}"):
                report.duplicates_archived += 1
        else:
            seen[key] = m["id"]

    if llm_call is None:
        report.notes = "dedupe only (no llm_call)"
        return report

    # Pass 2 — utility-tier proposals over the survivors, batched.
    survivors = await store.list_memories(confirmed=False)
    by_short = {m["id"][:8]: m["id"] for m in survivors}
    for i in range(0, len(survivors), batch):
        chunk = survivors[i:i + batch]
        try:
            reply = await llm_call(build_curate_prompt(chunk))
        except Exception as exc:  # noqa: BLE001 — advisory tier, best effort
            log.warning("curator llm batch failed: %s", exc)
            continue
        actions = parse_curation(reply)
        if actions is None:
            continue
        report.llm_archive_proposed += actions["archive"]
        report.llm_consolidate_proposed += actions["consolidate"]

    if apply:
        for a in report.llm_archive_proposed:
            full = by_short.get(str(a.get("id", ""))[:8])
            if full:
                await store.archive_memory(
                    full, f"curator: {str(a.get('reason', ''))[:120]}")
        for c in report.llm_consolidate_proposed:
            members = [by_short.get(str(x)[:8]) for x in (c.get("ids") or [])]
            members = [x for x in members if x]
            if len(members) < 2 or not c.get("title"):
                continue
            # source="proposed" — the queue-visibility contract. This wrote
            # source="curator" and then ARCHIVED the members it consolidated,
            # so a consolidation removed N rows from the human's confirm queue
            # and replaced them with one row `pending()` could not see. That is
            # the module docstring's "the human gate stands" failing on its own
            # terms: a consolidated learning could never reach the gate. The
            # curator names itself in `origin`, like every other producer.
            from .queue import ORIGIN_CURATOR
            new_id = await store.add_memory(
                mem_type="rule", title=str(c["title"])[:120],
                content=str(c.get("content", ""))[:2000]
                + f"\n\n[consolidated from {len(members)} proposals]",
                source="proposed", origin=ORIGIN_CURATOR, confirmed=False,
            )
            if new_id:
                for mid in members:
                    await store.archive_memory(mid, f"consolidated into {new_id[:8]}")
        report.llm_applied = True
    return report
