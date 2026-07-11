"""Phase 0.3: terminal agent errors must be classified, not lumped into one
undifferentiated 'agent_error'. A refusal must be distinguishable (it fails
fast; a retry just refuses again) from a retryable rate-limit/infra/error."""

from no_human.core.orchestrator import _classify_error


def test_refusal_from_stop_reason():
    # the API's own stop_reason — the reliable signal the fast-fail path uses
    assert _classify_error("refusal", "") == "refusal"
    assert _classify_error("REFUSAL", "I can't help with that") == "refusal"


def test_quota_beats_generic():
    assert _classify_error(None, "usage limit reached for today") == "quota"
    assert _classify_error("end_turn", "quota exceeded") == "quota"


def test_rate_limited_from_status_or_text():
    assert _classify_error(None, "", 429) == "rate_limited"
    assert _classify_error(None, "", 529) == "rate_limited"
    assert _classify_error(None, "Error: Overloaded") == "rate_limited"
    assert _classify_error(None, "rate limit hit") == "rate_limited"


def test_max_turns():
    assert _classify_error("max_turns", "") == "max_turns"
    assert _classify_error(None, "reached the maximum number of turns") == "max_turns"


def test_infra_transient():
    assert _classify_error(None, "Stream closed unexpectedly") == "infra"
    assert _classify_error(None, "connection error to the API") == "infra"


def test_plain_error_is_the_fallback():
    # a normal completion that errored for an unknown reason stays retryable
    assert _classify_error("end_turn", "AssertionError: boom") == "error"
    assert _classify_error(None, "") == "error"


def test_refusal_takes_priority_over_other_signals():
    # even if the refusal text mentions a limit, stop_reason=refusal wins
    assert _classify_error("refusal", "I won't exceed the rate limit") == "refusal"
