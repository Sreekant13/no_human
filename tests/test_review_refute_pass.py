"""Bounded refute pass on the review gate's FAIL path.

A blocking finding today survives to the verdict the moment its cited
file:line merely EXISTS (`_citation_fails_in_any`) — that proves the citation
is not hallucinated, never that the finding is CORRECT — and the gate's only
other independent-confirmation mechanism (the complex-tier angle passes) runs
only on a PASSING verdict. So a FAIL went straight back to the coder with no
second reader ever having tried to knock it down.

`_refute_candidates` / `_build_refute_prompt` / `_parse_refutations` /
`_apply_refutations` in `src/no_human/review/reviewer.py` close that gap with
ONE bounded, single-turn, read-only refute pass on the gate path, run only
after a FAIL is decided. This file proves, per the task's ten acceptance
criteria:

  AC1 — a blocking finding demotes to advisory when the refute pass cites
        counter-evidence at an EXISTING file:line (moves out of
        `blocking_items`, into `demoted_citations`).
  AC2 — it does NOT demote on an invalid counter-citation (nonexistent file,
        blank file, zero/missing line).
  AC3 — a goal veto (`goal.reachable is False`) is never demoted.
  AC4 — a `spec_compliance: false` stage is never demoted.
  AC5 — a critical-severity finding is never demoted, and is never even
        offered to the refute pass.
  AC6 — a refute pass that times out, errors, or reaches no verdict changes
        NOTHING: the FAIL stands byte-identical (checklist, severity,
        evidence, demoted_citations, decision.passed).
  AC7 — refute-pass tokens fold into `decision.tokens_used` /
        `cache_read_tokens` / `cache_creation_tokens` / `output_tokens` via
        the existing `_carry_usage` pattern (there is no `attempt_tokens`
        field on `ReviewDecision`).
  AC8 — no numeric scoring logic anywhere in the refute implementation:
        demote/no-demote is binary.
  AC9 — the refute pass only ever runs on the gate-path FAIL branch — never
        on PASS, never inside the complex-tier angle loop.
  AC10 — the deliberate-strictness deviation and the gate-path-only
        applicability are documented at the source (mirrored in the PR body
        alongside this file).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import subprocess
from pathlib import Path

import pytest

import no_human.review.reviewer as rv
from no_human.agent.claude_backend import AgentResult
from no_human.core.task import Task
from no_human.review.reviewer import (
    _REFUTE_TIMEOUT,
    AdversarialReviewer,
    ChecklistItem,
    ReviewDecision,
    _apply_refutations,
    _parse_review_output,
    _refute_candidates,
)

# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #

def _verdict_text(passed: bool, items: list[dict], *, goal=None, stages=None) -> str:
    data: dict = {"passed": passed, "items": items}
    if goal is not None:
        data["goal"] = goal
    if stages is not None:
        data["stages"] = stages
    return f"Some preamble.\n\nREVIEW_JSON_START\n{json.dumps(data)}\nREVIEW_JSON_END\n"


def _refute_text(refutations: list[dict]) -> str:
    return ("Some preamble.\n\nREFUTE_JSON_START "
            + json.dumps({"refutations": refutations})
            + " REFUTE_JSON_END\n")


def _agent_result(text: str, *, tokens_used: int = 100, cache_read: int = 0,
                   cache_creation: int = 0, output_tokens=None) -> AgentResult:
    return AgentResult(
        final_text=text, num_turns=1, is_error=False, tokens_used=tokens_used,
        session_id="fake", stop_reason="end_turn",
        cache_read_tokens=cache_read, cache_creation_tokens=cache_creation,
        output_tokens=output_tokens,
    )


async def _hang() -> None:
    await asyncio.sleep(999)


class ScriptedBackend:
    """Returns one scripted response per `run()` call, in order.

    A script entry is an ``AgentResult`` (normal return), a ``BaseException``
    instance (raised), or a zero-arg async callable (awaited — used to
    simulate a session that never returns, for the timeout sub-case).
    """

    model = "claude-opus-5"

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                   on_event=None, supervisor_hook=None):
        idx = len(self.calls)
        self.calls.append({"prompt": prompt, "max_turns": max_turns})
        item = self._script[idx]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item


@pytest.fixture
def simple_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    (repo / "calc.py").write_text("def add(a, b): return a + b\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    (repo / "calc.py").write_text("def add(a, b): return a + b\n\ndef mul(a, b): return a * b\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add mul"], check=True, capture_output=True)
    return repo


def _shrink_refute_timeout(monkeypatch, quick: float = 0.05) -> None:
    """Shrink ONLY the refute pass's `_REFUTE_TIMEOUT` (180s) window so a
    hung refute session times out fast in-test. Every other `wait_for` call
    (the main review, which completes instantly against a scripted backend
    regardless of its window) is untouched. Mirrors
    `tests/test_reviewer.py::test_angle_timeout_never_fails_the_gate`.
    """
    real_wait_for = rv.asyncio.wait_for

    async def quick_wait_for(coro, timeout=None):
        if timeout == _REFUTE_TIMEOUT:
            timeout = quick
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(rv.asyncio, "wait_for", quick_wait_for)


MAIN_FAIL_ITEMS = [{
    "label": "mul untested", "passed": False,
    "evidence": "no test covers the a=0 edge case", "severity": "high",
    "file": "calc.py", "line": 1,
}]
MAIN_FAIL_TEXT = _verdict_text(False, MAIN_FAIL_ITEMS)


def _assert_checklists_match(got: ReviewDecision, want: ReviewDecision) -> None:
    assert got.passed == want.passed
    assert len(got.checklist) == len(want.checklist)
    for g, w in zip(got.checklist, want.checklist):
        assert g.label == w.label
        assert g.passed == w.passed
        assert g.severity == w.severity
        assert g.evidence == w.evidence
        assert g.file == w.file
        assert g.line == w.line
    assert got.demoted_citations == want.demoted_citations


# --------------------------------------------------------------------------- #
# AC1 — valid counter-citation demotes                                        #
# --------------------------------------------------------------------------- #

def test_ac1_apply_refutations_demotes_on_existing_counter_citation(simple_repo):
    item = ChecklistItem("mul untested", False, "no edge case test",
                          file="calc.py", line=2, severity="high")
    decision = ReviewDecision(passed=False, checklist=[item])
    assert item in decision.blocking_items

    _apply_refutations(
        decision,
        [{"label": "mul untested", "file": "calc.py", "line": 1,
          "evidence": "calc.py:1 shows add() is unaffected"}],
        [item],
        [(simple_repo, "HEAD~1")],
    )

    assert item.severity == "low"
    assert item not in decision.blocking_items
    assert item in decision.advisory_items
    assert len(decision.demoted_citations) == 1
    assert "mul untested" in decision.demoted_citations[0]
    assert "calc.py:1" in decision.demoted_citations[0]


async def test_ac1_integration_full_review_demotes_and_flips_the_gate(simple_repo):
    """Regression guard for the attempt-1 review failure: the task is
    COMPLEX-tier and the refute pass demotes the ONLY blocking finding, which
    flips `decision.passed` False->True. If the angle block were gated on a
    live re-read of `decision.passed` (the attempt-1 bug) instead of the
    captured `gate_originally_passed`, it would fire here and the
    ScriptedBackend — scripted for exactly 2 calls — would raise IndexError
    on the 3rd. `len(backend.calls) == 2` below is the load-bearing assertion.
    """
    refute_response = _refute_text([
        {"label": "mul untested", "file": "calc.py", "line": 1,
         "evidence": "calc.py:1 shows the edge case is already exercised"},
    ])
    backend = ScriptedBackend([
        _agent_result(MAIN_FAIL_TEXT),
        _agent_result(refute_response),
    ])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("add mul()")
    t.context = {"complexity_tier": "complex"}
    t.acceptance_criteria = ["mul(a,b) returns product"]

    decision = await reviewer.review(t, repo_path=simple_repo)

    assert len(backend.calls) == 2, (
        "refute-induced FAIL->PASS flip must NOT open the complex-tier angle "
        f"block; expected exactly 2 backend calls, got {len(backend.calls)}")
    assert decision.passed is True
    assert decision.checklist[0].severity == "low"
    assert decision.checklist[0] not in decision.blocking_items
    assert len(decision.demoted_citations) == 1


# --------------------------------------------------------------------------- #
# AC2 — invalid counter-citation does not demote                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_refutation,why", [
    ({"label": "mul untested", "file": "nope.py", "line": 1,
      "evidence": "x"}, "nonexistent file"),
    ({"label": "mul untested", "file": "", "line": 1,
      "evidence": "x"}, "blank file"),
    ({"label": "mul untested", "file": "calc.py", "line": 0,
      "evidence": "x"}, "line == 0"),
    ({"label": "mul untested", "file": "calc.py",
      "evidence": "x"}, "missing line key"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_ac2_apply_refutations_does_not_demote_on_invalid_citation(
        simple_repo, bad_refutation, why):
    item = ChecklistItem("mul untested", False, "no edge case test",
                          file="calc.py", line=2, severity="high")
    decision = ReviewDecision(passed=False, checklist=[item])

    _apply_refutations(decision, [bad_refutation], [item],
                        [(simple_repo, "HEAD~1")])

    assert item.severity == "high", why
    assert item in decision.blocking_items, why
    assert decision.demoted_citations == [], why


async def test_ac2_integration_invalid_refutation_leaves_the_fail_standing(simple_repo):
    refute_response = _refute_text([
        {"label": "mul untested", "file": "does_not_exist.py", "line": 1,
         "evidence": "fabricated"},
    ])
    backend = ScriptedBackend([
        _agent_result(MAIN_FAIL_TEXT),
        _agent_result(refute_response),
    ])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("add mul()")
    t.acceptance_criteria = ["mul(a,b) returns product"]

    decision = await reviewer.review(t, repo_path=simple_repo)

    assert len(backend.calls) == 2
    assert decision.passed is False
    assert decision.checklist[0].severity == "high"
    assert decision.checklist[0] in decision.blocking_items
    assert decision.demoted_citations == []


# --------------------------------------------------------------------------- #
# AC3 — goal veto is never demoted                                            #
# --------------------------------------------------------------------------- #

def test_ac3_refute_candidates_skips_entirely_on_a_goal_veto():
    item = ChecklistItem("some other finding", False, "evidence",
                          file="calc.py", line=1, severity="high")
    decision = ReviewDecision(
        passed=False, checklist=[item],
        goal={"reachable": False, "entry_point": "calc.py:1"},
    )
    assert _refute_candidates(decision) == []


async def test_ac3_integration_goal_veto_never_demoted_refute_never_runs(simple_repo):
    goal = {"reachable": False, "entry_point": "calc.py:1"}
    main_text = _verdict_text(
        False,
        [{"label": "unrelated but passing", "passed": True, "evidence": "ok"}],
        goal=goal,
    )
    # Script has exactly ONE entry: if the call site ever invoked the refute
    # pass here, ScriptedBackend would raise IndexError on the 2nd call.
    backend = ScriptedBackend([_agent_result(main_text)])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("wire it up")

    decision = await reviewer.review(t, repo_path=simple_repo)

    assert len(backend.calls) == 1, "refute pass must never run on a goal veto"
    assert decision.passed is False
    assert decision.goal is not None
    assert decision.goal.get("reachable") is False
    assert decision.demoted_citations == []


# --------------------------------------------------------------------------- #
# AC4 — spec_compliance:false is never demoted                                #
# --------------------------------------------------------------------------- #

def test_ac4_refute_candidates_skips_entirely_on_spec_compliance_false():
    item = ChecklistItem("some other finding", False, "evidence",
                          file="calc.py", line=1, severity="high")
    decision = ReviewDecision(
        passed=False, checklist=[item],
        stages={"spec_compliance": {"passed": False, "reason": "AC #3 unmet"}},
    )
    assert _refute_candidates(decision) == []


async def test_ac4_integration_spec_compliance_false_never_demoted_refute_never_runs(simple_repo):
    stages = {"spec_compliance": {"passed": False, "reason": "AC #3 unmet"}}
    main_text = _verdict_text(
        False,
        [{"label": "unrelated but passing", "passed": True, "evidence": "ok"}],
        stages=stages,
    )
    backend = ScriptedBackend([_agent_result(main_text)])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("wire it up")

    decision = await reviewer.review(t, repo_path=simple_repo)

    assert len(backend.calls) == 1, "refute pass must never run on spec_compliance:false"
    assert decision.passed is False
    assert decision.stages == stages
    assert decision.demoted_citations == []


# --------------------------------------------------------------------------- #
# AC5 — critical severity is never demoted / never even offered               #
# --------------------------------------------------------------------------- #

def test_ac5_refute_candidates_excludes_critical_but_includes_high():
    critical = ChecklistItem("crit finding", False, "evidence",
                              file="calc.py", line=1, severity="critical")
    high = ChecklistItem("high finding", False, "evidence",
                          file="calc.py", line=1, severity="high")
    decision = ReviewDecision(passed=False, checklist=[critical, high])

    candidates = _refute_candidates(decision)

    assert critical not in candidates
    assert high in candidates


def test_ac5_apply_refutations_never_demotes_a_critical_item_even_if_named(simple_repo):
    critical = ChecklistItem("crit finding", False, "evidence",
                              file="calc.py", line=1, severity="critical")
    decision = ReviewDecision(passed=False, checklist=[critical])
    # Mirrors the real call site: critical was pre-filtered out of
    # `candidates`, so it's not in `by_label` even though the refutation
    # names its exact label.
    _apply_refutations(
        decision,
        [{"label": "crit finding", "file": "calc.py", "line": 1,
          "evidence": "x"}],
        [],  # candidates — critical never makes it in
        [(simple_repo, "HEAD~1")],
    )
    assert critical.severity == "critical"
    assert critical in decision.blocking_items
    assert decision.demoted_citations == []


async def test_ac5_integration_sole_critical_finding_skips_refute_entirely(simple_repo):
    main_text = _verdict_text(False, [{
        "label": "crit finding", "passed": False, "evidence": "very bad",
        "severity": "critical", "file": "calc.py", "line": 1,
    }])
    backend = ScriptedBackend([_agent_result(main_text)])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("wire it up")

    decision = await reviewer.review(t, repo_path=simple_repo)

    assert len(backend.calls) == 1, (
        "a FAIL whose only blocking finding is critical has nothing "
        "refutable — the pass must not run")
    assert decision.passed is False
    assert decision.checklist[0].severity == "critical"
    assert decision.checklist[0] in decision.blocking_items
    assert decision.demoted_citations == []


# --------------------------------------------------------------------------- #
# AC6 — timeout / error / no-verdict changes NOTHING                          #
# --------------------------------------------------------------------------- #

async def test_ac6_refute_timeout_leaves_decision_byte_identical(simple_repo, monkeypatch):
    _shrink_refute_timeout(monkeypatch)
    backend = ScriptedBackend([_agent_result(MAIN_FAIL_TEXT), _hang])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("add mul()")

    decision = await reviewer.review(t, repo_path=simple_repo)

    expected = _parse_review_output(MAIN_FAIL_TEXT, repo_path=simple_repo,
                                     before_ref="HEAD~1")
    assert len(backend.calls) == 2
    _assert_checklists_match(decision, expected)
    # A hard timeout never calls `_run_bounded`'s success path, so no
    # AgentResult ever reaches `_carry_usage` — tokens stay main-call-only.
    assert decision.tokens_used == 100


async def test_ac6_refute_error_leaves_decision_byte_identical(simple_repo):
    backend = ScriptedBackend([
        _agent_result(MAIN_FAIL_TEXT),
        RuntimeError("refute session exploded"),
    ])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("add mul()")

    decision = await reviewer.review(t, repo_path=simple_repo)

    expected = _parse_review_output(MAIN_FAIL_TEXT, repo_path=simple_repo,
                                     before_ref="HEAD~1")
    assert len(backend.calls) == 2
    _assert_checklists_match(decision, expected)
    assert decision.tokens_used == 100


async def test_ac6_refute_no_verdict_changes_nothing_but_still_bills(simple_repo):
    backend = ScriptedBackend([
        _agent_result(MAIN_FAIL_TEXT, tokens_used=100),
        _agent_result("I looked and have nothing further to say.", tokens_used=50),
    ])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("add mul()")

    decision = await reviewer.review(t, repo_path=simple_repo)

    expected = _parse_review_output(MAIN_FAIL_TEXT, repo_path=simple_repo,
                                     before_ref="HEAD~1")
    assert len(backend.calls) == 2
    _assert_checklists_match(decision, expected)
    # Unlike timeout/error, a real (if unparseable) result DID reach
    # `_run_bounded`'s success path, so `_carry_usage` folds its spend in —
    # "changes nothing" is about the VERDICT, never about billing.
    assert decision.tokens_used == 150


# --------------------------------------------------------------------------- #
# AC7 — refute-pass tokens fold into decision usage fields                    #
# --------------------------------------------------------------------------- #

async def test_ac7_refute_pass_tokens_fold_via_carry_usage(simple_repo):
    """`ReviewDecision` has no `attempt_tokens` field — verify the actual
    fields `_carry_usage` writes: `tokens_used`, `cache_read_tokens`,
    `cache_creation_tokens`, `output_tokens`."""
    backend = ScriptedBackend([
        _agent_result(MAIN_FAIL_TEXT, tokens_used=100, cache_read=10,
                      cache_creation=5, output_tokens=20),
        _agent_result(_refute_text([]), tokens_used=50, cache_read=3,
                      cache_creation=2, output_tokens=7),
    ])
    reviewer = AdversarialReviewer(backend=backend)
    t = Task.new("add mul()")

    decision = await reviewer.review(t, repo_path=simple_repo)

    assert len(backend.calls) == 2
    assert decision.passed is False  # empty refutations list — nothing demoted
    assert decision.tokens_used == 150
    assert decision.cache_read_tokens == 13
    assert decision.cache_creation_tokens == 7
    assert decision.output_tokens == 27


# --------------------------------------------------------------------------- #
# AC8 — no numeric scoring anywhere; demote/no-demote is binary               #
# --------------------------------------------------------------------------- #

_FORBIDDEN_SCORE_ROOTS = ("score", "weight", "confidence", "threshold", "rank", "rating")


def _identifiers(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            names.append(node.arg)
    return names


def test_ac8_no_numeric_scoring_identifiers_in_the_refute_implementation():
    """Scoped to IDENTIFIERS (variable/argument/keyword/function names), not
    string or docstring CONTENT — the prompt and the module comment
    legitimately tell the model 'do not ... score, weight, or rank' in
    prose, and that prohibition must not itself trip the guard it
    documents. A blind text-keyword grep over this block false-positives on
    exactly those three lines; this test intentionally does not do that."""
    offenders = []
    for fn in (rv._refute_candidates, rv._build_refute_prompt,
               rv._parse_refutations, rv._apply_refutations):
        tree = ast.parse(inspect.getsource(fn))
        for name in _identifiers(tree):
            low = name.lower()
            if any(root in low for root in _FORBIDDEN_SCORE_ROOTS):
                offenders.append(f"{fn.__name__}: {name!r}")
    assert offenders == [], f"numeric-scoring identifiers found: {offenders}"


def test_ac8_apply_refutations_demotion_is_a_fixed_constant():
    """Direct proof of 'binary only': the ONE assignment to `.severity` in
    `_apply_refutations` is the literal string 'low' — never a value derived
    from a score, weight, or rank."""
    tree = ast.parse(inspect.getsource(rv._apply_refutations))
    severity_assigns = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == "severity"
                for target in node.targets)
    ]
    assert len(severity_assigns) == 1, (
        f"expected exactly one `.severity =` assignment in _apply_refutations, "
        f"found {len(severity_assigns)}")
    value = severity_assigns[0]
    assert isinstance(value, ast.Constant) and value.value == "low", (
        f"`.severity` must be set to the fixed constant 'low', not a "
        f"computed value: got {ast.dump(value)}")


# --------------------------------------------------------------------------- #
# AC9 — refute runs on the gate-path FAIL branch only                         #
# --------------------------------------------------------------------------- #

def _reviewer_tree() -> ast.Module:
    return ast.parse(Path(rv.__file__).read_text(encoding="utf-8"))


def _find_method(tree: ast.AST, class_name: str, method_name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name == method_name):
                    return item
    return None


def _call_names(node: ast.AST) -> set[str]:
    return {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def test_ac9_refute_candidates_called_exactly_once_inside_review():
    tree = _reviewer_tree()
    review = _find_method(tree, "AdversarialReviewer", "review")
    assert review is not None
    calls = [
        n for n in ast.walk(review)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_refute_candidates"
    ]
    assert len(calls) == 1, (
        f"_refute_candidates must be called exactly once inside "
        f"AdversarialReviewer.review, found {len(calls)}")


def test_ac9_refute_pass_is_not_nested_inside_the_angle_block_and_runs_before_it():
    tree = _reviewer_tree()
    review = _find_method(tree, "AdversarialReviewer", "review")
    assert review is not None

    angle_if = None
    for node in ast.walk(review):
        if isinstance(node, ast.If):
            dumped = ast.dump(node.test)
            if "gate_originally_passed" in dumped and "_tier_wants_angles" in dumped:
                angle_if = node
                break
    assert angle_if is not None, (
        "could not locate the complex-tier angle `if` block in review()")

    inside_angle = set(ast.walk(angle_if))
    refute_call = next(
        n for n in ast.walk(review)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_refute_candidates")

    assert refute_call not in inside_angle, (
        "_refute_candidates is called INSIDE the complex-tier angle block — "
        "it must run on the gate path, before angles, never inside them")
    assert refute_call.lineno < angle_if.lineno, (
        "the refute pass must run BEFORE the angle block, not after")


def test_ac9_refute_functions_never_called_from_fast_review_or_agent_review():
    tree = _reviewer_tree()
    refute_names = {
        "_refute_candidates", "_build_refute_prompt",
        "_parse_refutations", "_apply_refutations",
    }
    for method_name in ("_fast_review", "_agent_review"):
        method = _find_method(tree, "AdversarialReviewer", method_name)
        assert method is not None, f"AdversarialReviewer.{method_name} not found"
        used = _call_names(method) & refute_names
        assert not used, (
            f"{method_name} must never call a refute-pass function, found {used}")


def test_ac9_refute_functions_never_called_from_an_early_return_mode_branch():
    tree = _reviewer_tree()
    review = _find_method(tree, "AdversarialReviewer", "review")
    assert review is not None
    refute_names = {
        "_refute_candidates", "_build_refute_prompt",
        "_parse_refutations", "_apply_refutations",
    }
    mode_markers = ("tamper_adjudication", "code_review", "already_satisfied")
    checked_any = False
    for node in ast.walk(review):
        if isinstance(node, ast.If):
            dumped = ast.dump(node.test)
            if "mode" in dumped and any(marker in dumped for marker in mode_markers):
                checked_any = True
                used = _call_names(node) & refute_names
                assert not used, (
                    "an early-return mode branch references a refute-pass "
                    f"function: {used}")
    assert checked_any, (
        "could not find any of the tamper_adjudication/code_review/"
        "already_satisfied mode branches to check")


# --------------------------------------------------------------------------- #
# AC10 — documentation                                                        #
# --------------------------------------------------------------------------- #

def test_ac10_refute_candidates_documents_the_deliberate_strictness_deviation():
    doc = rv._refute_candidates.__doc__ or ""
    assert "DELIBERATELY STRICTER" in doc
    assert "skips the pass ENTIRELY" in doc


def test_ac10_module_comment_documents_gate_path_only_and_the_mirror_contract():
    src = Path(rv.__file__).read_text(encoding="utf-8")
    assert "GATE-PATH ONLY" in src
    assert "mirrors, deliberately, the angle-skip contract" in src
