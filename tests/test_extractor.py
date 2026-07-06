"""Tests for history/extractor.py — multi-language-server discovery and merging."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from no_human.history.extractor import (
    LanguageServerClient,
    Transcript,
    extract_transcripts,
)


# --------------------------------------------------------------------------- #
# Helpers: fake LanguageServerClient                                           #
# --------------------------------------------------------------------------- #

def _fake_client(summaries: dict, steps_by_id: dict | None = None) -> LanguageServerClient:
    """Create a LanguageServerClient whose .call() returns canned data."""
    client = MagicMock(spec=LanguageServerClient)
    client.host = "127.0.0.1"
    client.port = 12345

    def _call(method, body=None):
        if method == "GetAllCascadeTrajectories":
            return {"trajectorySummaries": summaries}
        if method == "GetCascadeTrajectorySteps":
            cid = (body or {}).get("cascadeId", "")
            return {"steps": (steps_by_id or {}).get(cid, [])}
        return {}

    client.call = _call
    return client


def _make_summary(cid: str, title: str, *, hours_ago: int = 1) -> tuple[str, dict]:
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return cid, {"summary": title, "createdTime": ts, "stepCount": 2, "workspaces": []}


def _make_steps(text: str) -> list[dict]:
    return [
        {"type": "CORTEX_STEP_TYPE_USER_INPUT",
         "userInput": {"userResponse": "hello"}},
        {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
         "plannerResponse": {"response": text}},
    ]


# --------------------------------------------------------------------------- #
# extract_transcripts with a single client (backward compat)                   #
# --------------------------------------------------------------------------- #

def test_extract_single_client():
    cid, summary = _make_summary("c1", "Chat 1")
    client = _fake_client({cid: summary}, {"c1": _make_steps("reply 1")})

    result = extract_transcripts(days=7, client=client)

    assert len(result) == 1
    assert result[0].cascade_id == "c1"
    assert result[0].title == "Chat 1"
    assert len(result[0].messages) == 2


# --------------------------------------------------------------------------- #
# Multi-client merging: conversations deduped by cascade_id                    #
# --------------------------------------------------------------------------- #

def test_extract_merges_multiple_clients(monkeypatch):
    """When two servers have different conversations, both are returned."""
    cid1, sum1 = _make_summary("c1", "Chat from window 1")
    cid2, sum2 = _make_summary("c2", "Chat from window 2")

    client1 = _fake_client({cid1: sum1}, {"c1": _make_steps("r1")})
    client2 = _fake_client({cid2: sum2}, {"c2": _make_steps("r2")})

    # Patch _discover_all_clients to return both
    import no_human.history.extractor as ext_mod
    monkeypatch.setattr(ext_mod, "_discover_all_clients", lambda: [client1, client2])

    result = extract_transcripts(days=7)

    assert len(result) == 2
    ids = {t.cascade_id for t in result}
    assert ids == {"c1", "c2"}


def test_extract_deduplicates_across_clients(monkeypatch):
    """Same cascade_id from two servers → only one transcript."""
    cid, summary = _make_summary("shared", "Same chat")
    steps = _make_steps("reply")

    client1 = _fake_client({cid: summary}, {"shared": steps})
    client2 = _fake_client({cid: summary}, {"shared": steps})

    import no_human.history.extractor as ext_mod
    monkeypatch.setattr(ext_mod, "_discover_all_clients", lambda: [client1, client2])

    result = extract_transcripts(days=7)

    assert len(result) == 1
    assert result[0].cascade_id == "shared"


def test_extract_filters_by_days():
    """Conversations older than the cutoff are excluded."""
    cid_recent, sum_recent = _make_summary("recent", "Recent", hours_ago=1)
    cid_old, sum_old = _make_summary("old", "Old", hours_ago=72)

    client = _fake_client(
        {cid_recent: sum_recent, cid_old: sum_old},
        {"recent": _make_steps("r"), "old": _make_steps("r")},
    )

    result = extract_transcripts(days=2, client=client)

    assert len(result) == 1
    assert result[0].cascade_id == "recent"
