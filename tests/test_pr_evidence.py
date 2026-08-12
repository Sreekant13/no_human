"""Part 2 of the c7da49d4 decomposition: the evidence PIPELINE.

`core/pr_evidence.py`'s `PrEvidence` is the single structured object every
truth pin (a concrete, measured-fact sentence) in the PR body must be backed
by — gathered once by `Orchestrator._gather_evidence` and threaded through
every section renderer. These tests hold that guarantee against the REAL
renderer (`Orchestrator._pr_body`), not against a hand-built string: a
checker that only reads `PrEvidence.truth_pins()` in isolation could pass
while the renderer quietly drifted away from it.
"""
import re

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.pr_evidence import PrEvidence, visible_chars
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


class _Commit:
    files_changed = 4
    insertions = 120
    deletions = 18
    sha = "a" * 40


class _Result:
    final_text = ("Added the retry-with-backoff wrapper around the flaky "
                  "webhook call and a regression test that pins the 3-retry "
                  "cap.")
    num_turns = 9


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _receipts():
    cmds = [
        ("uv run pytest tests/test_webhook_retry.py -q",
         "5 passed in 1.21s", "test"),
        ("uv run ruff check src/no_human/net/webhook.py",
         "All checks passed!", "lint"),
    ]
    return [{"command": c, "output_excerpt": o, "kind": k, "truncated": False,
             "output_bytes": len(o)} for c, o, k in cmds]


def _task():
    """A representative task: the fixture criterion 3's char-count numbers
    and the truth-pin predicate below are both driven against."""
    t = Task.new("retry the flaky webhook call", repo_path="/r")
    t.acceptance_criteria = [
        "webhook POST retries up to 3 times on a 5xx before failing",
        "a regression test pins the retry cap and the backoff delay",
    ]
    t.context = {"review_history": [
        {"round": 1, "sha": "a" * 40, "passed": False,
         "blocking": ["the backoff delay is not tested"]},
        {"round": 2, "sha": "a" * 40, "passed": True, "blocking": []},
    ]}
    return t


# Every regex family a "truth pin" — a concrete, measured-fact sentence — can
# take in the rendered body. Anything matching one of these must be a value in
# `evidence.truth_pins()`; anything in `evidence.truth_pins()` must appear in
# the body verbatim. Both directions are checked by
# `test_every_rendered_truth_pin_has_backing_evidence` below.
_PIN_PATTERNS = [
    re.compile(r"independent review rounds: \d+; final verdict: \*\*[^*]+\*\*"),
    re.compile(r"\d+ verification command\(s\) were recorded during this attempt\."),
    re.compile(r"tests: (?:PASS|FAIL) — \d+ passed, \d+ failed, \d+ errors"),
    re.compile(r"CI state: .+"),
]


def _pins_in_body(body: str) -> list[str]:
    found: list[str] = []
    for pat in _PIN_PATTERNS:
        found.extend(m.group(0) for m in pat.finditer(body))
    return found


def test_every_rendered_truth_pin_has_backing_evidence(store, tmp_path):
    """ACCEPTANCE CRITERION 2's predicate: every truth pin the PR body
    renders is a value of `evidence.truth_pins()` — so a fact printed in the
    body can never drift from the evidence object supposed to back it.
    Driven against a REAL `_pr_body()` render, not a hand-built string; see
    `test_the_predicate_catches_an_unbacked_truth_pin` for proof this check
    actually fails on an unbacked pin rather than passing vacuously.
    """
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    head_sha = _Commit.sha
    evidence = orch._gather_evidence(
        task, test_evidence=test_evidence, receipts=receipts,
        head_sha=head_sha)
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)

    pins_backed = set(evidence.truth_pins().values())
    pins_rendered = _pins_in_body(body)
    assert pins_rendered, (
        "the fixture renders no truth pin at all — the predicate below "
        "would pass vacuously")
    for pin in pins_rendered:
        assert pin in pins_backed, (
            f"truth pin rendered with NO backing evidence field: {pin!r}\n"
            f"evidence.truth_pins() = {evidence.truth_pins()!r}")

    # ...and the other direction: every pin the object claims to back really
    # does reach the body. Declaring a pin nobody renders is not a lie, but a
    # renderer that stops reading an object's own field IS the drift this
    # module exists to prevent.
    for field, pin in evidence.truth_pins().items():
        assert pin in body, (
            f"evidence.{field} backs {pin!r}, which the body never renders")


