"""Pre-flight feasibility hint — a pure function of the task (feature #1)."""

from no_human.core.feasibility import (
    BAND_LARGE,
    BAND_TOO_LARGE,
    OFFER_CLARIFY,
    OFFER_SPLIT,
    estimate_feasibility,
)
from no_human.core.task import Task

RATES = {"simple": 60, "standard": 50, "complex": 38}


def _task(*, description="short", criteria=None, linked=None, verdict=None):
    t = Task.new("A task", description=description)
    t.acceptance_criteria = criteria or ["do the thing"]
    t.linked_repos = linked or []
    ctx = {}
    if verdict is not None:
        ctx["eval_result"] = {"verdict": verdict}
    t.context = ctx
    return t


def test_a_simple_task_gets_no_hint():
    # Nothing worth offering → no nag.
    assert estimate_feasibility(_task(), RATES) is None


def test_a_decompose_verdict_is_too_large_and_offers_split():
    hint = estimate_feasibility(_task(verdict="decompose"), RATES)
    assert hint is not None
    assert hint.band == BAND_TOO_LARGE
    assert hint.offer == OFFER_SPLIT
    # The tier at a decompose verdict is complex, so the rate is the complex one.
    assert hint.done_rate_pct == 38


def test_a_complex_tier_is_large_and_offers_split():
    # Two signals (multi-repo + long-spec) → complex, no decompose verdict.
    hint = estimate_feasibility(
        _task(description="x" * 2100, linked=["/other/repo"]), RATES)
    assert hint is not None
    assert hint.tier == "complex"
    assert hint.band == BAND_LARGE
    assert hint.offer == OFFER_SPLIT


def test_an_ambiguous_clarify_verdict_offers_clarify():
    # verdict=clarify is one signal (standard tier), not complex → clarify path.
    hint = estimate_feasibility(_task(verdict="clarify"), RATES)
    assert hint is not None
    assert hint.band == BAND_LARGE
    assert hint.offer == OFFER_CLARIFY


def test_the_message_names_a_rate_and_never_claims_failure():
    hint = estimate_feasibility(_task(verdict="decompose"), RATES)
    msg = hint.message()
    assert "38%" in msg
    assert "finished in one pass" in msg
    # Honest copy: it must not tell the user the task WILL fail.
    assert "will fail" not in msg.lower()


def test_missing_calibration_omits_the_number_but_still_hints():
    hint = estimate_feasibility(_task(verdict="decompose"), done_rate_by_tier=None)
    assert hint is not None
    assert hint.done_rate_pct is None
    assert "%" not in hint.message()


def test_fail_open_returns_none_on_a_bad_task():
    # A task the tier computation can't read must not raise into the create path.
    assert estimate_feasibility(object(), RATES) is None  # type: ignore[arg-type]
