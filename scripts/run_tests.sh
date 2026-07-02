#!/usr/bin/env bash
# Run no_human test suite.
#
# Usage:
#   ./scripts/run_tests.sh              # full suite, parallel
#   ./scripts/run_tests.sh fast          # fast tests only (~50s), skip slow
#   ./scripts/run_tests.sh slow          # slow tests only (~5min)
#   ./scripts/run_tests.sh full          # all tests, parallel
#
# Designed to work with any runner: local, Devin, CI.
# Exit code 0 = all tests passed.
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-full}"
shift 2>/dev/null || true

case "$MODE" in
  fast)
    echo "=== Running fast tests only (skipping slow) ==="
    uv run pytest -q --tb=short -n auto -m "not slow" "$@"
    ;;
  slow)
    echo "=== Running slow tests only ==="
    uv run pytest -q --tb=short -n auto -m slow "$@"
    ;;
  full|*)
    echo "=== Running full test suite ==="
    uv run pytest -q --tb=short -n auto "$@"
    ;;
esac

echo ""
echo "✓ Tests passed (mode=$MODE)"
