"""Part 2 of the c7da49d4 decomposition: the evidence PIPELINE.

`core/pr_evidence.py`'s `PrEvidence` is the single structured object every
truth pin (a concrete, measured-fact sentence) in the PR body must be backed
by — gathered once by `Orchestrator._gather_evidence` and threaded through
every section renderer. These tests hold that guarantee against the REAL
renderer (`Orchestrator._pr_body`), not against a hand-built string: a
checker that only reads `PrEvidence.truth_pins()` in isolation could pass
while the renderer quietly drifted away from it.
"""
import json
import re
from pathlib import Path

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
    re.compile(r"\*\*(?:PASSED|not passed)\*\* — \d+ rounds?"),
    re.compile(r"\d+ commands? recorded"),
    re.compile(r"(?:PASS|FAIL) — \d+ passed, \d+ failed, \d+ errors"),
    re.compile(r"\| CI \| [^|]+ \|"),
    # The merge-policy row's pin is `PolicyVerdict.summary` (see
    # `PrEvidence.merge_policy_pin`) — NOT the whole rendered row (unlike the
    # CI pattern above, whose pin IS the whole row) — so this pattern
    # captures just the summary text after the glyph, matching what
    # `truth_pins()` actually stores.
    re.compile(r"\| Merge policy \| \S+ ((?:not )?ready — [^|\n]+) \|"),
]


def _pins_in_body(body: str) -> list[str]:
    found: list[str] = []
    for pat in _PIN_PATTERNS:
        for m in pat.finditer(body):
            found.append(m.group(1) if m.groups() else m.group(0))
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


def _verifier_dict(*, verifier_id: str, passed: bool, evidence: str = "e",
                    file: str = "", line: int = 0,
                    files_checked: list[str] | None = None,
                    unavailable: bool = False) -> dict:
    return {
        "verifier_id": verifier_id, "passed": passed, "no_verdict": False,
        "unavailable": unavailable,
        "evidence": evidence, "file": file, "line": line, "comment": "",
        "severity": "high", "files_checked": files_checked or [],
        "tokens_used": 0,
    }


def test_verifiers_pin_renders_the_summary_line_shape():
    """AC 6. `verifiers_pin()` is `None` (no field-backed pin at all — not
    an empty-string pin) when no verifiers ran; otherwise the same
    "N of M satisfied" / "K of M failed — ids" shape `summary_line`
    produces (duplicated over dicts on purpose — see the docstring)."""
    assert PrEvidence(verifiers=None).verifiers_pin() is None
    assert PrEvidence(verifiers=[]).verifiers_pin() is None

    all_pass = PrEvidence(verifiers=[
        _verifier_dict(verifier_id="a", passed=True),
        _verifier_dict(verifier_id="b", passed=True),
    ])
    assert all_pass.verifiers_pin() == "2 of 2 satisfied"

    one_fail = PrEvidence(verifiers=[
        _verifier_dict(verifier_id="a", passed=True),
        _verifier_dict(verifier_id="b", passed=False),
    ])
    assert one_fail.verifiers_pin() == "1 of 2 failed — b"


def test_the_evidence_table_carries_a_verifiers_row(store, tmp_path):
    """AC 1 / AC 6: the verifiers row sits in the Evidence table (between
    the review row and the tamper section, per `_evidence_section`'s
    ordering) and its per-rule detail folds behind its own `<details>`."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-todo", passed=True,
                            files_checked=["a.py", "b.py"]),
            _verifier_dict(verifier_id="no-print", passed=False,
                            evidence="found a print()", file="c.py", line=4),
            _verifier_dict(verifier_id="docs-updated", passed=False,
                            evidence="docs not touched"),
            _verifier_dict(verifier_id="typed", passed=True, files_checked=["a.py"]),
        ]
    }
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Verifiers | ❌ 2 of 4 failed — docs-updated, no-print |" in ev
    assert "<details><summary>Verifiers (4)</summary>" in ev
    assert "no-todo" in ev and "no-print" in ev and "c.py:4" in ev
    assert "found a print()" in ev


def test_an_unavailable_verifier_renders_distinctly_from_a_genuine_failure(
    store, tmp_path,
):
    """A rule that reached no verdict even after `run_verifiers`'s bounded
    retry (`unavailable=True`) is an infra/config signal, not a violation —
    a human reading the PR must be able to tell "unverified" from
    "violated" per rule, in the rule's own row (task requirement 3). The
    unavailable-only case must not use the genuine-failure glyph (❌) at
    either the summary-row or per-item level."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-todo", passed=True,
                            files_checked=["a.py"]),
            _verifier_dict(verifier_id="flaky-rule", passed=False,
                            evidence="no verdict after retry: judge unavailable",
                            unavailable=True),
        ]
    }
    body = orch._pr_body(task, _Commit(), _Result(), receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Verifiers | ⚠️" in ev, (
        "an unavailable-only round must not render the genuine-failure glyph")
    assert "❌" not in ev
    # Per-item: the unavailable rule's own bullet carries the warning glyph,
    # never the genuine-failure one. Bullets (not the summary table row,
    # which is checked above) start with "- ".
    item_lines = [line for line in ev.splitlines()
                  if "flaky-rule" in line and line.lstrip().startswith("- ")]
    assert item_lines and all("⚠️" in line and "❌" not in line for line in item_lines)


