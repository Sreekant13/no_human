"""Sprint 1: transcript findings → human-confirmed learning queue.

Proves the ingester routes analyzer Findings into the queue as
source="proposed"/confirmed=0 (never auto-active), is idempotent across runs,
carries provenance, and supports an optional LLM pass that emits LABELS not
scores.
"""

from __future__ import annotations

import pytest

from no_human.core.db import Store
from no_human.history.analyzer import (
    Finding,
    build_llm_prompt,
    parse_llm_findings,
)
from no_human.history.extractor import Message, Transcript
from no_human.history.ingester import TranscriptIngester, _dedupe_key
from no_human.learning import LearningQueue


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _finding(content="never push to main", cat="rule", cascade="c1", msg=2):
    return Finding(
        category=cat, title=f"{content[:40]}", content=content,
        source_transcript=cascade, source_title="A chat",
        tags=["history", cat, "user_correction"], source_message=msg,
    )


def _transcript():
    return Transcript(
        cascade_id="cascade-123", title="Retention Bug", created="2026-06-17",
        messages=[
            Message("assistant", "I'll take a look.", "STEP"),
            Message("user", "never commit secrets to the repo", "STEP"),
            Message("user", "you are going in circles, stop guessing", "STEP"),
        ],
    )


async def test_findings_enqueued_as_unconfirmed_proposals(store):
    ing = TranscriptIngester(store)
    res = await ing.ingest_findings([_finding()])
    assert res.proposed == 1
    # It lands in the learning queue's pending() (source="proposed", confirmed=0)
    q = LearningQueue(store)
    pending = await q.pending()
    assert len(pending) == 1
    assert pending[0]["confirmed"] == 0
    # ...and is absent from the active set until a human confirms it.
    assert await q.active() == []


async def test_ingest_is_idempotent(store):
    ing = TranscriptIngester(store)
    first = await ing.ingest_findings([_finding()])
    second = await ing.ingest_findings([_finding()])
    assert first.proposed == 1
    assert second.proposed == 0
    assert second.duplicates == 1
    # Only one proposal exists, not two.
    assert len(await LearningQueue(store).pending()) == 1


async def test_provenance_carried_in_tags(store):
    ing = TranscriptIngester(store)
    await ing.ingest_findings([_finding(cascade="cascade-xyz", msg=7)])
    pending = await LearningQueue(store).pending()
    tags = pending[0]["tags"]
    assert "src:cascade-xyz" in tags
    assert "msg:7" in tags


async def test_dedupe_key_distinguishes_content():
    a = _dedupe_key(_finding(content="rule A"))
    b = _dedupe_key(_finding(content="rule B"))
    assert a != b
    assert _dedupe_key(_finding(content="rule A")) == a


async def test_ingest_transcripts_runs_heuristic(store):
    ing = TranscriptIngester(store)
    res = await ing.ingest_transcripts([_transcript()])
    assert res.transcripts == 1
    assert res.proposed >= 1  # the heuristic catches "never commit" / "stop guessing"


# --------------------------------------------------------------------------- #
# Optional LLM pass                                                            #
# --------------------------------------------------------------------------- #

def test_llm_prompt_asks_for_label_not_score():
    prompt = build_llm_prompt(_transcript())
    assert "importance" in prompt
    assert "low | med | high" in prompt or "low|med|high" in prompt
    # Must NOT request a numeric 1-10 score (constraint #3).
    import re
    assert not re.search(r"score\s+\d+\s*[-–]\s*10", prompt, re.IGNORECASE)
    assert "NOT a number" in prompt or "NOT a score" in prompt


def test_parse_llm_findings_valid():
    text = (
        "FINDINGS_JSON_START\n"
        '{"findings": [{"category": "anti_pattern", "rule": "do not claim you '
        'cannot access a system before checking skills", "anti_pattern": '
        '"claimed cannot access PR", "source_message": 3, "importance": "high"}]}\n'
        "FINDINGS_JSON_END\n"
    )
    findings = parse_llm_findings(text, _transcript())
    assert len(findings) == 1
    f = findings[0]
    assert f.category == "anti_pattern"
    assert f.importance == "high"
    assert f.source_message == 3
    assert "importance:high" in f.tags


def test_parse_llm_findings_rejects_garbage_importance():
    text = (
        'FINDINGS_JSON_START\n{"findings": [{"category": "rule", "rule": "x", '
        '"importance": "11/10"}]}\nFINDINGS_JSON_END'
    )
    findings = parse_llm_findings(text, _transcript())
    assert findings[0].importance == "med"  # invalid label → safe default, not a number


def test_parse_llm_findings_no_block_is_empty():
    assert parse_llm_findings("no json here", _transcript()) == []


async def test_ingest_transcripts_with_llm_pass(store):
    async def fake_llm(prompt):
        return (
            'FINDINGS_JSON_START\n{"findings": [{"category": "skill", "rule": '
            '"use the tracker-test-cases skill for test linking", "importance": '
            '"med", "source_message": 1}]}\nFINDINGS_JSON_END'
        )

    ing = TranscriptIngester(store, llm_call=fake_llm)
    res = await ing.ingest_transcripts([_transcript()], use_llm=True)
    # heuristic findings + the one LLM finding
    pending = await LearningQueue(store).pending()
    titles = [p["title"] for p in pending]
    assert any("tracker-test-cases" in t for t in titles)
    assert res.proposed >= 2
