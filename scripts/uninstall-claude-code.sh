#!/usr/bin/env bash
# Remove PolyMem hook entries from ~/.claude/settings.json while preserving
# all other hooks the user had configured.

set -e

SETTINGS="${HOME}/.claude/settings.json"

if [ ! -f "$SETTINGS" ]; then
  echo "Settings file not found: $SETTINGS"
  exit 0
fi

if ! command -v jq &>/dev/null; then
  echo "Please install jq: brew install jq" >&2
  exit 1
fi

BACKUP="${SETTINGS}.bak.uninstall.$(date +%Y%m%d-%H%M%S)"
cp "$SETTINGS" "$BACKUP"
echo "✓ Backup: $BACKUP"

TMP=$(mktemp)
# Remove any hook block whose commands mention "polymem". Also drop
# top-level event-type keys that become empty afterwards. Remove
# POLYMEM_ROOT from env.
jq '
   .hooks = (
     (.hooks // {})
     | to_entries
     | map(
         .value = (.value | map(
           .hooks = (.hooks | map(select(.command | test("polymem") | not)))
           | select((.hooks | length) > 0)
         ))
       )
     | map(select((.value | length) > 0))
     | from_entries
   )
   | if .env then .env = (.env | del(.POLYMEM_ROOT)) else . end
   ' "$SETTINGS" > "$TMP"
mv "$TMP" "$SETTINGS"

echo "✓ PolyMem hooks removed."
echo ""
echo "Remaining hook events:"
jq -r '.hooks | to_entries[] | "  \(.key): \(.value | length) hook-block(s)"' "$SETTINGS" 2>/dev/null || echo "  (none)"
