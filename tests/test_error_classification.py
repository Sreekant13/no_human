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


def test_a_wrapped_transport_message_classifies_the_same_as_an_unwrapped_one():
    """This classifier and `claude_backend.is_transport_failure` must not
    disagree about one failure — the comment on `_TRANSPORT_FAILURE_MARKERS`
    says so, and that is the only reason "connection error" is in the backend's
    list at all. When the backend learned to read a message a terminal had
    broken in half, this side had not: the wrapped shape was RETRIED as a
    transport death and then LABELLED `error`, so the retry and the routing
    described different incidents.
    """
    for text in ("Stream\nclosed unexpectedly by consumer",
                 "Stream  closed unexpectedly",
                 "\tStream\tclosed unexpectedly",
                 "API request failed with a connection\nerror: upstream hung up",
                 "reviewer timed\nout after 600s"):
        assert _classify_error(None, text) == "infra", text


def test_dewrapping_does_not_widen_what_counts_as_infra():
    """The control. Collapsing whitespace must change how a message is READ,
    never which messages are eligible — a version that returned "infra" more
    often would route ordinary failures to the auto-retrying transient path.
    """
    assert _classify_error("end_turn", "AssertionError:\nboom") == "error"
    assert _classify_error(None, "3 tests\nfailed, 2 passed") == "error"
    assert _classify_error(None, "  \n\t\n  ") == "error"
    # Earlier branches still win over the de-wrapped one.
    assert _classify_error("refusal", "Stream closed") == "refusal"
    assert _classify_error(None, "usage limit reached\nStream closed") == "quota"
    assert _classify_error(None, "Stream closed", 429) == "rate_limited"

    # RECORDED, NOT ENDORSED — and NOT introduced by the de-wrapping. This
    # classifier matches a marker ANYWHERE in the text, so an errored session
    # carrying the model's own prose about transports has always landed on
    # "infra" here, where `is_transport_failure` (which spends money) excludes
    # it. Narrowing this to the backend's corroborated-opening rule is a real
    # behaviour change at two call sites and belongs in its own change; if you
    # are making it, these two assertions are the ones to flip.
    prose = ("Done. Summary of the change:\n"
             "- added connection error handling to the poller\n")
    assert _classify_error(None, prose) == "infra"
    assert _classify_error(
        None, 'Fixed it.\nThe CLI says "Stream closed" and we retry.') == "infra"


def test_plain_error_is_the_fallback():
    # a normal completion that errored for an unknown reason stays retryable
    assert _classify_error("end_turn", "AssertionError: boom") == "error"
    assert _classify_error(None, "") == "error"


def test_refusal_takes_priority_over_other_signals():
    # even if the refusal text mentions a limit, stop_reason=refusal wins
    assert _classify_error("refusal", "I won't exceed the rate limit") == "refusal"
