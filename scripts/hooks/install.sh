#!/usr/bin/env bash
# Install (or remove) the no_human pre-commit gate for THIS clone.
#
# The gate blocks a commit that changes a RELEASE_MANIFEST.txt-pinned file
# without re-pinning it in the same commit -- the split-commit defect that keeps
# landing a red main and tripping the export gate. It is opt-in: nothing runs
# until you install it here, and installing touches only this clone's git config,
# never the shared, tracked repo.
#
# It symlinks scripts/hooks/pre-commit into this clone's active hooks directory,
# preserving any existing pre-commit hook as pre-commit.local (the gate does not
# chain to it -- restore it by hand if you had one). This leaves every other hook
# untouched, unlike setting core.hooksPath.
#
# Usage:
#   ./scripts/hooks/install.sh            # install
#   ./scripts/hooks/install.sh --uninstall
#   ./scripts/hooks/install.sh --help
#
# Alternative (git-native, affects ALL hooks for this clone):
#   git config core.hooksPath scripts/hooks     # enable
#   git config --unset core.hooksPath           # disable
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$(git rev-parse --show-toplevel)
HOOKS_DIR=$(cd "$ROOT" && git rev-parse --git-path hooks)
case "$HOOKS_DIR" in /*) : ;; *) HOOKS_DIR="$ROOT/$HOOKS_DIR" ;; esac
SRC="$ROOT/scripts/hooks/pre-commit"
DEST="$HOOKS_DIR/pre-commit"

usage() { sed -n '2,25p' "$0"; }

case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  --uninstall)
    if [ -L "$DEST" ]; then
      rm -f "$DEST"
      echo "Removed the no_human pre-commit gate ($DEST)."
      if [ -f "$DEST.local" ]; then
        mv "$DEST.local" "$DEST"
        echo "Restored your previous pre-commit hook."
      fi
    else
      echo "No no_human pre-commit gate symlink at $DEST; nothing to remove."
    fi
    exit 0 ;;
  "") : ;;
  *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
esac

chmod +x "$SRC"
mkdir -p "$HOOKS_DIR"
if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then
  mv "$DEST" "$DEST.local"
  echo "Preserved your existing pre-commit hook as $DEST.local"
fi
ln -sf "$SRC" "$DEST"
echo "Installed the no_human pre-commit gate:"
echo "  $DEST -> $SRC"
echo "It blocks a commit that changes a pinned file without re-pinning it."
echo "Remove it with: ./scripts/hooks/install.sh --uninstall"
