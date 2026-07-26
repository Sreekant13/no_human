#!/usr/bin/env bash
# One-command bootstrap for no_human: checks prerequisites, installs the
# project into a local .venv, verifies the Claude CLI, seeds ~/.no_human/,
# then runs `nh doctor`.
#
# Idempotent: safe to re-run. Never overwrites an existing config.yaml or
# .env, never auto-installs system tools, never downloads the Claude CLI.
#
# Usage:
#   ./scripts/bootstrap.sh            # run it
#   ./scripts/bootstrap.sh --dry-run  # print what would happen, touch nothing
#   ./scripts/bootstrap.sh --help
set -euo pipefail

cd "$(dirname "$0")/.."

DRY=0

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh [--dry-run] [--help]

Runs the 5-step no_human bootstrap:
  1. Check uv is on PATH, and that a python3.12+ interpreter is available
     (system python3, or one uv can find/provision)
  2. Create/reuse .venv and `uv pip install -e .`
  3. Check the Claude Code CLI is on PATH
  4. Create ~/.no_human/ and a config.yaml from the template if absent
  5. Run `nh doctor` and print next steps

  --dry-run   Print what would happen; make no filesystem or PATH changes.
  --help, -h  Show this help and exit.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "unknown flag: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

say_step() {
  echo "==> [$1/5] $2"
}

# Wrapper: in --dry-run, print the command instead of running it.
run() {
  if [ "$DRY" = "1" ]; then
    echo "DRY-RUN: would run: $*"
  else
    "$@"
  fi
}

NH_HOME="${NO_HUMAN_HOME:-$HOME/.no_human}"

# --------------------------------------------------------------------- #
# Step 1: python3.12+ and uv
# --------------------------------------------------------------------- #
say_step 1 "Checking python3.12+ and uv"

UV_OK=1
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found."
  echo "Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
  UV_OK=0
else
  echo "uv: $(uv --version 2>&1)"
fi

PYTHON_OK=0
PY_VIA_UV=0
if command -v python3 >/dev/null 2>&1; then
  PY_VERSION="$(python3 --version 2>&1 | awk '{print $2}')"
  PY_MAJOR="$(echo "$PY_VERSION" | cut -d. -f1)"
  PY_MINOR="$(echo "$PY_VERSION" | cut -d. -f2)"
  if [ "${PY_MAJOR:-0}" -gt 3 ] || { [ "${PY_MAJOR:-0}" -eq 3 ] && [ "${PY_MINOR:-0}" -ge 12 ]; }; then
    echo "python3: $PY_VERSION"
    PYTHON_OK=1
  else
    echo "system python3 is $PY_VERSION (no_human needs 3.12+)"
  fi
else
  echo "python3 not found on PATH."
fi

# A 3.12+ interpreter doesn't have to be the system one: uv can find or
# provision its own, so a stock Mac's older/absent system python3 alone must
# not turn the script away.
if [ "$PYTHON_OK" = "0" ] && [ "$UV_OK" = "1" ]; then
  if uv python find 3.12 >/dev/null 2>&1; then
    echo "uv reports Python 3.12 is available (uv python find 3.12) — will use it"
  else
    echo "uv will provision Python 3.12 for the venv (uv venv --python 3.12)"
  fi
  PYTHON_OK=1
  PY_VIA_UV=1
fi

if [ "$PYTHON_OK" = "0" ]; then
  echo "Install Python 3.12+: https://python.org or \`brew install python@3.12\`"
fi

if [ "$PYTHON_OK" = "0" ] || [ "$UV_OK" = "0" ]; then
  echo "Install the missing tool(s) above, then re-run this script."
  exit 1
fi

# --------------------------------------------------------------------- #
# Step 2: venv + editable install
# --------------------------------------------------------------------- #
say_step 2 "Creating .venv and installing no_human (editable)"

if [ -d .venv ]; then
  echo ".venv already exists — reusing it"
elif [ "$PY_VIA_UV" = "1" ]; then
  run uv venv --python 3.12
else
  run uv venv
fi
run uv pip install -e .

# --------------------------------------------------------------------- #
# Step 3: Claude Code CLI
# --------------------------------------------------------------------- #
say_step 3 "Checking the Claude Code CLI"

if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code CLI not found. Install it: npm install -g @anthropic-ai/claude-code (https://claude.com/claude-code)"
  exit 1
fi
echo "claude: $(claude --version 2>&1 || true)"

# --------------------------------------------------------------------- #
# Step 4: ~/.no_human/ + config.yaml template
# --------------------------------------------------------------------- #
say_step 4 "Setting up $NH_HOME"

if [ "$DRY" = "1" ]; then
  echo "DRY-RUN: would run: mkdir -p $NH_HOME"
  if [ -f "$NH_HOME/config.yaml" ]; then
    echo "config.yaml exists — leaving untouched"
  else
    echo "DRY-RUN: would run: cp scripts/config.yaml.template $NH_HOME/config.yaml"
  fi
else
  mkdir -p "$NH_HOME"
  if [ -f "$NH_HOME/config.yaml" ]; then
    echo "config.yaml exists — leaving untouched"
  else
    cp scripts/config.yaml.template "$NH_HOME/config.yaml"
    echo "created $NH_HOME/config.yaml from template"
  fi
fi

# --------------------------------------------------------------------- #
# Step 5: activate + doctor + next steps
# --------------------------------------------------------------------- #
say_step 5 "Running nh doctor"

if [ "$DRY" = "1" ]; then
  echo "DRY-RUN: would run: source .venv/bin/activate"
  echo "DRY-RUN: would run: nh doctor"
else
  if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
  fi
  run nh doctor
fi

echo ""
echo "Next steps:"
echo "  nh auth use <profile>"
echo "  nh start"
