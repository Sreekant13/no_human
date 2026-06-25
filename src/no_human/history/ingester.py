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
from .analyzer import (
    Finding,
    analyze_transcript,
    analyze_transcript_llm,
)
from .extractor import Transcript, extract_transcripts

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


@dataclass
class IngestResult:
    proposed: int = 0          # newly-enqueued proposals
    duplicates: int = 0        # deduped (already proposed)
    transcripts: int = 0
    findings: int = 0
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
        exists is counted as a duplicate, not re-proposed."""
        result = IngestResult(findings=len(findings))
        for f in findings:
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
                project=None,
                source="proposed",
                confirmed=False,
                dedupe_key=_dedupe_key(f),
            )
            if mem_id:
                result.proposed += 1
                result.proposals.append({
                    "id": mem_id, "category": f.category, "title": f.title,
                    "content": f.content[:400], "importance": f.importance,
                    "source_transcript": f.source_transcript,
                    "source_message": f.source_message,
                })
            else:
                result.duplicates += 1
        log.info(
            "ingested %d findings: %d proposed, %d duplicate",
            result.findings, result.proposed, result.duplicates,
        )
        return result

    async def ingest_transcripts(
        self, transcripts: list[Transcript], *, use_llm: bool = False,
        llm_concurrency: int = 5,
    ) -> IngestResult:
        """Analyze transcripts (heuristic + optional LLM pass) and ingest.

        The LLM pass runs CONCURRENTLY across transcripts (bounded by
        ``llm_concurrency``) so onboarding doesn't serialize N model calls. A
        transcript whose LLM call fails is skipped (its heuristic findings still
        land) — the pass is additive, never a hard dependency."""
        findings: list[Finding] = []
        for t in transcripts:
            findings.extend(analyze_transcript(t))

        if use_llm and self._llm_call is not None and transcripts:
            sem = asyncio.Semaphore(max(1, llm_concurrency))

            async def _one(t: Transcript) -> list[Finding]:
                async with sem:
                    return await analyze_transcript_llm(t, self._llm_call)

            for res in await asyncio.gather(
                *[_one(t) for t in transcripts], return_exceptions=True
            ):
                if isinstance(res, list):
                    findings.extend(res)
                else:
                    log.warning("LLM analysis of a transcript failed: %s", res)

        result = await self.ingest_findings(findings)
        result.transcripts = len(transcripts)
        return result

    async def ingest(self, *, days: int = 30, use_llm: bool = False) -> IngestResult:
        """End-to-end: extract recent transcripts, analyze, ingest. The single
        entry point the CLI / onboarding / periodic re-analysis call."""
        transcripts = extract_transcripts(days=days)
        return await self.ingest_transcripts(transcripts, use_llm=use_llm)
