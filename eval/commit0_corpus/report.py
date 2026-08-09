"""Report a Commit0 bench run the way the north star asks for it.

Identical in shape to eval/parcelo_corpus/report.py — it reads the same score
record northstar.py writes, so the two tiers report the same way.

PASS is the HOLDOUT (the vendored library's own real test suite), never the
judge. Both are printed so the gap between them is visible — that gap is where
an over-prescriptive holdout or an over-generous judge hides.

Cost is in the repo's own weighted-token unit (core.pricing: fresh 1.0,
cache-read 0.1, cache-write 1.25), summed over ALL SIX USAGE_ROLES — the score
record aggregates the roles into three price classes, so a per-role split is
not recoverable from it, and a USD figure would need the per-model split it
does not keep. Stated rather than guessed.
"""

import json
import statistics
import sys
from pathlib import Path

FRESH, READ, WRITE = 1.0, 0.1, 1.25


def weighted(s: dict) -> float:
    return (s["nh_tokens"] * FRESH
            + s["nh_cache_tokens"] * READ
            + s["nh_cache_creation_tokens"] * WRITE)


def _events(s: dict, *kinds: str) -> list[dict]:
    return [e for e in (s.get("events") or []) if e.get("kind") in kinds]


def attempts_used(s: dict) -> str:
    """'attempt N/3' lines are the only record of how many attempts a spec
    burned — `list_attempts` is summed into the token totals and then thrown
    away with the sandbox."""
    starts = _events(s, "attempt_start")
    return starts[-1]["text"].replace("attempt ", "") if starts else "—"


def review_verdict(s: dict) -> str:
    """The independent reviewer's own verdict, distinct from BOTH the holdout
    and the goal judge. Three different opinions; a benchmark that collapses
    them cannot say which gate let a bad PR through."""
    rev = _events(s, "review")
    if not rev:
        return "—"
    text = rev[-1]["text"]
    return text.split(" —")[0].strip()[:12] or "?"


def stop_reason(s: dict) -> str:
    """Why it stopped short, when it did. Empty for a spec that reached the
    gate — the absence of a stop signal is itself the answer there."""
    marks = _events(s, "escalation", "blocked", "budget_exhausted", "stuck",
                    "stagnation", "park", "lifetime_budget_exhausted")
    if marks:
        return f"{marks[-1]['kind']}: {marks[-1]['text'][:60]}"
    return ""


def main() -> int:
    d = json.load(open(sys.argv[1]))
    scores = d["scores"]
    print(f"run: {d.get('label')}  ({d.get('created_at')})  n={len(scores)}\n")

    hdr = (f"{'spec':40} {'outcome':18} {'hold':5} {'judge':5} {'review':7} "
           f"{'att':4} {'turns':>5} {'weighted tok':>13} {'wall s':>7}")
    print(hdr)
    print("-" * len(hdr))

    verdicts = {}
    for s in sorted(scores, key=lambda x: x["task_id"]):
        esc = s["expected_escalation"]
        if esc:
            ok = s["escalated_honestly"]
            hold = "esc"
        else:
            ok = s["mergeable"] is True
            hold = {True: "PASS", False: "FAIL", None: "—"}[s["mergeable"]]
        verdicts[s["task_id"]] = (ok, s)
        judge = {True: "PASS", False: "FAIL", None: "—"}[s["goal_satisfied"]]
        print(f"{s['task_id']:40} {s['outcome_status']:18} {hold:5} {judge:5} "
              f"{review_verdict(s):7} {attempts_used(s):4} "
              f"{s['nh_turns']:>5} {weighted(s):>13,.0f} "
              f"{s['nh_wall_clock_s']:>7.0f}")

    passed = [s for ok, s in verdicts.values() if ok]
    failed = [s for ok, s in verdicts.values() if not ok]
    n = len(verdicts)
    print(f"\nSUCCESS (holdout green / honest escalation): "
          f"{len(passed)}/{n} = {100.0 * len(passed) / n:.1f}%")

    judge_only = sum(1 for _, s in verdicts.values() if s["goal_satisfied"])
    print(f"  (the judge alone would have said {judge_only}/{n} = "
          f"{100.0 * judge_only / n:.1f}% — the holdout is the verdict)")

    if passed:
        print(f"median weighted tokens / successful ticket: "
              f"{statistics.median(weighted(s) for s in passed):,.0f}")
        print(f"median turns / successful ticket:            "
              f"{statistics.median(s['nh_turns'] for s in passed):.0f}")
        print(f"median wall clock / successful ticket:       "
              f"{statistics.median(s['nh_wall_clock_s'] for s in passed):.0f}s")
    print(f"median weighted tokens / ticket (all):       "
          f"{statistics.median(weighted(s) for s in scores):,.0f}")
    print(f"TOTAL weighted tokens for the run:           "
          f"{sum(weighted(s) for s in scores):,.0f}")

    if failed:
        print("\nFAILURE TAXONOMY")
        for s in failed:
            print(f"\n  {s['task_id']}")
            print(f"    outcome={s['outcome_status']}  turns={s['nh_turns']}  "
                  f"attempts={attempts_used(s)}  weighted={weighted(s):,.0f}  "
                  f"holdout={s['mergeable']}  judge={s['goal_satisfied']}  "
                  f"review={review_verdict(s)}")
            if stop_reason(s):
                print(f"    stopped short: {stop_reason(s)}")
            print(f"    judge note: {(s.get('notes') or '')[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
