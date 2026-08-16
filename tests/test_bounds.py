"""Termination bounds + stuck detection (§3.5)."""

from no_human.core.bounds import Bounds, StuckDetector, error_signature
from no_human.core.pricing import weighted_tokens


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
    """Assert against the dataclass, not a literal: this test used to hardcode
    60, so it was a third copy of a default that already lived in two places."""
    b = Bounds.from_config(None)
    assert b == Bounds()
    assert b.max_attempts == Bounds().max_attempts
    assert b.max_turns_per_attempt == Bounds().max_turns_per_attempt


def test_bounds_from_config_override():
    b = Bounds.from_config({"max_attempts": 5, "max_turns_per_attempt": 10})
    assert b.max_attempts == 5
    assert b.max_turns_per_attempt == 10


def test_turns_for_simple_task_uses_base():
    b = Bounds.from_config(None)
    assert b.turns_for(complex_task=False) == b.max_turns_per_attempt


def test_turns_for_complex_task_scaled():
    b = Bounds.from_config(None)
    expected = int(b.max_turns_per_attempt * b.complex_multiplier)
    assert b.turns_for(complex_task=True) == expected
    assert expected > b.max_turns_per_attempt


def test_turns_for_multiplier_one_disables_bump():
    b = Bounds.from_config({"max_turns_per_attempt": 60, "complex_multiplier": 1.0})
    assert b.turns_for(complex_task=True) == 60


def test_complex_multiplier_from_config():
    b = Bounds.from_config({"max_turns_per_attempt": 40, "complex_multiplier": 2.0})
    assert b.turns_for(complex_task=True) == 80


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




def test_doom_loop_resets_on_different_call():
    d = StuckDetector(doom_loop_threshold=3)
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Grep", "pattern|/src")  # different → resets
    assert d.record_tool_call("Grep", "pattern|/src") is False  # only 2
    assert d.record_tool_call("Grep", "pattern|/src") is True   # 3rd → stuck


# --- R2.3 Layer 1: per-file edit-count loop detection --- #

def test_edit_loop_after_threshold():
    d = StuckDetector(edit_threshold=3)
    assert d.record_edit("/src/foo.py") is False
    assert d.record_edit("/src/foo.py") is False
    assert d.record_edit("/src/foo.py") is True  # 3rd edit → stuck


def test_edit_loop_different_files_dont_trigger():
    d = StuckDetector(edit_threshold=3)
    assert d.record_edit("/src/foo.py") is False
    assert d.record_edit("/src/bar.py") is False
    assert d.record_edit("/src/foo.py") is False  # foo.py only at 2


def test_edit_loop_reflected_in_stuck_reason():
    d = StuckDetector(edit_threshold=2)
    d.record_edit("/src/foo.py")
    d.record_edit("/src/foo.py")
    assert d.stuck_reason is not None
    assert "edit-loop" in d.stuck_reason
    assert "/src/foo.py" in d.stuck_reason


# --- R2.1: ping-pong (A-B-A-B) detection --- #

def test_ping_pong_detected_on_alternating_pattern():
    d = StuckDetector()
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Edit", "/b.py")
    d.record_tool_call("Read", "/a.py")
    assert d.detect_ping_pong() is False  # only 3 calls, need 4
    d.record_tool_call("Edit", "/b.py")
    assert d.detect_ping_pong() is True  # A-B-A-B


def test_ping_pong_not_detected_on_forward_progress():
    d = StuckDetector()
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Edit", "/b.py")
    d.record_tool_call("Read", "/c.py")
    d.record_tool_call("Edit", "/d.py")
    assert d.detect_ping_pong() is False


def test_ping_pong_reflected_in_stuck_reason():
    d = StuckDetector()
    for sig in [("Read", "/a.py"), ("Edit", "/b.py")] * 2:
        d.record_tool_call(*sig)
    assert d.stuck_reason is not None
    assert "ping-pong" in d.stuck_reason


def test_stuck_reason_prioritizes_doom_loop_over_others():
    """doom-loop (identical repeats) should win over a coincidental edit-loop."""
    d = StuckDetector(doom_loop_threshold=2, edit_threshold=2)
    d.record_edit("/a.py")
    d.record_edit("/a.py")  # edit-loop condition also true
    d.record_tool_call("Edit", "/a.py")
    d.record_tool_call("Edit", "/a.py")  # doom-loop condition also true
    assert d.stuck_reason is not None
    assert d.stuck_reason.startswith("doom-loop")


