"""The curated-corpus guard — needs eval/northstar_tasks/, so private only.

Split out of tests/test_bench_task.py on 2026-07-30. The 54 curated specs are
verbatim operator work-conversation content and EXPORT_CLASSIFICATION.txt drops the
whole directory, so in the public export this test found an empty directory and
failed with `curated core suite shrank: 0` — the no-shrink guard firing on the
export rather than on tampering. Everything else in test_bench_task.py is
env-independent and still runs publicly.

This file is classified `drop` in EXPORT_CLASSIFICATION.txt.
"""

from __future__ import annotations

from no_human.eval.bench_task import load_bench_tasks


def test_committed_core_specs_are_valid_and_honest():
    """The hand-curated core specs (eval/northstar_tasks/*.yaml — the reviewable,
    git-tracked bench suite) must all parse and carry honest metadata: a
    malformed or dishonestly-flagged YAML would silently corrupt `nh bench run`.
    Env-independent — asserts schema + honesty invariants, never on-disk repo
    paths (those are the operator's local clones, absent in CI)."""
    from no_human.eval.bench_task import NORTHSTAR_DIR

    specs = load_bench_tasks(NORTHSTAR_DIR)
    ids = [s.id for s in specs]
    assert len(ids) == len(set(ids)), "duplicate spec ids in the curated suite"
    core = [s for s in specs if s.subset == "core"]
    # No-shrink guard (the operator's tamper-guard philosophy applied to the
    # bench): the curated core suite is 36 after the multi-project expansion; a
    # drop is intentional and must update this floor visibly.
    assert len(core) >= 36, f"curated core suite shrank: {len(core)}"
    for s in core:
        assert s.id.startswith("ns-"), s.id
        assert s.title.strip(), f"{s.id}: empty title"
        assert s.request.strip(), f"{s.id}: empty request"
        # NO-LEAK: acceptance_criteria and holdout are the only hand-writable
        # fields; they must be empty or a curator could smuggle the solution
        # into the spec (critical principle #1).
        assert not s.acceptance_criteria, f"{s.id}: acceptance_criteria must be empty (leak vector)"
        assert not s.holdout.strip(), f"{s.id}: holdout must be empty (leak vector)"
        # A pathological `request` is an operator-pasted full transcript (message
        # #1 = a completed conversation), which embeds the solution — the largest
        # legitimate initial request in the suite is ~27 KB, so a >50 KB request
        # is a transcript-paste leak, not an ask. (Caught a 500 KB paste in G-2.)
        assert len(s.request) < 50_000, (
            f"{s.id}: request is {len(s.request)} chars — a pasted transcript "
            "(solution leak), not an initial request")
        # A runnable spec must name its repo; a non-runnable coverage row must
        # say WHY it can't run — so the suite stays honestly accountable.
        if s.runnable:
            assert s.repo.get("path"), f"{s.id}: runnable but no repo path"
        else:
            assert s.skip_reason.strip(), f"{s.id}: non-runnable without a reason"


def test_every_gated_spec_records_why_it_is_gated():
    """A spec that expects an escalation must say WHY.

    Without it, "the agent failed to escalate here" and "this spec should never
    have been gated" are the same row, and every honest-escalation figure needs
    a caveat nobody can settle from the data. `skip_reason` beside it is the
    same idea for `runnable: false`; this is that idiom applied to the gate.

    The 14 specs that predate the field carry the `unrecorded` sentinel, NOT a
    reconstructed reason. Their request text hints at one — external systems a
    replay cannot reach, local files that no longer exist — but writing an
    inference into a corpus makes it indistinguishable from a record for every
    reader afterwards. The sentinel is deliberately not a description, so it can
    never be mistaken for evidence.

    What this guard is FOR is the next one: a newly gated spec with no reason
    fails here rather than silently joining the unadjudicable pile.
    """
    from no_human.eval.bench_task import NORTHSTAR_DIR, load_bench_tasks

    specs = load_bench_tasks(NORTHSTAR_DIR)
    gated = [s for s in specs if s.expect_escalation]
    # Discovery that finds nothing passes vacuously. The corpus has had gated
    # specs since it was built; zero of them means the loader or the field was
    # renamed, not that the corpus is clean.
    assert gated, (
        "no gated specs found at all — expect_escalation stopped being read, "
        "or the corpus moved. This guard is measuring nothing.")
    missing = sorted(s.id for s in gated if not s.escalation_reason.strip())
    assert not missing, (
        f"{len(missing)} gated spec(s) record no reason for the gate: {missing}\n"
        "Add `escalation_reason:` saying what the agent cannot get past — a "
        "system a replay cannot reach, a file that no longer exists, a decision "
        "only a human can make. If the reason is genuinely unknown, say that "
        "explicitly; do not reconstruct one from the request text.")