def test_the_predicate_catches_an_unbacked_truth_pin():
    """NEGATIVE HALF: a fact-shaped sentence with NO backing field must fail
    the predicate above — proving it is a real check, not one that always
    passes. `evidence.tests` is `None` here, so a stray "tests: PASS" line is
    exactly the failure this predicate exists to catch.
    """
    evidence = PrEvidence(tests=None)
    stray_body = "## Evidence\n- tests: PASS — 99 passed, 0 failed, 0 errors\n"
    pins_rendered = _pins_in_body(stray_body)
    assert pins_rendered == ["tests: PASS — 99 passed, 0 failed, 0 errors"]
    assert pins_rendered[0] not in set(evidence.truth_pins().values()), (
        "the negative fixture must NOT be backed — otherwise this test "
        "proves nothing about the predicate's sensitivity")


def test_evidence_gathered_once_backs_every_section(store, tmp_path):
    """CRITERION 1: sections render EXCLUSIVELY from the `PrEvidence`
    instance `_pr_body` gathers once — not a second, independent read of
    `task.context`. Proven by mutating `task.context` AFTER gathering: a
    section that still re-read `task.context` directly would show the
    mutated (2-round) history instead of the gathered (1-round) snapshot.
    """
    orch = _orch(store, tmp_path)
    task = _task()
    task.context = {"review_history": [
        {"round": 1, "sha": "a" * 40, "passed": True, "blocking": []},
    ]}
    evidence = orch._gather_evidence(task, head_sha="a" * 40)
    assert evidence.review_verdict == {
        "rounds": 1, "verdict": "PASSED", "addressed": []}

    task.context = {"review_history": [
        {"round": 1, "sha": "a" * 40, "passed": True, "blocking": []},
        {"round": 2, "sha": "a" * 40, "passed": True, "blocking": []},
    ]}
    section = orch._review_evidence_section(
        task, head_sha="a" * 40, evidence=evidence)
    assert "review rounds: 1" in section
    assert "review rounds: 2" not in section


def test_no_gate_output_is_duplicated_in_the_rendered_body(store, tmp_path):
    """CRITERION 5: repro/tests/review_verdict each appear exactly once in
    the rendered body — no gate output is duplicated inline anymore."""
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    assert body.count("independent review rounds:") == 1
    assert body.count("verification command(s) were recorded") == 1
    assert body.count("- tests: PASS") == 1


def test_the_pr_body_char_count_drops_at_least_10_percent_when_collapsed(
        store, tmp_path):
    """CRITERION 3, executable: the SCANNABLE character count (everything
    outside a `<details>` fold — what a reader sees without expanding
    anything, since GitHub renders `<details>` closed by default) drops by
    at least 10% relative to the raw total, on this module's representative
    fixture (`_task()` / `_receipts()` above). The same fixture, measured
    end-to-end before and after this change (reconstructing "before" from
    the unwrapped `Orchestrator._verification_section` output, which this
    change does not alter — see the commit message for the exact command):
    6442 chars total/visible before (no `<details>` existed yet), 6536 total
    (+1.5%) / 890 visible after — an 86.2% drop in what a reader actually
    scans without expanding anything.
    """
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts)
    total = len(body)
    visible = visible_chars(body)
    assert visible < total
    reduction = (total - visible) / total
    assert reduction >= 0.10, f"only a {reduction:.1%} reduction: {body!r}"