def test_stuck_reason_none_when_healthy():
    d = StuckDetector()
    d.record_tool_call("Read", "/a.py")
    assert d.stuck_reason is None


# ------------------- hard-abort thresholds (ARCH_REVIEW B2 #1) -------------- #
# Advisory thresholds emit telemetry; hard thresholds end the attempt. A
# recognized loop used to be allowed to burn the whole 500-turn budget (live
# precedent: 3.4M cache-read in 41 turns). The hard tier is deliberately far
# above the advisory tier so it only fires on unambiguous runaways.


def test_hard_stuck_none_below_doom_abort_threshold():
    d = StuckDetector()
    for _ in range(d.doom_loop_abort - 1):
        d.record_tool_call("Bash", "pytest -x")
    assert d.stuck_reason is not None  # advisory fired long ago
    assert d.hard_stuck_reason is None  # but no abort yet


def test_hard_stuck_doom_loop_at_abort_threshold():
    d = StuckDetector()
    for _ in range(d.doom_loop_abort):
        d.record_tool_call("Bash", "pytest -x")
    assert d.hard_stuck_reason is not None
    assert "doom-loop" in d.hard_stuck_reason


def test_hard_stuck_edit_loop_at_abort_threshold():
    d = StuckDetector()
    for _ in range(d.edit_abort - 1):
        d.record_edit("/a.py")
    assert d.hard_stuck_reason is None
    d.record_edit("/a.py")
    assert d.hard_stuck_reason is not None
    assert "edit-loop" in d.hard_stuck_reason


def test_hard_stuck_sustained_ping_pong():
    d = StuckDetector()
    for _ in range(5):  # 10 alternating calls — advisory, not abort
        d.record_tool_call("Read", "/a.py")
        d.record_tool_call("Edit", "/b.py")
    assert d.hard_stuck_reason is None
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Edit", "/b.py")  # 12 alternating calls — sustained
    assert d.hard_stuck_reason is not None
    assert "ping-pong" in d.hard_stuck_reason


def test_hard_stuck_not_fooled_by_progress():
    d = StuckDetector()
    for i in range(30):
        d.record_tool_call("Read", f"/file{i}.py")
    assert d.hard_stuck_reason is None


def test_attempt_tokens_default_and_override():
    """Per-attempt spend cap (v6: four specs burned the whole lifetime budget
    in attempt #1). Default must clear the largest measured successful attempt
    (3.06M raw complex-tier cache-read = 306,000 cost-weighted) with headroom,
    and stay well under the lifetime cap so the bounded loop keeps at least two
    real attempts.

    Both caps are COST-WEIGHTED tokens since 2026-07-31 (core.pricing). Raised
    2026-08-03 (800k -> 2M with lifetime 1.6M -> 4M) from the honest-ledger
    sweep: the converted caps were calibrated on the pre-fix ledger whose
    subagent spend was under-counted — against honest numbers the old lifetime
    cap parked 52.9% of real tasks and the old attempt cap ended 31% of real
    attempts. Derivation and the re-sweep obligation live on core.bounds.Bounds."""
    b = Bounds()
    assert b.attempt_tokens == 2_000_000
    # The measured complex attempt (3.06M raw, ~all cache-read) in the cap's
    # own unit: 3_060_000 x CACHE_READ_WEIGHT.
    assert b.attempt_tokens > weighted_tokens(cache_read_tokens=3_060_000)
    assert b.attempt_tokens <= b.lifetime_tokens // 2
    assert Bounds.from_config({"attempt_tokens": 123}).attempt_tokens == 123


def test_investigation_overlay_keeps_base_caps():
    """Review D10: the investigation/design_doc bounds overlay must inherit
    the configured token caps, not silently revert them to dataclass
    defaults for exactly the kinds that produce reports."""
    base = {"attempt_tokens": 123, "lifetime_tokens": 456,
            "max_attempts": 3, "max_turns_per_attempt": 500}
    inv = {"max_attempts": 8, "max_turns_per_attempt": 80}
    merged = {**base, "max_attempts": inv["max_attempts"],
              "max_turns_per_attempt": inv["max_turns_per_attempt"]}
    b = Bounds.from_config(merged)
    assert b.max_attempts == 8
    assert b.max_turns_per_attempt == 80
    assert b.attempt_tokens == 123
    assert b.lifetime_tokens == 456