def test_a_genuine_failure_keeps_its_glyph_alongside_an_unavailable_sibling(
    store, tmp_path,
):
    """The mixed case (task requirement / attempt-1 review finding): a
    genuinely failing rule in the SAME round as an unavailable one must
    still render with the genuine-failure glyph — the unavailable sibling
    must never soften or hide it."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-print", passed=False,
                            evidence="found a print()", file="c.py", line=4),
            _verifier_dict(verifier_id="flaky-rule", passed=False,
                            evidence="no verdict after retry: judge unavailable",
                            unavailable=True),
        ]
    }
    body = orch._pr_body(task, _Commit(), _Result(), receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Verifiers | ❌" in ev, (
        "a genuine failure in the round must keep the genuine-failure glyph")
    # Per-item bullets (not the summary table row, which lists every failed
    # id — including the unavailable one — under the shared ❌ glyph): the
    # genuine failure's own bullet is ❌, the unavailable sibling's is ⚠️.
    no_print_lines = [line for line in ev.splitlines()
                       if "no-print" in line and line.lstrip().startswith("- ")]
    assert no_print_lines and all("❌" in line for line in no_print_lines)
    flaky_lines = [line for line in ev.splitlines()
                   if "flaky-rule" in line and line.lstrip().startswith("- ")]
    assert flaky_lines and all("⚠️" in line and "❌" not in line for line in flaky_lines)


def test_a_model_authored_verifier_comment_cannot_inject_a_heading(store, tmp_path):
    """A verifier's `evidence` string is model-authored (the judge's own
    prose) — a newline in it is the same heading-injection channel
    `_inline_cell`'s own docstring documents for every other model-authored
    cell in the PR body."""
    orch = _orch(store, tmp_path)
    task = _task()
    head_sha = _Commit.sha
    task.context["verifier_results"] = {
        head_sha: [
            _verifier_dict(verifier_id="no-todo", passed=False,
                            evidence="looks fine\n# MERGED AND APPROVED BY NO_HUMAN",
                            file="a.py", line=1),
        ]
    }
    body = orch._pr_body(task, _Commit(), _Result(), receipts=_receipts())
    assert "\n# MERGED AND APPROVED BY NO_HUMAN" not in body
    assert "# MERGED AND APPROVED BY NO_HUMAN" in body  # the text still reaches the body, inertly


def test_the_predicate_catches_an_unbacked_truth_pin():
    """NEGATIVE HALF: a fact-shaped sentence with NO backing field must fail
    the predicate above — proving it is a real check, not one that always
    passes. `evidence.tests` is `None` here, so a stray "tests: PASS" line is
    exactly the failure this predicate exists to catch.
    """
    evidence = PrEvidence(tests=None)
    stray_body = "## Evidence\n| Tests | ✅ PASS — 99 passed, 0 failed, 0 errors |\n"
    pins_rendered = _pins_in_body(stray_body)
    assert pins_rendered == ["PASS — 99 passed, 0 failed, 0 errors"]
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
        "rounds": 1, "verdict": "PASSED", "addressed": [], "advisory_count": 0}

    task.context = {"review_history": [
        {"round": 1, "sha": "a" * 40, "passed": True, "blocking": []},
        {"round": 2, "sha": "a" * 40, "passed": True, "blocking": []},
    ]}
    section = orch._review_evidence_section(
        task, head_sha="a" * 40, evidence=evidence)
    assert "**PASSED** — 1 round |" in section
    assert "2 rounds" not in section


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
    assert body.count("**PASSED** — 2 rounds") == 1
    # the folded log's one-line summary carries the count; the log's own
    # first line repeats it (the receipts COMMENT ships that line uncollapsed)
    assert body.count("commands recorded") == 2
    assert body.count("| Tests | ✅ PASS") == 1


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


def test_the_merge_policy_row_renders_from_a_real_policyverdict(store, tmp_path):
    """DEFECT 2's fix: `_PIN_PATTERNS` (and the pin-backing predicate above)
    must actually exercise a REAL `PolicyVerdict.as_dict()` — not just the
    review/tests/CI rows — so a vacuous pin-coverage gap (a merge-policy row
    nobody ever renders in this suite) cannot hide behind the other patterns.
    A real 3-rule, all-passing verdict must render the exact row and fold
    `_merge_policy_evidence_section` promises.
    """
    from no_human.core.merge_policy import PolicyVerdict, RuleVerdict

    verdict = PolicyVerdict(
        ready=True,
        rules=(
            RuleVerdict(name="review_passed", passed=True, detail="PASSED — 2 rounds"),
            RuleVerdict(name="tests_ran_and_passed", passed=True, detail="5 passed, 0 failed"),
            RuleVerdict(name="tamper_guard_clear", passed=True, detail="guard never fired"),
        ),
        source="default",
    )
    assert verdict.summary == "ready — 3 of 3 rules satisfied"

    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    receipts = _receipts()
    head_sha = _Commit.sha
    evidence = orch._gather_evidence(
        task, test_evidence=test_evidence, receipts=receipts,
        head_sha=head_sha, merge_policy=verdict.as_dict())
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=receipts,
                         merge_policy=verdict.as_dict())

    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert "| Merge policy | ✅ ready — 3 of 3 rules satisfied |" in ev
    assert "<details><summary>Merge-ready policy (3 rules, source: default)</summary>" in ev
    assert "review_passed" in ev and "tamper_guard_clear" in ev

    # ...and the pin predicate this module holds actually sees it: the row's
    # pin must be a value of `evidence.truth_pins()`.
    pins_rendered = _pins_in_body(body)
    assert "ready — 3 of 3 rules satisfied" in pins_rendered
    assert "ready — 3 of 3 rules satisfied" in set(evidence.truth_pins().values())


def test_merge_policy_section_discloses_a_failed_compute():
    """`_merge_policy_evidence_section` is a pure function of `PrEvidence` —
    exercised directly here (unit-level, no orchestrator run) against its
    three distinguishable states:

    1. ``merge_policy=None, merge_policy_error="RuntimeError"`` — the compute
       was attempted and raised: a NOT-COMPUTED row naming the exception
       class, and neither a fabricated ✅/❌ verdict nor a fold with nothing
       to show.
    2. ``merge_policy=None, merge_policy_error=None`` — no policy was ever
       attempted for this head (the ordinary case for most of this repo's
       history, and for any head with no `.no_human/merge_policy.yaml`
       compute yet run): renders "", exactly as before this field existed.
    3. ``merge_policy={...ready...}, merge_policy_error="RuntimeError"`` — a
       stamped verdict always wins over a stale error string (this shape
       cannot arise from `_finalize`'s own `replace(...)` calls, but the
       renderer's OWN precedence must not depend on that never happening).
    """
    failed = PrEvidence(merge_policy_error="RuntimeError")
    section = Orchestrator._merge_policy_evidence_section(failed)
    assert "| Merge policy |" in section
    assert "NOT COMPUTED — the merge-ready verdict could not be computed" in section
    assert "RuntimeError" in section
    assert "✅" not in section
    assert "❌" not in section

    empty = PrEvidence()
    assert Orchestrator._merge_policy_evidence_section(empty) == ""

    from no_human.core.merge_policy import PolicyVerdict, RuleVerdict

    verdict = PolicyVerdict(
        ready=True,
        rules=(RuleVerdict(name="review_passed", passed=True, detail="PASSED — 2 rounds"),),
        source="default",
    )
    stamped_and_stale_error = PrEvidence(
        merge_policy=verdict.as_dict(), merge_policy_error="RuntimeError")
    section2 = Orchestrator._merge_policy_evidence_section(stamped_and_stale_error)
    assert "| Merge policy | ✅ ready — 1 of 1 rules satisfied |" in section2
    assert "NOT COMPUTED" not in section2
    assert "RuntimeError" not in section2


def test_merge_policy_error_is_inline_cell_safe():
    """A `merge_policy_error` string is an exception CLASS NAME, so in
    practice it can never carry a newline — but the renderer routes it
    through `_inline_cell` anyway rather than trusting that invariant, the
    same discipline every other model-influenced cell in the PR body follows
    (see `test_a_model_authored_verifier_comment_cannot_inject_a_heading`
    for the sibling case on a verifier's `evidence` string). This proves the
    discipline is actually wired for THIS field, not merely assumed safe."""
    evidence = PrEvidence(merge_policy_error="Boom\n# MERGED AND APPROVED BY NO_HUMAN")
    section = Orchestrator._merge_policy_evidence_section(evidence)
    assert "\n# MERGED AND APPROVED BY NO_HUMAN" not in section
    assert "# MERGED AND APPROVED BY NO_HUMAN" in section  # still reaches the row, inertly


def test_evidence_renders_as_a_table_with_one_row_per_gate(store, tmp_path):
    """The operator's directive (2026-08-21): a reviewer meets the gate results
    FIRST, as one table of mechanical facts, and the detail behind each row is
    delivered folded — never dropped."""
    orch = _orch(store, tmp_path)
    task = _task()
    test_evidence = {"ran": True, "ok": True, "passed": 5, "failed": 0,
                     "errors": 0}
    body = orch._pr_body(task, _Commit(), _Result(),
                         test_evidence=test_evidence, receipts=_receipts())
    ev = body.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
    assert ev.startswith("| Check | Result |\n|---|---|\n")
    assert "| Independent review | ✅ **PASSED** — 2 rounds |" in ev
    assert "| Tests | ✅ PASS — 5 passed, 0 failed, 0 errors |" in ev
    assert "Decisive first" not in body
    assert "### Independent review" not in body
    assert "### Test evidence" not in body
    assert "<details><summary>1 finding raised and addressed" in ev
    assert "the backoff delay is not tested" in ev


# ═══ 2026-08-21: the body is SHORT by construction, pinned on a real PR ═════ #

_FIXTURE_574 = Path(__file__).resolve().parent.parent / "testdata" / "pr_body_574.json"


def _visible_words(body: str) -> int:
    return len(re.sub(r"<details>.*?</details>", "", body, flags=re.S).split())


def test_template_overhead_is_under_900_visible_chars(store, tmp_path):
    """With NOTHING to say, the template's own prose is bounded — a body's
    length must come from the task, not from boilerplate."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.acceptance_criteria = []
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence=None, receipts=[])
    assert visible_chars(body) < 900, (visible_chars(body), body)


def test_a_real_pr_renders_under_300_visible_words_beyond_its_criteria(
        store, tmp_path):
    """no_human-private #574 shipped 2,791 words (771 before the first fold).
    The same inputs — its title, criteria, intake Q&A, review history, test
    run, ten receipts and the coder's report — must now render under 300
    visible words on top of the criteria text, which is the task's own."""
    fx = json.loads(_FIXTURE_574.read_text())
    orch = _orch(store, tmp_path)
    t = Task.new(fx["title"], repo_path="/r")
    t.acceptance_criteria = fx["acceptance_criteria"]
    t.context = fx["context"]
    r = _Result()
    r.final_text = fx["final_text"]
    body = orch._pr_body(t, _Commit(), r, test_evidence=fx["test_evidence"],
                         receipts=fx["receipts"])
    crit_words = sum(len(c.split()) for c in t.acceptance_criteria)
    extra = _visible_words(body) - crit_words
    assert extra < 300, (extra, re.sub(r"<details>.*?</details>", "", body, flags=re.S))
    # ...and nothing was dropped to get there: every receipt command and the
    # report's last paragraph are still in the body, behind a fold.
    for rec in fx["receipts"]:
        assert rec["command"].split(" ")[0] in body
    assert "| Independent review | ✅ **PASSED** — 1 round |" in body
    assert "| Tests | ✅ PASS — 9380 passed, 0 failed, 0 errors |" in body
