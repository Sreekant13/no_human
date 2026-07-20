#!/usr/bin/env bash
# Run no_human test suite.
#
# Usage:
#   ./scripts/run_tests.sh              # full suite, parallel
#   ./scripts/run_tests.sh fast          # fast tests only (~50s), skip slow
#   ./scripts/run_tests.sh slow          # slow tests only (~5min)
#   ./scripts/run_tests.sh full          # all tests, parallel
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
    echo "=== Running fast tests only (skipping slow) ==="
    uv run pytest -q --tb=short -n auto -m "not slow" "$@"
    run_web_tests
    ;;
  slow)
    echo "=== Running slow tests only ==="
    uv run pytest -q --tb=short -n auto -m slow "$@"
    ;;
  full|*)
    echo "=== Running full test suite ==="
    uv run pytest -q --tb=short -n auto "$@"
    run_web_tests
    ;;
esac

echo ""
echo "✓ Tests passed (mode=$MODE)"