# ------- 2026-08-16 false doom-loop regression (ticket 32fae028) ----------- #
# Two healthy attempts were hard-aborted as "identical tool call repeated 9x"
# while window-reading one 2,805-line file: the Read signature was the path
# alone, and bounds' [:100] prefix truncation re-collapsed whatever the
# summarizer did distinguish behind a ~95-char worktree path. These tests run
# the REAL pipeline — _summarize_tool_sig output fed to record_tool_call —
# with incident-shaped inputs, in both directions (progress stays quiet, true
# repeats still fire).

from no_human.core.orchestrator import _summarize_tool_sig  # noqa: E402

# Same order of magnitude as the worktree paths in the incident (~95 chars).
_WT = ("/private/tmp/claude-501/some-project-slug/0000000000000000/scratchpad/"
       "train44/web/src/SlideOver.jsx")
assert len(_WT) > 90


def test_windowed_reads_of_one_long_path_file_are_progress():
    """The incident, replayed: nine different windows of one file must not
    fire the detector even though the path eats the whole truncation head."""
    d = StuckDetector(doom_loop_threshold=3)
    for i, offset in enumerate([1, 200, 415, 600, 800, 1000, 1400, 2000, 2400]):
        sig = _summarize_tool_sig("Read", {"file_path": _WT,
                                           "offset": offset, "limit": 200})
        assert d.record_tool_call("Read", sig) is False, f"fired at window {i}"


def test_rereading_the_same_window_is_still_a_loop():
    d = StuckDetector(doom_loop_threshold=3)
    sig = _summarize_tool_sig("Read", {"file_path": _WT,
                                       "offset": 415, "limit": 200})
    assert d.record_tool_call("Read", sig) is False
    assert d.record_tool_call("Read", sig) is False
    assert d.record_tool_call("Read", sig) is True  # 3rd identical → stuck


def test_different_edits_to_one_file_are_progress():
    d = StuckDetector(doom_loop_threshold=3)
    for i in range(9):
        sig = _summarize_tool_sig("Edit", {
            "file_path": _WT,
            "old_string": f"const before{i} = null",
            "new_string": f"const after{i} = null",
        })
        assert d.record_tool_call("Edit", sig) is False, f"fired at edit {i}"


def test_identical_edit_retried_is_still_a_loop():
    d = StuckDetector(doom_loop_threshold=3)
    inp = {"file_path": _WT, "old_string": "a", "new_string": "b"}
    assert d.record_tool_call("Edit", _summarize_tool_sig("Edit", inp)) is False
    assert d.record_tool_call("Edit", _summarize_tool_sig("Edit", inp)) is False
    assert d.record_tool_call("Edit", _summarize_tool_sig("Edit", inp)) is True


def test_distinct_bash_commands_sharing_a_long_prefix_are_progress():
    """Long worktree-path prefixes made distinct commands collide inside any
    prefix truncation; the trailing command hash must keep them apart."""
    d = StuckDetector(doom_loop_threshold=3)
    for target in ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"):
        sig = _summarize_tool_sig(
            "Bash", {"command": f"cd {_WT[:70]} && python -m pytest {target} -q"})
        assert d.record_tool_call("Bash", sig) is False


def test_identical_bash_command_retried_is_still_a_loop():
    d = StuckDetector(doom_loop_threshold=3)
    cmd = {"command": f"cd {_WT[:70]} && python -m pytest tests/test_a.py -q"}
    assert d.record_tool_call("Bash", _summarize_tool_sig("Bash", cmd)) is False
    assert d.record_tool_call("Bash", _summarize_tool_sig("Bash", cmd)) is False
    assert d.record_tool_call("Bash", _summarize_tool_sig("Bash", cmd)) is True


def test_pdf_page_walk_is_progress_and_replace_all_is_a_different_edit():
    """Review follow-ups: `pages` discriminates Read (a page-by-page PDF walk
    must not collapse to one signature), and `replace_all` discriminates Edit
    (a failed unique-match Edit retried with replace_all is a new action)."""
    d = StuckDetector(doom_loop_threshold=3)
    for pages in ("1-5", "6-10", "11-15"):
        sig = _summarize_tool_sig("Read", {"file_path": _WT, "pages": pages})
        assert d.record_tool_call("Read", sig) is False
    e1 = _summarize_tool_sig("Edit", {"file_path": _WT, "old_string": "a",
                                      "new_string": "b"})
    e2 = _summarize_tool_sig("Edit", {"file_path": _WT, "old_string": "a",
                                      "new_string": "b", "replace_all": True})
    assert e1 != e2
