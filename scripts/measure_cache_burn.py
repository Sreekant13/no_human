#!/usr/bin/env python3
"""Offline, token-free before/after for the coder-in-attempt cache-burn fix.

The fix (`agent/backend.py::DEFAULT_CODER_COMPACT_WINDOW_TOKENS`) lowers the
coder's SDK auto-compact window from the CLI's own default (the full ~200k
model context, under which auto-compaction almost never fired for coder
sessions — 0 `compaction` events across the whole recorded history, see the
query in this module's docstring test) to 140k tokens. Below that threshold,
the SDK's OWN compaction — not a hand-rolled replacement — resets the running
conversation, so the per-turn "resend the whole conversation" cost (what cache
reads bill for) stops growing without bound.

This script does NOT re-run any coder task — this is an offline dev session
with no benchmark infrastructure to launch real attempts, and the fix changes
the vendor CLI's OWN opaque compaction behaviour, which cannot be replayed
deterministically after the fact. What it CAN do, and does, is compute a
COUNTERFACTUAL over REAL recorded per-turn context growth: for every past
coder attempt, replay the exact sequence of `cache_creation_tokens` (the
SDK's own per-turn "new tokens written to the cache this turn" figure —
always non-negative, unlike diffing the noisier `cache_read_tokens` series)
already recorded in `task_events`, and ask "if a compaction had fired every
time the running total crossed the new window, what would the running
context — and so the per-turn cache-READ cost, which bills for resending it —
have been instead?" Both the AS-RECORDED (no compaction) and WINDOWED replay
are built from the SAME growth series, so the windowed total can only ever be
lower, by construction. The per-turn GROWTH figures are real, measured
numbers; only the post-compaction floor (what compaction resets the context
DOWN to) is an estimate, because no historical compaction ever fired to
measure it from — that estimate is named and deliberately conservative (see
`--post-compact-floor`).

    python scripts/measure_cache_burn.py --db ~/.no_human/no_human.db --role coder

Fails CLOSED: zero attempts, zero usage rows, or an unreadable DB is a
non-zero exit with a loud message — never a quiet "0% reduction, clean".
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

#: `orchestrator.CODER_ROLE` — duplicated as a literal (not imported) so this
#: script has no import-time dependency on the app package, and can run
#: against a DB from a differently-versioned checkout.
CODER_SOURCE = "agent"

#: The fix's own default (`agent/backend.py::DEFAULT_CODER_COMPACT_WINDOW_TOKENS`).
DEFAULT_WINDOW_TOKENS = 140_000

#: What compaction resets the running context DOWN to, as a fraction of the
#: window. UNMEASURED (0 historical compaction events to measure it from,
#: see the module docstring) — this is a stated estimate, not a fact, and it
#: is deliberately LOW (i.e. conservative in the sense of assuming compaction
#: buys LESS headroom back) only in that a smaller floor delays the NEXT
#: simulated compaction, understating how often the fix would fire; a
#: shallower compaction (bigger floor) makes each individual reduction
#: smaller. Reported explicitly in the output so a reader can re-derive the
#: number under a different assumption.
DEFAULT_POST_COMPACT_FLOOR_FRACTION = 0.3


class EmptyInputSet(SystemExit):
    """Raised (as a SystemExit subclass) to fail CLOSED and loud."""

    def __init__(self, message: str):
        super().__init__(f"FAIL (empty input set): {message}")


def _load_coder_usage_by_attempt(
    db_path: Path, *, task_id: str | None = None,
) -> dict[tuple[str, int], list[dict]]:
    """Real recorded coder-session `usage` events, grouped by (task_id, attempt).

    Attempt boundaries come from `attempt_start` events (`orchestrator.py`'s
    own attempt loop emits one per attempt, before the coder session starts),
    not from a synthetic reset heuristic — a real event the product already
    writes, not a guess about the data's shape.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        where = "AND task_id = ?" if task_id else ""
        params = (task_id,) if task_id else ()

        starts = con.execute(
            f"""
            SELECT task_id, ts, json_extract(data, '$.text') AS text
            FROM task_events
            WHERE json_extract(data, '$.kind') = 'attempt_start' {where}
            ORDER BY task_id, ts
            """,
            params,
        ).fetchall()
        boundaries: dict[str, list[tuple[float, int]]] = {}
        for row in starts:
            text = row["text"] or ""
            # "attempt N/M"
            try:
                n = int(text.split()[1].split("/")[0])
            except (IndexError, ValueError):
                continue
            boundaries.setdefault(row["task_id"], []).append((row["ts"], n))

        usage_rows = con.execute(
            f"""
            SELECT task_id, ts,
                   json_extract(data, '$.tokens_used') AS tokens_used,
                   json_extract(data, '$.cache_read_tokens') AS cache_read_tokens,
                   json_extract(data, '$.cache_creation_tokens') AS cache_creation_tokens
            FROM task_events
            WHERE json_extract(data, '$.kind') = 'usage'
              AND json_extract(data, '$.source') = ?
              {where}
            ORDER BY task_id, ts
            """,
            (CODER_SOURCE, *params),
        ).fetchall()
    finally:
        con.close()

    out: dict[tuple[str, int], list[dict]] = {}
    for row in usage_rows:
        bnds = boundaries.get(row["task_id"]) or []
        attempt_n = None
        for ts, n in bnds:
            if row["ts"] >= ts:
                attempt_n = n
            else:
                break
        if attempt_n is None:
            continue  # usage recorded before any attempt_start — not attributable
        key = (row["task_id"], attempt_n)
        out.setdefault(key, []).append({
            "tokens_used": int(row["tokens_used"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "cache_creation_tokens": int(row["cache_creation_tokens"] or 0),
        })
    return out


def _replay(
    per_turn_growth: list[int], *, window: int | None, floor_fraction: float,
) -> tuple[list[int], int]:
    """Replay real per-turn context GROWTH into a running "size before this
    turn" series — the quantity that drives the per-turn cache-read cost,
    since the whole prior conversation is what gets resent and billed as a
    cache read.

    `per_turn_growth[i]` is `cache_creation_tokens` for turn i: the SDK's own
    reported "new tokens written to the cache this turn", which — unlike
    diffing the noisy, sometimes-non-monotonic `cache_read_tokens` series
    (an earlier version of this script did that, and got attempts where the
    "after" total came out HIGHER than "before" — a modelling bug, not a
    real effect) — is a genuinely per-turn, always-non-negative measurement.

    `window=None` means "no compaction ever" (the AS-RECORDED regime: 0
    historical `compaction` events across the whole database — see the
    module docstring). With a window, the running total resets to
    `floor_fraction * window` every time it would exceed the window,
    modelling the SDK's own auto-compaction. Because both series are built
    from the IDENTICAL growth input and the windowed series can only ever
    reset LOWER, `after[i] <= before[i]` at every turn by construction —
    the class of bug the previous version had is structurally excluded here.
    """
    floor = int(window * floor_fraction) if window else 0
    series: list[int] = []
    running = 0
    compactions = 0
    for growth in per_turn_growth:
        running += max(0, growth)
        if window is not None and running > window:
            compactions += 1
            running = floor
        series.append(running)
    return series, compactions


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round(pct / 100 * (len(values) - 1))))
    return values[idx]


