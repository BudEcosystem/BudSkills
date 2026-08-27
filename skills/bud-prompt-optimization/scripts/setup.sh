#!/usr/bin/env bash
#
# Prepare the prompt-optimisation toolkit and report exactly what works here.
#
#   ./setup.sh                 # check everything, link the tools onto PATH
#   ./setup.sh --no-link       # check only, change nothing
#   ./setup.sh --optional      # additionally offer the third-party escape hatches
#
# The toolkit itself needs nothing but python3. It never needs a third-party
# API key. This script tells you which of the optional paths are available
# rather than assuming any of them are.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINK=1
OPTIONAL=0
FAIL=0
WARN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-link)  LINK=0 ;;
    --optional) OPTIONAL=1 ;;
    -h|--help)  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

ok()   { printf '  [ ok ] %s\n' "$*"; }
no()   { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  [ -- ] %s\n' "$*"; WARN=$((WARN+1)); }

echo "Bud prompt optimisation - environment check"
echo
echo "Required:"

# ---------------------------------------------------------------- python
if command -v python3 >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  if python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
    ok "python3 $PYV (stdlib only - no pip install needed)"
  else
    no "python3 $PYV is too old; 3.9 or newer is required"
  fi
else
  no "python3 not found - it is the only hard dependency"
fi

# ---------------------------------------------------------------- toolkit
TOOLKIT="$(cd "$HERE/../../bud-platform/scripts" 2>/dev/null && pwd || true)"
if [ -n "$TOOLKIT" ] && [ -f "$TOOLKIT/budkit/__init__.py" ]; then
  ok "shared client found at $TOOLKIT"
else
  no "shared client (budkit) not found - install the whole skill set, or export BUD_TOOLKIT_DIR"
fi

# ---------------------------------------------------------------- scripts
for tool in gen-cases run-cases score-run guard propose-prompt; do
  if [ -x "$HERE/$tool" ]; then
    if python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$HERE/$tool" 2>/dev/null; then
      ok "$tool"
    else
      no "$tool does not parse"
    fi
  else
    no "$tool missing or not executable"
  fi
done

echo
echo "Connection (only needed to RUN cases; writing and scoring them works offline):"

if command -v bud >/dev/null 2>&1; then
  ok "bud is on PATH"
  if bud whoami >/dev/null 2>&1; then
    ok "signed in as $(bud whoami 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("email","?"))' 2>/dev/null)"
  else
    warn "not signed in - run: bud login   (set BUD_API_URL, BUD_EMAIL, BUD_PASSWORD first)"
  fi
else
  warn "bud not on PATH - run ../../bud-platform/scripts/setup.sh"
fi

GATEWAY="${BUD_GATEWAY_URL:-}"
if [ -z "$GATEWAY" ] && [ -n "${BUD_API_URL:-}" ]; then
  GATEWAY="$(printf '%s' "$BUD_API_URL" | sed -E 's#^(https?://)app\.#\1gateway.#')"
fi
if [ -n "$GATEWAY" ]; then
  ok "inference gateway: $GATEWAY"
  if [ -n "${BUD_GATEWAY_TOKEN:-}" ]; then
    ok "gateway token present in the environment"
  else
    warn "no BUD_GATEWAY_TOKEN - the tools mint one themselves, or: export BUD_GATEWAY_TOKEN=\$(bud token)"
  fi
else
  warn "cannot derive the gateway origin - set BUD_GATEWAY_URL or BUD_API_URL"
fi

# ---------------------------------------------------------------- PATH links
if [ "$LINK" = 1 ]; then
  TARGET=""
  for d in "$HOME/.local/bin" "$HOME/bin"; do
    case ":$PATH:" in *":$d:"*) TARGET="$d"; break ;; esac
  done
  TARGET="${TARGET:-$HOME/.local/bin}"
  mkdir -p "$TARGET" 2>/dev/null
  echo
  echo "Linking into $TARGET (prefixed, so nothing on your PATH is shadowed):"
  for tool in gen-cases run-cases score-run guard propose-prompt; do
    if ln -sf "$HERE/$tool" "$TARGET/bud-$tool" 2>/dev/null; then
      ok "bud-$tool"
    else
      warn "could not link bud-$tool (call it by path instead: $HERE/$tool)"
    fi
  done
  case ":$PATH:" in
    *":$TARGET:"*) ;;
    *) warn "add to your shell profile:  export PATH=\"$TARGET:\$PATH\"" ;;
  esac
fi

# ---------------------------------------------------------------- optional
if [ "$OPTIONAL" = 1 ]; then
  echo
  echo "Optional third-party tools. None of these are needed, and each is a"
  echo "large install. See references/research-findings.md for why they are"
  echo "optional rather than built in."
  echo

  if command -v node >/dev/null 2>&1; then
    NODEV="$(node --version 2>/dev/null)"
    echo "  node $NODEV found."
    echo "    promptfoo: declarative suites + a browser report. Works against your"
    echo "    installation with no third-party key, but installs ~2.1 GB of packages"
    echo "    and needs Node 22.22+. Only worth it if you want the UI."
    echo "      mkdir -p ~/.bud/tools/promptfoo && cd ~/.bud/tools/promptfoo && npm install promptfoo"
  else
    echo "  node not found - promptfoo (the only Node option worth considering) is unavailable."
  fi
  echo
  echo "  DSPy: automatic few-shot/instruction search. Works against your installation"
  echo "  (pass api_base + the gateway token), MIT, actively maintained. ~166 MB, or"
  echo "  ~243 MB with the optuna extra that MIPROv2 needs. Worth it only if you want"
  echo "  BootstrapFewShot-style search beyond what propose-prompt does."
  if python3 -c 'import dspy' 2>/dev/null; then
    ok "dspy already importable"
  else
    echo "      python3 -m venv ~/.bud/tools/dspy-venv"
    echo "      ~/.bud/tools/dspy-venv/bin/pip install 'dspy'          # BootstrapFewShot, ~8 LM calls"
    echo "      ~/.bud/tools/dspy-venv/bin/pip install 'dspy[optuna]'  # adds MIPROv2, ~920 LM calls"
  fi
  echo
  echo "  PromptPex: NOT installable. Its runtime was archived by its authors and its"
  echo "  package on the registry is an empty deprecation stub that its own dependency"
  echo "  range resolves to, so a plain install silently produces nothing that runs."
  echo "  Its method is reimplemented in gen-cases instead. Details in references/."
fi

echo
if [ "$FAIL" -gt 0 ]; then
  echo "$FAIL required check(s) failed - fix those before starting."
  exit 1
fi
echo "Ready. $WARN optional item(s) unavailable."
echo
echo "Writing and scoring a case set needs none of the connection items above:"
echo "the coding agent driving this is the generator and the judge by default."
exit 0
