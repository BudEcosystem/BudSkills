#!/usr/bin/env bash
# Put `bud` on your PATH. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for d in "$HOME/.local/bin" "$HOME/bin"; do
  case ":$PATH:" in *":$d:"*) TARGET="$d"; break ;; esac
done
TARGET="${TARGET:-$HOME/.local/bin}"
mkdir -p "$TARGET"
ln -sf "$HERE/bud" "$TARGET/bud"

# needs to be on PATH too.

if ! command -v python3 >/dev/null 2>&1; then
  echo "warning: python3 not found - the toolkit needs it." >&2
fi

echo "Linked: $TARGET/bud -> $HERE/bud"
case ":$PATH:" in
  *":$TARGET:"*) echo "Ready. Run: bud login" ;;
  *) echo "Add to your shell profile:  export PATH=\"$TARGET:\$PATH\"" ;;
esac
