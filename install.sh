#!/usr/bin/env bash
#
# Install the Bud Foundry skills for Claude Code, Codex, or any agent that
# reads skill directories. Safe to re-run: it replaces its own symlinks and
# touches nothing else.
#
#   ./install.sh                 # install for the current user (~/.claude/skills)
#   ./install.sh --project       # install into ./.claude/skills of the current repo
#   ./install.sh --dest <dir>    # install somewhere specific
#   ./install.sh --copy          # copy instead of symlinking (for shipping elsewhere)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/skills"
DEST=""
MODE="link"

while [ $# -gt 0 ]; do
  case "$1" in
    --project) DEST="$PWD/.claude/skills"; shift ;;
    --dest)    DEST="${2:?--dest needs a directory}"; shift 2 ;;
    --copy)    MODE="copy"; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
DEST="${DEST:-$HOME/.claude/skills}"

command -v python3 >/dev/null 2>&1 || {
  echo "error: python3 is required (it is the only dependency)." >&2; exit 1; }

mkdir -p "$DEST"
count=0
for skill in "$SRC"/*/; do
  name="$(basename "$skill")"
  target="$DEST/$name"
  rm -rf "$target"
  if [ "$MODE" = "copy" ]; then cp -R "$skill" "$target"; else ln -s "${skill%/}" "$target"; fi
  count=$((count + 1))
done
echo "Installed $count skills into $DEST"

# Put the shared `bud` command on PATH.
bash "$SRC/bud-platform/scripts/setup.sh"

cat <<'EOF'

Next steps:

  export BUD_API_URL=https://app.<your-domain>     # the Bud API origin
  export BUD_EMAIL=you@example.com
  export BUD_PASSWORD=...                          # or omit to be prompted
  bud login && bud whoami

Then just ask your agent for what you want - for example
"deploy the cheapest model that can handle 100 concurrent requests".
EOF
