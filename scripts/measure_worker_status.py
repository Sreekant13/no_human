#!/usr/bin/env python3
"""Before/after latency harness for the `/api/worker/status` stall (2026-09-03).

Live samples that opened the ticket: 5.5 s and 13.9 s responses, with the very
next poll returning in 0.044 s — a single-flight lock winner paying for a
synchronous `git rev-parse HEAD` (and, once behind, a second `git merge-base
--is-ancestor`) under `asyncio.to_thread`, versus a lock loser or same-HEAD
fast return. See `_loaded_code_stale` / `worker_status` in
`src/no_human/api/app.py` and `head_sha` / `staleness_note` in
`src/no_human/core/build_info.py`.

This script is READ-ONLY: it only issues `GET` requests against an already
running server (`nh serve`, typically `http://127.0.0.1:8420`) and times a
local `git rev-parse HEAD` for attribution — it never writes to the server and
never mutates the checkout.

    python scripts/measure_worker_status.py --url http://127.0.0.1:8420 --n 30

Run this BEFORE and AFTER the fix (same server instance ideally, or two runs
bracketing a restart) with the same load (e.g. four coder sessions busy) to
produce the before/after numbers the PR body needs. Stdlib only, matching
`scripts/measure_cache_burn.py`'s shape.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Endpoints measured by default. `/api/worker/status` is the endpoint the
#: ticket names; `/api/queue/health` is what the CLI's `pool_probe.py` polls
#: (deliverable 4 — report only, no code change to that file).
DEFAULT_PATHS = ("/api/worker/status", "/api/queue/health")


class EmptyInputSet(RuntimeError):
    """Zero successful samples for a path — fail closed, never print a fake 0."""

    def __init__(self, message: str):
        super().__init__(f"FAIL (empty input set): {message}")


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        raise EmptyInputSet("percentile of an empty sample set")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    frac = k - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _sample_path(base_url: str, path: str, n: int, timeout: float) -> dict:
    """Issue `n` sequential GETs against `base_url + path`, timing each.

    Sequential (not concurrent) on purpose: this measures what a single
    polling tab / `nh status` call actually experiences, which is exactly
    what the ticket's live samples describe — not a load-generation tool.
    """
    url = base_url.rstrip("/") + path
    elapsed: list[float] = []
    statuses: list[int] = []
    errors: list[str] = []
    samples: list[dict] = []
    for i in range(n):
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                resp.read()
                status = resp.status
        except urllib.error.URLError as exc:
            status = -1
            errors.append(f"sample {i}: {exc}")
        took = time.perf_counter() - start
        elapsed.append(took)
        statuses.append(status)
        samples.append({"i": i, "seconds": round(took, 4), "status": status})

    ok = [s for s, st in zip(elapsed, statuses) if st == 200]
    if not ok:
        raise EmptyInputSet(
            f"{path}: 0/{n} requests returned 200 "
            f"(errors: {errors[:3]}{'...' if len(errors) > 3 else ''})"
        )

    slowest = sorted(samples, key=lambda s: s["seconds"], reverse=True)[:5]
    return {
        "path": path,
        "count": len(ok),
        "count_total": n,
        "count_errors": n - len(ok),
        "p50_s": round(_percentile(ok, 50), 4),
        "p95_s": round(_percentile(ok, 95), 4),
        "max_s": round(max(ok), 4),
        "min_s": round(min(ok), 4),
        "slowest_samples": slowest,
        "errors": errors,
    }


def _time_git_rev_parse(checkout: Path, timeout: float) -> dict:
    """Attribution: how long does the exact git call the handler used to run
    (synchronously, per request) take on this box, right now."""
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=timeout,
        )
        took = time.perf_counter() - start
        return {
            "checkout": str(checkout),
            "seconds": round(took, 4),
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        took = time.perf_counter() - start
        return {
            "checkout": str(checkout), "seconds": round(took, 4),
            "returncode": None, "stdout": "", "stderr": "TIMEOUT",
        }


def measure(base_url: str, paths: list[str], n: int, timeout: float,
            checkout: Path) -> dict:
    per_path = [_sample_path(base_url, path, n, timeout) for path in paths]
    git_timing = _time_git_rev_parse(checkout, timeout=max(timeout, 10.0))
    return {"url": base_url, "n": n, "paths": per_path, "git_rev_parse_head": git_timing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8420",
                         help="Base URL of a running `nh serve` instance.")
    parser.add_argument("--n", type=int, default=30,
                         help="Number of sequential samples per path.")
    parser.add_argument("--path", action="append", dest="paths", default=None,
                         help="Repeatable. Defaults to /api/worker/status "
                              "and /api/queue/health.")
    parser.add_argument("--timeout", type=float, default=20.0,
                         help="Per-request timeout in seconds — deliberately "
                              "above the old 2x_GIT_TIMEOUT=20s ceiling so a "
                              "pre-fix run doesn't itself time out mid-measurement.")
    parser.add_argument("--checkout", type=Path, default=Path.cwd(),
                         help="Git checkout to time `rev-parse HEAD` against "
                              "for attribution (default: cwd).")
    args = parser.parse_args(argv)

    paths = args.paths or list(DEFAULT_PATHS)
    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return 2

    try:
        result = measure(args.url, paths, args.n, args.timeout, args.checkout)
    except EmptyInputSet as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    print(file=sys.stderr)
    for p in result["paths"]:
        print(
            f"{p['path']}: n={p['count']}/{p['count_total']} "
            f"p50={p['p50_s']:.3f}s p95={p['p95_s']:.3f}s max={p['max_s']:.3f}s",
            file=sys.stderr,
        )
    git = result["git_rev_parse_head"]
    print(
        f"git rev-parse HEAD ({git['checkout']}): {git['seconds']:.3f}s "
        f"(rc={git['returncode']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
