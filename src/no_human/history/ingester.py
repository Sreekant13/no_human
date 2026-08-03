"""Ingest analyzer findings into the human-confirmed learning queue
(EVOLUTION_PLAN §1.1, Sprint 1).

The pipeline is: ``extractor`` (transcripts) → ``analyzer`` (Findings, heuristic
+ optional LLM pass) → **this module** → ``learning/queue.py`` Proposals.

Every finding is enqueued as a memory with ``source="proposed"`` and
``confirmed=0``. That is the learning queue's "awaiting confirmation" marker
(``LearningQueue.pending`` filters on it). Nothing here ever becomes an active
rule — a human must confirm it in ``nh learnings`` / the onboarding rules-review
step. Auto-applying transcript-mined rules would let one-off context calcify into
permanent rules, the exact failure constraint #3 / the queue invariant forbid.

A stable ``dedupe_key`` per finding makes re-ingestion idempotent: running the
ingester twice (e.g. a periodic re-analysis, §9) never duplicates a proposal.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field

from ..core.db import Store
from ..learning.pii import contains_pii
from .analyzer import (
    Finding,
    analyze_transcript,
    analyze_transcript_llm,
)
from .extractor import IDENotRunningError, Transcript, extract_transcripts

log = logging.getLogger("no_human.history")


def _dedupe_key(f: Finding) -> str:
    """Stable signature so the same lesson is not proposed twice across runs.

    Keyed on (category, normalized content, source transcript) — matches the
    learning-queue's ``learn:`` namespace so provenance is consistent."""
    raw = "\x1f".join(
        p.strip().lower()
        for p in (f.category, f.content, f.source_transcript)
        if p
    )
    return "learn:" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def _transcript_sig(t: Transcript) -> str:
    """Content-signature for a transcript (Phase 7e history cache).

    Based on the messages' content — if the conversation hasn't changed,
    the signature stays stable and re-analysis is skipped.
    """
    raw = "\x1f".join(
        f"{m.role}:{(m.content or '')[:500]}" for m in t.messages[:50]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass
class IngestResult:
    proposed: int = 0          # newly-enqueued proposals
    duplicates: int = 0        # deduped (already proposed)
    transcripts: int = 0
    findings: int = 0
    dropped_pii: int = 0       # findings refused at the personal-data gate
    proposals: list[dict] = field(default_factory=list)


class TranscriptIngester:
    """Routes analyzer findings into the learning queue as proposals."""

    def __init__(self, store: Store, *, llm_call=None):
        """``llm_call`` (optional async ``(prompt)->str``) enables the LLM
        analyzer pass when ``ingest(use_llm=True)`` is called. Off by default —
        it costs tokens."""
        self.store = store
        self._llm_call = llm_call

    async def ingest_findings(self, findings: list[Finding]) -> IngestResult:
        """Enqueue each finding as a ``source="proposed"`` memory (confirmed=0).

        Idempotent via per-finding ``dedupe_key``: a finding whose key already
        exists is counted as a duplicate, not re-proposed.

        This method is the last thing between a mined finding and the database,
        so the personal-data gate is applied HERE as well as upstream in the
        analyzer: every caller — the CLI, the onboarding wizard, periodic
        re-analysis, and any future one — funnels through it, and a gate placed
        only in the analyzer would be bypassed by anything that builds Findings
        another way. Refused findings are DROPPED, never redacted, and counted
        in ``dropped_pii``."""
        result = IngestResult(findings=len(findings))
        for f in findings:
            pii = contains_pii(f.title, f.content)
            if pii is not None:
                result.dropped_pii += 1
                log.info("refused a proposed learning carrying personal data "
                         "(%s) from transcript %s", pii.kind, f.source_transcript)
                continue
            tags = list(f.tags)
            # Carry provenance in tags (no new table — EVOLUTION_PLAN DB section):
            # source transcript + message index, so the rules-review UI can show
            # where a proposed rule came from.
            if f.source_transcript:
                tags.append(f"src:{f.source_transcript}")
            if f.source_message >= 0:
                tags.append(f"msg:{f.source_message}")
            mem_id = await self.store.add_memory(
                mem_type=f.category,
                title=f.title,
                content=f.content,
                tags=tags,
                # Scope the mined rule to the repo the conversation happened in
                # (empty → unscoped, as before). A no_human task shouldn't see
                # rules mined from an unrelated repo's chats.
                project=f.project or None,
                source="proposed",
                confirmed=False,
                dedupe_key=_dedupe_key(f),
            )
            if mem_id:
                result.proposed += 1
                result.proposals.append({
                    # The FULL content, not a slice of it. This used to be
                    # `f.content[:400]`, which is what the rules-review step in the
                    # wizard renders — so a heuristic finding (whose content is the
                    # user's own message, verbatim) arrived cut off mid-sentence,
                    # with no marker and no way to reach the rest. The row written
                    # by add_memory above already holds the whole thing, and
                    # /api/rules already serves whole memory rows to Settings, so
                    # nothing here is a new size class; the slice only ever hid
                    # text the caller is being asked to APPROVE.
                    "id": mem_id, "category": f.category, "title": f.title,
                    "content": f.content, "importance": f.importance,
                    "source_transcript": f.source_transcript,
                    "source_message": f.source_message,
                })
            else:
                result.duplicates += 1
        log.info(
            "ingested %d findings: %d proposed, %d duplicate, %d refused "
            "(personal data)",
            result.findings, result.proposed, result.duplicates,
            result.dropped_pii,
        )
        return result

    async def ingest_transcripts(
        self, transcripts: list[Transcript], *, use_llm: bool = False,
        llm_concurrency: int = 5,
    ) -> IngestResult:
        """Analyze transcripts (heuristic + optional LLM pass) and ingest.

        Phase 7e: transcripts whose content-signature is already cached are
        skipped — makes re-scans fast without losing accuracy. The cache can
        be cleared with ``store.history_cache_clear()`` for a full re-scan.

        The LLM pass runs CONCURRENTLY across transcripts (bounded by
        ``llm_concurrency``) so onboarding doesn't serialize N model calls. A
        transcript whose LLM call fails is skipped (its heuristic findings still
        land) — the pass is additive, never a hard dependency."""
        # Phase 7e: filter out cached transcripts
        new_transcripts: list[Transcript] = []
        cached_count = 0
        for t in transcripts:
            sig = _transcript_sig(t)
            try:
                cached = await self.store.history_cache_get(sig)
            except Exception:  # noqa: BLE001 — cache miss is fine
                cached = None
            if cached:
                cached_count += 1
            else:
                new_transcripts.append(t)
        if cached_count:
            log.info("history cache: skipped %d unchanged transcripts", cached_count)

        findings: list[Finding] = []
        for t in new_transcripts:
            findings.extend(analyze_transcript(t))

        if use_llm and self._llm_call is not None and new_transcripts:
            sem = asyncio.Semaphore(max(1, llm_concurrency))

            async def _one(t: Transcript) -> list[Finding]:
                async with sem:
                    return await analyze_transcript_llm(t, self._llm_call)

            for res in await asyncio.gather(
                *[_one(t) for t in new_transcripts], return_exceptions=True
            ):
                if isinstance(res, list):
                    findings.extend(res)
                else:
                    log.warning("LLM analysis of a transcript failed: %s", res)

        result = await self.ingest_findings(findings)
        result.transcripts = len(transcripts)

        # Phase 7e: cache all processed transcripts so they're skipped next time
        for t in new_transcripts:
            sig = _transcript_sig(t)
            try:
                await self.store.history_cache_put(
                    sig, t.cascade_id, t.title, "",
                )
            except Exception:  # noqa: BLE001 — cache write failure is non-fatal
                pass

        return result

    async def ingest(self, *, days: int = 30, use_llm: bool = False) -> IngestResult:
        """End-to-end: extract recent transcripts, analyze, ingest. The single
        entry point the CLI / onboarding / periodic re-analysis call.

        No IDE running is the ORDINARY case, not a failure — every other
        caller of ``extract_transcripts`` (``nh history``, ``nh bench build``,
        the onboarding history scan) already catches ``IDENotRunningError``
        and degrades to "no Windsurf transcripts" instead of raising. This was
        the one caller that did not, so the periodic re-analysis job (due
        immediately on every fresh boot — see ``ReanalysisJob.due``) let it
        propagate up to ``Scheduler.tick``'s generic handler, which logged it
        as a "re-analysis failed" WARNING on every single startup.
        """
        try:
            transcripts = extract_transcripts(days=days)
        except (IDENotRunningError, ImportError) as exc:
            log.debug("windsurf transcripts skipped: %s", exc)
            transcripts = []
        return await self.ingest_transcripts(transcripts, use_llm=use_llm)
