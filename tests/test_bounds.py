"""Termination bounds + stuck detection (§3.5)."""

from no_human.core.bounds import Bounds, StuckDetector, error_signature


def test_signature_stable_across_volatile_tokens():
    a = "Error at /Users/x/foo.py:42:7 ref 0x7ffabc id deadbeefcafe"
    b = "Error at /Users/y/bar.py:99:1 ref 0x1234ff id 0011223344ff"
    assert error_signature(a) == error_signature(b)


def test_signature_differs_on_real_change():
    a = "AssertionError: expected 1 got 2"
    b = "TypeError: cannot add str and int"
    assert error_signature(a) != error_signature(b)


def test_stuck_after_repeat():
    d = StuckDetector(threshold=2)
    assert d.record("AssertionError: expected 1 got 2") is False
    assert d.record("AssertionError: expected 1 got 2") is True  # same -> stuck


def test_not_stuck_on_progress():
    d = StuckDetector(threshold=2)
    assert d.record("AssertionError: expected 1 got 2") is False
    assert d.record("TypeError: different failure entirely") is False


def test_bounds_from_config_defaults():
    b = Bounds.from_config(None)
    assert b.max_attempts == 3
    assert b.max_turns_per_attempt == 60


def test_bounds_from_config_override():
    b = Bounds.from_config({"max_attempts": 5, "max_turns_per_attempt": 10})
    assert b.max_attempts == 5
    assert b.max_turns_per_attempt == 10
