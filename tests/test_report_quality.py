"""C3 — report-deliverable adequacy guard.

Verifies the guard REJECTS unambiguously-inadequate report deliverables
(empty / placeholder non-answers / an empty design doc) while a genuinely
terse-but-real finding PASSES (high-precision — a false reject just costs a
retry, but must not reject valid work).
"""

import pytest

from no_human.core.report_quality import report_inadequacy


# --- SHOULD be rejected (inadequate) ----------------------------------------
@pytest.mark.parametrize("text", [
    "",
    "   \n  ",
    "Done.",
    "done",
    "**Done.**",
    "- done",
    "Complete",
    "Investigation complete",
    "N/A",
    "n/a",
    "None",
    "nothing to report",
    "No changes needed",
    "PR opened",
    "see above",
    "OK",
])
def test_inadequate_reports_are_flagged(text):
    assert report_inadequacy(text, "investigation") is not None


def test_empty_design_doc_flagged():
    assert report_inadequacy("", "design_doc") is not None


def test_short_design_doc_flagged():
    # A design DOCUMENT can't be a sentence; the length floor catches it.
    short = "Use a queue for the export. Done."
    assert len(short) < 200
    assert report_inadequacy(short, "design_doc") is not None


# --- SHOULD pass (real deliverable, even if terse) --------------------------
@pytest.mark.parametrize("text", [
    # a terse-but-real investigation finding — must NOT be rejected
    "Root cause: a missing null-check at foo.py:42; guard it with `if x is None: return`.",
    "The flake is a race in the teardown: the worker is killed before the socket "
    "closes. Fix by awaiting close() before terminate().",
    # a valid 'no changes needed' conclusion WITH reasoning passes (only the bare
    # phrase is a placeholder)
    "No changes needed: the endpoint already validates the token at auth.py:88, "
    "so the reported gap does not exist on main.",
])
def test_terse_but_real_findings_pass(text):
    assert report_inadequacy(text, "investigation") is None


def test_real_design_doc_passes():
    doc = (
        "# Export pipeline design\n\n"
        "## Problem\nThe current synchronous export blocks the request thread for "
        "large tenants, causing timeouts.\n\n"
        "## Approach\nMove export to a background worker fed by a durable queue; "
        "the request enqueues a job and returns a job id the client polls.\n\n"
        "## Trade-offs\nAdds eventual-consistency for the download link but removes "
        "the timeout class entirely.\n"
    )
    assert len(doc) >= 200
    assert report_inadequacy(doc, "design_doc") is None


def test_non_report_kind_and_reasoned_conclusions_pass():
    # A substantial investigation is fine regardless of the leading word.
    assert report_inadequacy(
        "Done — verified the fix: the retry now backs off (see run 7008).",
        "investigation") is None