def measure(
    db_path: Path, *, task_id: str | None, window: int, floor_fraction: float,
) -> dict:
    if not db_path.exists():
        raise EmptyInputSet(f"no such database: {db_path}")

    by_attempt = _load_coder_usage_by_attempt(db_path, task_id=task_id)
    if not by_attempt:
        raise EmptyInputSet(
            f"no coder ('{CODER_SOURCE}') usage events found "
            f"(task_id={task_id!r}) in {db_path}"
        )

    before_totals: list[int] = []
    after_totals: list[int] = []
    per_attempt: list[dict] = []
    total_compactions = 0

    for (task, attempt_n), rows in sorted(by_attempt.items()):
        if len(rows) < 2:
            continue  # a one-message attempt has nothing to compact
        growth = [r["cache_creation_tokens"] for r in rows]
        before_series, _ = _replay(growth, window=None, floor_fraction=floor_fraction)
        after_series, compactions = _replay(
            growth, window=window, floor_fraction=floor_fraction)
        before, after = sum(before_series), sum(after_series)
        before_totals.append(before)
        after_totals.append(after)
        total_compactions += compactions
        per_attempt.append({
            "task_id": task, "attempt": attempt_n, "turns": len(rows),
            "before_modelled_burn": before, "after_modelled_burn": after,
            "recorded_cache_read": sum(r["cache_read_tokens"] for r in rows),
            "compactions": compactions,
        })

    if not before_totals:
        raise EmptyInputSet(
            "every matched attempt had fewer than 2 usage events — "
            "nothing long enough to compact"
        )

    reductions = [
        (1 - a / b) if b else 0.0 for b, a in zip(before_totals, after_totals)
    ]
    return {
        "db": str(db_path),
        "window_tokens": window,
        "post_compact_floor_fraction": floor_fraction,
        "attempts_measured": len(before_totals),
        "before_median_modelled_burn": statistics.median(before_totals),
        "after_median_modelled_burn": statistics.median(after_totals),
        "before_p95_modelled_burn": _percentile(before_totals, 95),
        "after_p95_modelled_burn": _percentile(after_totals, 95),
        "median_reduction_pct": round(statistics.median(reductions) * 100, 1),
        "simulated_compactions_total": total_compactions,
        "per_attempt": per_attempt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--role", default="coder",
                         help="Only 'coder' is measured; any other value is refused "
                              "(the fix is scoped to the coder role only).")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_TOKENS)
    parser.add_argument("--post-compact-floor", type=float,
                         default=DEFAULT_POST_COMPACT_FLOOR_FRACTION,
                         help="Post-compaction context size as a fraction of "
                              "--window. UNMEASURED — see module docstring.")
    args = parser.parse_args(argv)

    if args.role != "coder":
        print(f"FAIL: --role must be 'coder' (got {args.role!r}); "
              "this fix touches no other role.", file=sys.stderr)
        return 2

    try:
        result = measure(
            args.db, task_id=args.task_id, window=args.window,
            floor_fraction=args.post_compact_floor,
        )
    except EmptyInputSet as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    print(
        f"\n{result['attempts_measured']} real coder attempts, "
        f"window={result['window_tokens']:,} tokens, "
        f"post-compact floor={result['post_compact_floor_fraction']:.0%} of window "
        "(estimate — no historical compaction event exists to measure this from):\n"
        f"  median modelled burn: {result['before_median_modelled_burn']:,.0f} -> "
        f"{result['after_median_modelled_burn']:,.0f} "
        f"({result['median_reduction_pct']}% reduction)\n"
        f"  P95 modelled burn:    {result['before_p95_modelled_burn']:,.0f} -> "
        f"{result['after_p95_modelled_burn']:,.0f}\n"
        f"  simulated compactions across all attempts: "
        f"{result['simulated_compactions_total']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
