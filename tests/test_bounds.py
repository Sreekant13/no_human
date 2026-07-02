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


# --- Phase 7e: doom-loop detection via tool-call signatures --- #

def test_doom_loop_after_three_identical():
    """Three consecutive identical tool calls → doom-loop fires."""
    d = StuckDetector(doom_loop_threshold=3)
    assert d.record_tool_call("Read", "/src/foo.py") is False
    assert d.record_tool_call("Read", "/src/foo.py") is False
    assert d.record_tool_call("Read", "/src/foo.py") is True  # 3rd → stuck


def test_doom_loop_not_triggered_by_interleaved():
    """Different calls interleaved should NOT trigger doom-loop."""
    d = StuckDetector(doom_loop_threshold=3)
    assert d.record_tool_call("Read", "/src/foo.py") is False
    assert d.record_tool_call("Edit", "/src/bar.py") is False
    assert d.record_tool_call("Read", "/src/foo.py") is False  # reset streak
    assert d.record_tool_call("Read", "/src/foo.py") is False  # only 2


def test_doom_loop_health_reflects_state():
    d = StuckDetector(doom_loop_threshold=3)
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Read", "/a.py")
    h = d.health
    assert h["consecutive_repeats"] == 2
    assert h["total_tool_calls"] == 2


def test_doom_loop_resets_on_different_call():
    d = StuckDetector(doom_loop_threshold=3)
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Grep", "pattern|/src")  # different → resets
    assert d.health["consecutive_repeats"] == 1
    assert d.record_tool_call("Grep", "pattern|/src") is False  # only 2
    assert d.record_tool_call("Grep", "pattern|/src") is True   # 3rd → stuck
