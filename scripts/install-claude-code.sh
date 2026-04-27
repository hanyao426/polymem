#!/usr/bin/env bash
# Install PolyMem hooks for Claude Code (user-level, additive).
#
# Safe semantics:
#   - Backs up ~/.claude/settings.json before any change
#   - APPENDS to existing hook arrays (does NOT replace them)
#   - If user already has a PostToolUse hook, PolyMem's is added alongside
#   - Idempotent: running twice won't duplicate (checks for existing polymem entries)
#
# Usage:
#   ./install-claude-code.sh                  # collect-only (no SessionStart injection)
#   ./install-claude-code.sh --hybrid         # collect + lightweight $PMEM index injection (recommended)
#   ./install-claude-code.sh --with-injection # collect + full $PMEM block injection (heaviest)

set -e

POLYMEM_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${HOME}/.claude/settings.json"

HOOKS_SRC="${POLYMEM_ROOT}/collectors/claude-code/hooks.json"
case "${1:-}" in
  --hybrid)
    HOOKS_SRC="${POLYMEM_ROOT}/collectors/claude-code/hooks-hybrid.json"
    echo "▶ Using HYBRID variant (SessionStart injects lightweight \$PMEM index; details via MCP)"
    ;;
  --with-injection)
    HOOKS_SRC="${POLYMEM_ROOT}/collectors/claude-code/hooks-with-injection.json"
    echo "▶ Using INJECTION variant (SessionStart injects FULL \$PMEM context)"
    ;;
  "")
    echo "▶ Using COLLECT-ONLY variant (no SessionStart injection)"
    ;;
  *)
    echo "Unknown flag: $1" >&2
    echo "Valid: --hybrid | --with-injection | (no flag)" >&2
    exit 1
    ;;
esac

if [ ! -f "$HOOKS_SRC" ]; then
  echo "Hooks source not found: $HOOKS_SRC" >&2
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "Please install jq: brew install jq" >&2
  exit 1
fi

if ! command -v bun &>/dev/null; then
  echo "Please install bun: curl -fsSL https://bun.sh/install | bash" >&2
  exit 1
fi

mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

# ─── Backup ────────────────────────────────────────────────────────────────
BACKUP="${SETTINGS}.bak.$(date +%Y%m%d-%H%M%S)"
cp "$SETTINGS" "$BACKUP"
echo "✓ Backup: $BACKUP"

# ─── Idempotency check ─────────────────────────────────────────────────────
if jq -e '.. | .command? // empty | select(test("polymem"))' "$SETTINGS" >/dev/null 2>&1; then
  echo "⚠ PolyMem hooks already installed. Remove them first with:"
  echo "   ./scripts/uninstall-claude-code.sh"
  exit 1
fi

# ─── Additive merge ────────────────────────────────────────────────────────
# For each event type in the new hooks, APPEND its array to the existing
# array at that key (do not overwrite). Also record POLYMEM_ROOT in env.
TMP=$(mktemp)
jq --slurpfile add "$HOOKS_SRC" \
   --arg root "$POLYMEM_ROOT" \
   '
   .hooks = (
     reduce (($add[0].hooks | to_entries[])) as $e
       (.hooks // {};
        .[$e.key] = ((.[$e.key] // []) + $e.value))
   )
   | .env = ((.env // {}) + {"POLYMEM_ROOT": $root})
   ' "$SETTINGS" > "$TMP"
mv "$TMP" "$SETTINGS"

echo "✓ Hooks merged additively into $SETTINGS"
echo ""
echo "Summary of hook events affected:"
jq -r '.hooks | to_entries[] | "  \(.key): \(.value | length) total hook-block(s)"' "$SETTINGS"
echo ""
echo "POLYMEM_ROOT = $POLYMEM_ROOT"
echo ""
echo "Next: start the engine with:"
echo "  ${POLYMEM_ROOT}/scripts/start-engine.sh"
echo ""
echo "Uninstall anytime:"
echo "  ${POLYMEM_ROOT}/scripts/uninstall-claude-code.sh"
echo "  (or restore manually: cp $BACKUP $SETTINGS)"
