#!/usr/bin/env bash
# Run no_human test suite.
#
# Usage:
#   ./scripts/run_tests.sh              # full suite, parallel
#   ./scripts/run_tests.sh fast          # the PR lane: not slow, not nightly
#   ./scripts/run_tests.sh slow          # slow tests only (~5min)
#   ./scripts/run_tests.sh nightly       # the nightly lane: slow or nightly
#   ./scripts/run_tests.sh full          # all tests, parallel
#
# `fast` and `nightly` are the two halves of the CI split in
# .github/workflows/ci.yml, spelled identically on purpose: if the selectors
# here and there disagree, a green local run stops predicting the CI result,
# and a node can fall out of both lanes without anything going red.
# tests/test_test_lanes.py asserts the two files agree.
#
# Designed to work with any runner: local, agent-a, CI.
# Exit code 0 = all tests passed.
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-full}"
shift 2>/dev/null || true

# The board's role-attribution logic (web/src/eventRoles.js) decides which agent
# each event is drawn as. It is pure JS, so node's built-in runner exercises it —
# no extra dependency. Skipped when node isn't installed.
run_web_tests() {
  if command -v node >/dev/null 2>&1; then
    echo "=== Running web tests (node --test) ==="
    (cd web && node --test src/)
  else
    echo "=== Skipping web tests (node not installed) ==="
  fi
}

case "$MODE" in
  fast)
    echo "=== Running the PR lane (not slow, not nightly) ==="
    uv run pytest -q --tb=short -n auto -m "not slow and not nightly" "$@"
    run_web_tests
    ;;
  slow)
    echo "=== Running slow tests only ==="
    uv run pytest -q --tb=short -n auto -m slow "$@"
    ;;
  nightly)
    # -n 4, not -n auto: this one is run unattended by the 03:00 launchd job on
    # a machine that is also serving the live queue, and -n auto has wedged
    # this repo before. The other modes are left as they were.
    echo "=== Running the nightly lane (slow or nightly) ==="
    uv run pytest -q --tb=short -n 4 -m "slow or nightly" "$@"
    ;;
  full|*)
    echo "=== Running full test suite ==="
    uv run pytest -q --tb=short -n auto "$@"
    run_web_tests
    ;;
esac

echo ""
echo "✓ Tests passed (mode=$MODE)"
