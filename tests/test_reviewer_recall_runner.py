"""Tests for the SCRUM-29 reviewer-recall runner (eval/reviewer_recall/runner.py).

The runner lives outside src/no_human (single surface: eval/ CLI python
only — docs/REVIEWER_RECALL_METHOD.md), so it is loaded here by file path,
the same way the CLI wiring (`nh bench report --reviewer-recall`) loads it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "eval" / "reviewer_recall" / "runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "test_reviewer_recall_runner_module", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rr = _load_runner()


def _truth(**overrides):
    base = {
        "class": "logic", "file": "app.py", "hunk_lines": [10, 20],
        "description": "off-by-one", "keywords": ["off-by-one", "boundary"],
        "planted_by": "supervising-session", "date": "2026-07-25",
    }
    base.update(overrides)
    return base


def _case(**truth_overrides) -> "rr.CaseSpec":
    return rr.CaseSpec(
        case_id="synthetic-case", dir=Path("/dev/null"), base_ref="deadbeef",
        diff_text="diff --git a/app.py b/app.py\n", truth=_truth(**truth_overrides),
    )


def _control_case() -> "rr.CaseSpec":
    return rr.CaseSpec(
        case_id="synthetic-control", dir=Path("/dev/null"), base_ref="deadbeef",
        diff_text="diff --git a/app.py b/app.py\n",
        truth={"class": "control", "file": None, "hunk_lines": None,
               "description": "clean", "keywords": [],
               "planted_by": "supervising-session", "date": "2026-07-25"},
    )


# --- score_case: the six required stubbed-reviewer scenarios ---------------

def test_caught_case():
    case = _case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=15,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is True
    assert result.clean_pass is None


def test_missed_wrong_file():
    case = _case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="other.py", line=15,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "planted file" in result.reason


def test_missed_right_file_wrong_lines():
    case = _case()  # hunk_lines [10, 20] -> in-range is [7, 23]
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=99,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "hunk_lines" in result.reason


def test_keyword_miss():
    case = _case()  # keywords: off-by-one, boundary
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=15,
                  text="this code could use a docstring", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "keyword" in result.reason


def test_control_clean_pass():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="PASS", findings=[])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is True
    assert result.caught is None


def test_control_false_alarm():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=1, text="looks suspicious", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is False
    assert "false alarm" in result.reason


# --- additional scoring edges -----------------------------------------------

def test_control_fail_only_non_blocking_is_clean_pass():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=1, text="nit: naming", blocking=False),
    ])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is True


def test_pass_verdict_is_missed_even_with_stray_finding_text():
    case = _case()
    outcome = rr.ReviewOutcome(status="PASS", findings=[])
    result = rr.score_case(case, outcome)
    assert result.caught is False


# --- render_report: denominators, never a bare percentage -------------------

def test_render_report_format():
    seeded_hit = rr.CaseResult(case_id="a", cls="logic", is_control=False,
                               outcome=rr.ReviewOutcome(status="FAIL"), caught=True)
    seeded_miss = rr.CaseResult(case_id="b", cls="security", is_control=False,
                                outcome=rr.ReviewOutcome(status="PASS"), caught=False)
    control_ok = rr.CaseResult(case_id="c", cls="control", is_control=True,
                               outcome=rr.ReviewOutcome(status="PASS"), clean_pass=True)
    report = rr.RecallReport(
        results=[seeded_hit, seeded_miss, control_ok],
        model="claude-opus-4-8", run_date="2026-07-25",
    )
    text = rr.render_report(report)
    assert "reviewer recall: 1/2" in text
    assert "logic 1/1" in text and "security 0/1" in text
    assert "specificity:     1/1 clean diffs passed" in text
    assert "model: claude-opus-4-8" in text
    assert "run date: 2026-07-25" in text
    assert "docs/REVIEWER_RECALL_METHOD.md" in text
    # never a bare percentage: the "(NN%)" always follows an "x/y" denominator
    assert "reviewer recall: 1/2 (50%)" in text


# --- corpus + checkout/apply wiring (real corpus, no LLM calls) -------------

def test_load_cases_real_corpus():
    cases = rr.load_cases()
    assert len(cases) >= 9
    ids = {c.case_id for c in cases}
    assert "logic-stale-renders-fresh" in ids
    assert "control-bench-baseline" in ids
    classes = {c.truth["class"] for c in cases}
    assert classes == {"logic", "security", "spec-miss", "test-tamper", "control"}


def test_prepare_case_repo_has_no_descendant_history(tmp_path):
    case = next(c for c in rr.load_cases() if c.case_id == "logic-stale-renders-fresh")
    case_repo = rr.prepare_case_repo(REPO_ROOT, case, tmp_path)
    log = subprocess.run(["git", "rev-list", "--all"], cwd=case_repo,
                         check=True, capture_output=True, text=True)
    commit_shas = log.stdout.split()
    assert len(commit_shas) == 1, "checkout must carry no ancestor/descendant history"
    # the diff actually applied
    diff_status = subprocess.run(["git", "status", "--porcelain"], cwd=case_repo,
                                 check=True, capture_output=True, text=True)
    assert diff_status.stdout.strip() != ""


@pytest.mark.asyncio
async def test_run_case_invokes_injected_reviewer_with_checked_out_repo(tmp_path):
    case = next(c for c in rr.load_cases() if c.case_id == "logic-stale-renders-fresh")
    seen = {}

    async def stub_reviewer(repo_path, diff_text, case_spec):
        seen["repo_path"] = repo_path
        seen["diff_text"] = diff_text
        seen["case_id"] = case_spec.case_id
        return rr.ReviewOutcome(status="FAIL", findings=[
            rr.Finding(file=case_spec.truth["file"],
                      line=case_spec.truth["hunk_lines"][0] + 1,
                      text="fresh renders stale duplicate divider", blocking=True),
        ])

    result = await rr.run_case(REPO_ROOT, case, stub_reviewer, tmp_path)
    assert seen["case_id"] == "logic-stale-renders-fresh"
    assert seen["diff_text"] == case.diff_text
    assert (Path(seen["repo_path"]) / case.truth["file"]).exists()
    assert result.caught is True


@pytest.mark.asyncio
async def test_run_all_iterates_every_case_with_injected_reviewer():
    call_count = {"n": 0}

    async def stub_reviewer(repo_path, diff_text, case_spec):
        call_count["n"] += 1
        if case_spec.is_control:
            return rr.ReviewOutcome(status="PASS", findings=[])
        return rr.ReviewOutcome(status="PASS", findings=[])  # every seeded case missed

    report = await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer, model="stub-model")
    all_cases = rr.load_cases()
    assert call_count["n"] == len(all_cases)
    assert len(report.results) == len(all_cases)
    assert report.model == "stub-model"


# --- SCRUM-47: ERROR path never scores as a miss ----------------------------

@pytest.mark.asyncio
async def test_setup_failure_marks_status_error(tmp_path):
    bad_case = rr.CaseSpec(
        case_id="broken-checkout", dir=Path("/dev/null"),
        base_ref="not-a-real-ref-deadbeef", diff_text="",
        truth=_truth(),
    )

    async def stub_reviewer(repo_path, diff_text, case_spec):
        raise AssertionError("reviewer must never be invoked when setup fails")

    result = await rr.run_case(REPO_ROOT, bad_case, stub_reviewer, tmp_path)
    assert result.status == "ERROR"
    assert result.caught is None
    assert result.clean_pass is None
    assert "case setup failed" in result.reason


# --- SCRUM-47: headline refused when any case errored ------------------------

def test_render_report_refuses_on_error():
    error_result = rr.CaseResult(
        case_id="broken-checkout", cls="logic", is_control=False,
        outcome=rr.ReviewOutcome(status="ERROR"), status="ERROR",
        reason="case setup failed: fatal: bad revision",
    )
    report = rr.RecallReport(results=[error_result], model="claude-opus-4-8",
                             run_date="2026-07-25")
    with pytest.raises(rr.HeadlineRefusedError):
        rr.render_report(report)


def test_render_report_ok_when_no_errors():
    seeded_hit = rr.CaseResult(case_id="a", cls="logic", is_control=False,
                               outcome=rr.ReviewOutcome(status="FAIL"), caught=True)
    report = rr.RecallReport(results=[seeded_hit], model="claude-opus-4-8",
                             run_date="2026-07-25")
    text = rr.render_report(report)
    assert "reviewer recall: 1/1" in text


# --- SCRUM-47: transcripts written on every run_all invocation --------------

@pytest.mark.asyncio
async def test_run_all_writes_transcripts(tmp_path):
    async def stub_reviewer(repo_path, diff_text, case_spec):
        return rr.ReviewOutcome(status="PASS", findings=[])

    report = await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer,
                              model="stub-model", run_date="2026-07-25",
                              runs_dir=tmp_path)
    all_cases = rr.load_cases()
    out_dir = tmp_path / "2026-07-25"
    assert out_dir.is_dir()
    for case in all_cases:
        case_file = out_dir / f"{case.case_id}.json"
        assert case_file.exists(), f"missing transcript for {case.case_id}"
    sample = json.loads((out_dir / f"{all_cases[0].case_id}.json").read_text())
    assert {"case_name", "status", "score"} <= sample.keys()
    assert sample["case_name"] == all_cases[0].case_id
    assert sample["status"] == "OK"


# --- SCRUM-47: ERROR cases omit `caught` in transcripts (never a miss) ------

def test_error_transcript_omits_caught(tmp_path):
    error_result = rr.CaseResult(
        case_id="broken-checkout", cls="logic", is_control=False,
        outcome=rr.ReviewOutcome(status="ERROR"), status="ERROR",
        reason="case setup failed: fatal: bad revision",
    )
    report = rr.RecallReport(results=[error_result], model="claude-opus-4-8",
                             run_date="2026-07-25")
    rr.write_transcripts(report, tmp_path)
    data = json.loads((tmp_path / "2026-07-25" / "broken-checkout.json").read_text())
    assert data["status"] == "ERROR"
    assert "caught" not in data
    assert data["score"] is None
    assert "case setup failed" in data["error_message_if_error"]
