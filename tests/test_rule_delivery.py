"""Tests for tiered rule delivery (Phase 5a).

Verifies the hard character caps on _format_active_memories and the
importance-based tiering.
"""

from no_human.core.orchestrator import Orchestrator


def _make_orch_with_memories(memories):
    """Create a minimal Orchestrator with stubbed _active_memories."""
    # Orchestrator needs a backend and store — use None since we only test
    # the pure _format_active_memories method.
    orch = object.__new__(Orchestrator)
    orch._active_memories = memories
    return orch


def _mem(title, content, importance="med", mem_type="rule"):
    tags = [f"importance:{importance}"]
    return {"title": title, "content": content, "type": mem_type, "tags": tags}


# ── Tiering ──────────────────────────────────────────────────────────── #


def test_empty_memories():
    orch = _make_orch_with_memories([])
    assert orch._format_active_memories() == ""


def test_none_memories():
    orch = _make_orch_with_memories(None)
    assert orch._format_active_memories() == ""


def test_critical_rules_full_content():
    """High-importance rules appear with full content, not truncated."""
    long_content = "x" * 500
    orch = _make_orch_with_memories([_mem("Rule A", long_content, "high")])
    result = orch._format_active_memories()
    assert "Critical rules" in result
    assert long_content in result


def test_relevant_rules_compact():
    """Med-importance rules appear with compact (200-char) content."""
    content = "y" * 300
    orch = _make_orch_with_memories([_mem("Rule B", content, "med")])
    result = orch._format_active_memories()
    assert "Relevant rules" in result
    assert content[:200] in result
    assert content not in result  # truncated at 200


def test_low_importance_title_only():
    """Low-importance rules appear with title only."""
    orch = _make_orch_with_memories([
        _mem("Rule C", "detailed content here", "low"),
    ])
    result = orch._format_active_memories()
    assert "Additional context" in result
    assert "Rule C" in result
    assert "detailed content here" not in result


def test_mixed_tiers():
    orch = _make_orch_with_memories([
        _mem("Critical", "important stuff", "high"),
        _mem("Relevant", "medium stuff", "med"),
        _mem("LowPrio", "low stuff", "low"),
    ])
    result = orch._format_active_memories()
    assert "Critical rules" in result
    assert "Relevant rules" in result
    assert "Additional context" in result


# ── Hard caps ────────────────────────────────────────────────────────── #


def test_critical_cap_enforced():
    """The critical block must never exceed _RULES_CRITICAL_CAP characters."""
    big_rules = [_mem(f"Rule {i}", "x" * 400, "high") for i in range(50)]
    orch = _make_orch_with_memories(big_rules)
    result = orch._format_active_memories()
    # Extract the critical section
    lines = result.split("\n\n")
    crit_block = next((b for b in lines if "Critical rules" in b), "")
    # The critical block must be within the cap + header overhead
    assert len(crit_block) <= Orchestrator._RULES_CRITICAL_CAP + 200  # header


def test_relevant_cap_enforced():
    """The relevant block must never exceed _RULES_RELEVANT_CAP characters."""
    big_rules = [_mem(f"Rule {i}", "y" * 200, "med") for i in range(50)]
    orch = _make_orch_with_memories(big_rules)
    result = orch._format_active_memories()
    lines = result.split("\n\n")
    rel_block = next((b for b in lines if "Relevant rules" in b), "")
    assert len(rel_block) <= Orchestrator._RULES_RELEVANT_CAP + 200  # header


def test_default_importance_is_relevant():
    """Memories without importance tags go to the 'relevant' tier."""
    orch = _make_orch_with_memories([
        {"title": "No Tags", "content": "stuff", "type": "rule", "tags": []},
    ])
    result = orch._format_active_memories()
    assert "Relevant rules" in result
    assert "No Tags" in result
