"""P4 (safe slice): the PR body surfaces an Assumptions & Open Questions
section so the reviewing human catches what the agent assumed."""

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def test_section_empty_when_nothing_to_flag(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("clean task", repo_path="/r")
    assert orch._assumptions_section(t) == ""


def test_section_lists_assumptions_and_enrichment(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("ambiguous task", repo_path="/r")
    t.context = {
        "assumptions": ["header is X-Instance-Id", "retry 3 times"],
        "original_criteria": ["works"],
    }
    section = orch._assumptions_section(t)
    assert "Assumptions & Open Questions" in section
    assert "header is X-Instance-Id" in section
    assert "retry 3 times" in section
    assert "auto-sharpened" in section
    assert "works" in section


def test_section_includes_blocker_diagnosis(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("stuck task", repo_path="/r")
    t.blocker = {"root_cause_hypothesis": "stuck at 60%", "question": "revise?"}
    section = orch._assumptions_section(t)
    assert "Unresolved: stuck at 60%" in section
    assert "Open question: revise?" in section


class _Commit:
    files_changed = 2
    insertions = 10
    deletions = 1


class _Result:
    final_text = "did the thing"
    num_turns = 5


def test_pr_body_embeds_section(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.acceptance_criteria = ["c1"]
    t.context = {"assumptions": ["assume A"]}
    body = orch._pr_body(t, _Commit(), _Result())
    assert "## ⚠️ Assumptions & Open Questions" in body
    assert "assume A" in body
    # Section sits between criteria and implementation summary.
    assert body.index("Acceptance criteria") < body.index("Assumptions & Open")
    assert body.index("Assumptions & Open") < body.index("Implementation summary")


def test_pr_body_clean_when_no_assumptions(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _Result())
    assert "Assumptions & Open Questions" not in body


def test_pr_body_no_evidence_section_by_default(store, tmp_path):
    """Default path (no test_evidence) is unchanged: no Test evidence section."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _Result())
    assert "Test evidence" not in body


def test_pr_body_surfaces_layered_evidence(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    evidence = {
        "ran": True, "ok": True,
        "layers": [
            "unit: PASS — 12 passed",
            "integration: PASS — 4 passed",
            "e2e: deferred (wake-gated)",
        ],
    }
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence=evidence)
    # The test run is now a `### Test evidence` sub-section under the one
    # `## Evidence` umbrella, and the Stats section is gone.
    assert "## Evidence" in body
    assert "### Test evidence" in body
    assert "## Stats" not in body
    assert "integration: PASS — 4 passed" in body
    assert "e2e: deferred (wake-gated)" in body
    # The test evidence sits inside the Evidence umbrella, before the mechanical
    # "How I verified this" receipts section.
    assert body.index("## Evidence") < body.index("Test evidence")
    assert body.index("Test evidence") < body.index("## How I verified this")


def test_pr_body_surfaces_single_run_aggregate(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    evidence = {"ran": True, "ok": False, "passed": 8, "failed": 2, "errors": 1}
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence=evidence)
    assert "## Test evidence" in body
    assert "tests: FAIL" in body
    assert "8 passed, 2 failed, 1 errors" in body


def test_pr_body_no_evidence_when_tests_did_not_run(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence={"ran": False})
    assert "Test evidence" not in body


def test_section_includes_intake_qa(store, tmp_path):
    """§6 grill: the self-answered Q&A is audited at the human gate."""
    orch = _orch(store, tmp_path)
    t = Task.new("grilled task", repo_path="/r")
    t.context = {"intake_qa": [
        {"question": "Which file?", "decision_it_changes": "target",
         "answer": "src/x.py:1", "source": "repo-evidence", "carve_out": "none"},
        {"question": "Rotate credential?", "decision_it_changes": "auth",
         "answer": "HUMAN-GATED: not self-answerable", "source": "",
         "carve_out": "access"},
    ]}
    section = orch._assumptions_section(t)
    assert "Which file?" in section
    assert "src/x.py:1" in section
    assert "repo-evidence" in section
    assert "Rotate credential?" in section
    assert "HUMAN-GATED" in section
