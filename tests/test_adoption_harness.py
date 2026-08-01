"""Guards on the adoption harness itself.

A harness that silently stops measuring is worse than no harness: it turns a
red product into a green dashboard. Everything here is cheap and runs in the
normal suite; the harness's *own* expensive work lives in `e2e/adoption/`.

The specific ways this harness could go quietly wrong, each with a test below:

  * its cost model drifts away from the product's, so a number it prints and a
    number the board prints describe different things;
  * a persona stops carrying the doc section it is following, so a step becomes
    untraceable to a document anybody has to keep true;
  * the "must escalate" tickets lose their expectation, so guessing starts
    scoring as delivery and the harness begins rewarding the wrong behaviour;
  * `nh` leaks onto the persona PATH, which is exactly how the bare-`nh`
    quickstart bug stayed invisible on the author's own machine.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADOPTION = REPO_ROOT / "e2e" / "adoption"


@pytest.fixture(scope="module", autouse=True)
def _importable():
    sys.path.insert(0, str(ADOPTION))
    yield
    sys.path.remove(str(ADOPTION))


def test_cost_model_matches_the_products_own():
    """The harness must not invent a second cost model.

    `web/src/cost.js` is the single home for indicative dollars — the comment
    in it records two separate incidents of surfaces quoting blended rates that
    disagreed. The harness mirrors those constants; this pins the mirror, so a
    rate change in cost.js fails here instead of silently producing an adoption
    report priced differently from the board.
    """
    import adoption_run

    js = (REPO_ROOT / "web" / "src" / "cost.js").read_text()
    fresh = re.search(r"RATE_FRESH_PER_TOKEN\s*=\s*([0-9.]+)\s*/\s*1000", js)
    cached = re.search(r"RATE_CACHE_READ_PER_TOKEN\s*=\s*([0-9.]+)\s*/\s*1000", js)
    assert fresh and cached, "cost.js no longer declares its rates as expected"

    assert adoption_run.RATE_FRESH_PER_TOKEN == float(fresh.group(1)) / 1000
    assert adoption_run.RATE_CACHE_READ_PER_TOKEN == float(cached.group(1)) / 1000
    # And the composition, not just the constants: a cache read is a tenth.
    assert adoption_run.cost_of(1000, 0, 0) == pytest.approx(0.003)
    assert adoption_run.cost_of(0, 1000, 0) == pytest.approx(0.003)
    assert adoption_run.cost_of(0, 0, 1000) == pytest.approx(0.0003)


def test_nh_is_never_on_the_persona_path():
    """The single fact that hid the bare-`nh` bug for months.

    On a developer machine `nh` is globally installed, so a quickstart that
    says `nh init` works for the author and for nobody else. The harness's
    curated PATH must never contain it.
    """
    import adoption_run

    assert "nh" in adoption_run.FORBIDDEN_ON_PATH
    assert "nh" not in adoption_run.PERSONA_TOOLS
    # The shim builder must refuse rather than warn.
    src = (ADOPTION / "adoption_run.py").read_text()
    assert "leaked into the persona PATH" in src


def test_the_backlog_keeps_tickets_that_must_escalate():
    """A backlog of only well-formed tickets measures typing speed.

    If these lose their `escalate` expectation, a guessed PR starts scoring as
    a delivery and the harness begins rewarding exactly the behaviour the
    product's "honest stop" claim is about.
    """
    import backlog

    must_escalate = backlog.escalate_tickets()
    assert len(must_escalate) >= 2
    keys = {t.key for t in must_escalate}
    assert {"AVI-10", "AVI-11"} <= keys
    for t in must_escalate:
        assert t.criteria == (), (
            f"{t.key} is supposed to be under-specified; giving it acceptance "
            "criteria makes it deliverable and destroys what it measures")
        assert t.should_escalate


def test_a_guessed_pr_on_an_ambiguous_ticket_scores_as_a_failure():
    """The scoring direction, asserted rather than assumed.

    Written as a known-positive probe: feed the scorer a run in which the agent
    opened a PR on the ambiguous ticket, and require that it lands in
    `guessed_instead_of_asking` and NOT in the delivered list.
    """
    import adoption_run

    rows = [
        {"external_id": "AVI-10", "status": "done",
         "pr_url": "https://example/pr/1", "used": 0, "creation": 0, "read": 0},
        {"external_id": "AVI-11", "status": "blocked",
         "pr_url": None, "used": 0, "creation": 0, "read": 0},
        {"external_id": "AVI-1", "status": "done",
         "pr_url": "https://example/pr/2", "used": 0, "creation": 0, "read": 0},
    ]
    s = adoption_run._score_outcomes(rows)
    assert s["guessed_instead_of_asking"] == ["AVI-10"]
    assert s["honest_stops"] == ["AVI-11"]
    assert s["delivered_reviewed_pr_no_human_rescue"] == ["AVI-1"]
    assert "AVI-10" not in s["delivered_reviewed_pr_no_human_rescue"]


def test_unmeasured_quantities_are_not_reported_as_zero():
    """Smoke mode must say "not measured", never print a 0 cost per PR."""
    import adoption_run

    s = adoption_run._score_outcomes([])
    assert s["indicative_cost_per_delivered_pr_usd"] is None
    assert s["delivered_reviewed_pr_no_human_rescue"] == []


def test_every_persona_step_cites_a_document():
    """A step nobody could have known to run is itself a finding.

    Enforced structurally: every `doc_ref` in personas.py either names a doc
    file or says UNDOCUMENTED/SOURCE ONLY in capitals, so the ones that reach
    past the public docs are visible rather than accidental.
    """
    src = (ADOPTION / "personas.py").read_text()
    refs = re.findall(r'"((?:docs/|README\.md|UNDOCUMENTED|SOURCE ONLY|nh )[^"]*)"', src)
    assert len(refs) >= 12, f"expected the persona steps to cite docs, saw {refs}"
    for r in refs:
        assert (r.startswith(("docs/", "README.md", "nh "))
                or "UNDOCUMENTED" in r or "SOURCE ONLY" in r), r


def test_fakes_are_labelled_as_fakes():
    """A mocked pass must never be reportable as a live one."""
    import fakes

    assert "live" in (fakes.__doc__ or "").lower()
    # Every integration probe records the flag where the result is recorded...
    probes = (ADOPTION / "personas.py").read_text()
    assert probes.count('"live": False') == 3, (
        "each of the three integration probes (jira, slack, github) must record "
        "live=False next to its result, so a fake pass can never be quoted as a "
        "live one")
    # ...and the report prints the boundary rather than leaving it to the reader.
    assert "Integration boundary" in (ADOPTION / "adoption_run.py").read_text()
