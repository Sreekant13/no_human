#!/usr/bin/env bash
# Nightly funnel-regression eval — what the launchd job at 03:00 runs.
#
# Usage:
#   ./scripts/nightly_eval.sh              # run it
#   ./scripts/nightly_eval.sh --help
#
# Environment (all optional):
#   NH_NIGHTLY_HOME     the run's OWN no_human home   (default ~/.no_human-nightly)
#   NH_NIGHTLY_OUT      where the report lands        (default $NH_NIGHTLY_HOME/out)
#   NH_NIGHTLY_TIMEOUT  hard wall clock, seconds      (default 14400 = 4h)
#
# THE HOME IS NOT ~/.no_human, and that is the point: the nightly run has its
# own database, worktree root and port so it cannot disturb the queue the
# operator is looking at. `funnel_eval._assert_isolated` re-checks that in
# Python; this is the outer half of the same guarantee.
#
# This job is also where the NIGHTLY TEST LANE runs. GitHub Actions minutes
# are exhausted and ci.yml deliberately carries no `schedule:` (a cron there
# would also fire the windows job, which bills at 2x), so this machine — which
# already wakes at 03:00 — is where `-m "slow or nightly"` actually runs. It
# runs FIRST, because the eval below has a four-hour timeout that would
# otherwise pre-empt it, and its failure does not stop the eval — both
# verdicts are wanted from one run.
#
# THE TEST LANE GETS A THROWAWAY HOME OF ITS OWN, separate from both the
# operator's ~/.no_human and this script's NH_NIGHTLY_HOME. See the comment at
# the invocation: the suite binds its database path from HOME at import time.
#
# Exit code IS the verdict: 0 only when the nightly test lane passed AND every
# eval tier passed and the ratchet held. The eval's own code wins when both
# fail, so a timeout is still 124, like timeout(1).
#
# NOT SAFE TO RUN CONCURRENTLY WITH THE 03:00 JOB. There is no run lock here,
# and since 6b4a73f6 a second concurrent run rmtree's the live work tree.
# Nothing below changes that; adding the test lane does not make a manual run
# alongside the scheduled one safe.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  # The header block IS the help — printed up to the first line of code, so
  # it cannot drift out of date with the range of a hardcoded sed.
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0
fi

NH_NIGHTLY_HOME="${NH_NIGHTLY_HOME:-$HOME/.no_human-nightly}"
NH_NIGHTLY_OUT="${NH_NIGHTLY_OUT:-$NH_NIGHTLY_HOME/out}"
NH_NIGHTLY_TIMEOUT="${NH_NIGHTLY_TIMEOUT:-14400}"

# ---------------------------------------------------------------------------
# Env checks. Every one reports before the first exit, so a fresh machine
# learns all of what it is missing in one run rather than one thing per run.
# ---------------------------------------------------------------------------
missing=0
for tool in git uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "MISSING: $tool is not on PATH" >&2
    missing=1
  fi
done
if [ "$NH_NIGHTLY_HOME" = "$HOME/.no_human" ]; then
  echo "REFUSING: NH_NIGHTLY_HOME is the operator's own ~/.no_human" >&2
  missing=1
fi
[ "$missing" -eq 0 ] || { echo "fix the above and re-run" >&2; exit 1; }

mkdir -p "$NH_NIGHTLY_OUT"

# ---------------------------------------------------------------------------
# Timeout guard. ONE implementation, deliberately — an earlier version
# preferred timeout(1)/gtimeout when present, which made the guard's behaviour
# depend on which binary the machine happened to have while this comment
# promised a group kill on all of them. macOS ships no timeout(1) at all
# (verified: `timeout: command not found`), so the fallback was the only path
# ever actually exercised here and the other two were untested claims.
#
# What it does: `set -m` puts the job in its OWN process group, so
# `kill -- -PID` reaches the whole tree. That is the point — killing only the
# shell's direct child leaves the Agent SDK subprocesses running, which is
# exactly the hang this guard exists for.
#
# ponytail: polls once a second. Fine for a 4-hour bound; if a tighter one is
# ever wanted, reach for coreutils rather than sharpening this.
# ---------------------------------------------------------------------------
run_guarded() {
  set -m
  "$@" &
  local pid=$! rc=0 waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$NH_NIGHTLY_TIMEOUT" ]; then
      echo "TIMEOUT after ${NH_NIGHTLY_TIMEOUT}s — killing the run's process group" >&2
      kill -9 -- "-$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid" || rc=$?
  set +m
  return "$rc"
}

# caffeinate -i: an unattended 03:00 run must not be suspended halfway through
# by idle sleep. macOS only; absent everywhere else, so it is optional.
CAFFEINE=()
command -v caffeinate >/dev/null 2>&1 && CAFFEINE=(caffeinate -i)

# The nightly test lane. Guarded like the eval so a wedged xdist worker cannot
# hold the machine until morning, and its code is captured rather than allowed
# to abort the script under `set -e`: a red lane must still leave an eval
# verdict behind.
#
# IT RUNS UNDER ITS OWN HOME, and that is not tidiness. tests/conftest.py says
# it in the repo's own words: `config.DB_PATH` and `config.CONFIG_PATH` bind at
# IMPORT time from `Path.home()/".no_human"`, so the suite writes `cache/`,
# `worktrees/` and `config.yaml` into whatever HOME it is given and "a branch
# carrying a migration replays that migration into the operator's live database
# — which has actually happened". The mitigation there is "a procedure rather
# than a guard", and an unattended 03:00 job cannot follow a procedure. Two
# things make it worse than the general case here: the live server holds
# `~/.no_human/no_human.db` open, and this lane is exactly the e2e/orchestrator
# tail that creates and rmtree's directories under `~/.no_human/worktrees/`,
# where a live task worktree sits. NH_NIGHTLY_HOME is the eval's isolation and
# is not reused: the lane gets a throwaway of its own, removed on exit.
#
# UV_CACHE_DIR is pinned to the real one first, because uv resolves its cache
# from HOME (`uv cache dir` under a fresh HOME points inside it) and an
# unpinned cache means re-downloading the whole environment every night.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
lane_home="$(mktemp -d)"
trap 'rm -rf "$lane_home"' EXIT
echo "=== nightly test lane: ./scripts/run_tests.sh nightly (HOME=$lane_home) ==="
lane_rc=0
run_guarded "${CAFFEINE[@]}" env HOME="$lane_home" ./scripts/run_tests.sh nightly || lane_rc=$?
echo "=== nightly test lane exit $lane_rc ==="

echo "=== nightly funnel eval: home=$NH_NIGHTLY_HOME out=$NH_NIGHTLY_OUT ==="
rc=0
run_guarded "${CAFFEINE[@]}" uv run python -m no_human.eval.funnel_eval \
  --home "$NH_NIGHTLY_HOME" --out "$NH_NIGHTLY_OUT" || rc=$?

echo "=== eval exit $rc, test lane exit $lane_rc — read $NH_NIGHTLY_OUT/SUMMARY.md ==="
# The eval's code wins when both are non-zero, so 124 still means what the
# header says it means; the lane can only turn a 0 into a failure, never mask
# one.
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
exit "$lane_rc"
