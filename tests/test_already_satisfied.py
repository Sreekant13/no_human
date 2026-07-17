"""The already-satisfied zero-diff terminal (v6 taxonomy: 'answer in hand'
AMBIGUITY parks): claim parsing is strict because a valid claim diverts the
anti-fabrication default — anything less falls back to the zero-diff failure.

Tightened per PR #101 review (MEDIUM): standalone marker line, exact verdict
grammar per criterion, nonempty evidence, and coverage of every acceptance
criterion."""

from no_human.core.orchestrator import _parse_already_satisfied

CLAIM = (
    "Verified each criterion against the existing code.\n"
    "ALREADY-SATISFIED\n"
    "CRITERION: fetch retries on 5xx — MET — evidence: src/fetcher.py:42\n"
    "CRITERION: retry is capped at 3 — MET — evidence: src/fetcher.py:47\n"
)


def test_parse_accepts_a_fully_cited_claim():
    assert _parse_already_satisfied(CLAIM, 2) == CLAIM.strip()


def test_parse_rejects_missing_marker():
    text = CLAIM.replace("ALREADY-SATISFIED", "already done, trust me")
    assert _parse_already_satisfied(text, 2) is None


def test_parse_requires_the_marker_on_its_own_line():
    """'this is NOT an ALREADY-SATISFIED case' is a negation, not a claim."""
    text = ("This is NOT an ALREADY-SATISFIED case.\n"
            "CRITERION: fetch retries on 5xx — MET — evidence: src/fetcher.py:42\n"
            "CRITERION: retry is capped at 3 — MET — evidence: src/fetcher.py:47\n")
    assert _parse_already_satisfied(text, 2) is None


def test_parse_rejects_marker_without_criterion_lines():
    assert _parse_already_satisfied("ALREADY-SATISFIED\nall good.", 1) is None


def test_parse_rejects_any_not_met_variant():
    for verdict in ("NOT-MET", "NOT MET", "UNMET"):
        text = CLAIM + f"CRITERION: logs each retry — {verdict} — evidence: (none)\n"
        assert _parse_already_satisfied(text, 3) is None, verdict


def test_parse_requires_the_exact_verdict_slot_not_a_substring():
    """'METRICS endpoint exists' contains 'MET' but carries no verdict."""
    text = ("ALREADY-SATISFIED\n"
            "CRITERION: METRICS endpoint exists and is documented\n")
    assert _parse_already_satisfied(text, 1) is None


def test_parse_rejects_empty_evidence():
    text = ("ALREADY-SATISFIED\n"
            "CRITERION: fetch retries on 5xx — MET — evidence:\n")
    assert _parse_already_satisfied(text, 1) is None


def test_parse_requires_coverage_of_every_acceptance_criterion():
    """A claim listing 1 of N criteria is not 'EVERY criterion is MET'."""
    one_line = ("ALREADY-SATISFIED\n"
                "CRITERION: fetch retries on 5xx — MET — evidence: src/fetcher.py:42\n")
    assert _parse_already_satisfied(one_line, 2) is None
    assert _parse_already_satisfied(one_line, 1) == one_line.strip()
    assert _parse_already_satisfied(one_line, 0) == one_line.strip()


def test_parse_rejects_empty_and_none():
    assert _parse_already_satisfied("", 1) is None
    assert _parse_already_satisfied(None, 0) is None
