#!/usr/bin/env bash
# Start the PolyMem Codex watcher in the background.
#
# Codex CLI does not expose hooks — it writes JSONL session rollouts to
# ~/.codex/sessions/. This watcher polls them and incrementally streams
# new tool calls + user/assistant messages to PolyMem.
#
# Usage:
#   ./start-codex-watcher.sh                # foreground (Ctrl+C to stop)
#   ./start-codex-watcher.sh --background   # nohup background
#
# Env (optional):
#   POLYMEM_CODEX_POLL_MS       — poll interval ms (default 30000)
#   POLYMEM_CODEX_BACKFILL_DAYS — only ingest sessions younger than N days (default 1)

set -e

POLYMEM_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCHER="${POLYMEM_ROOT}/collectors/codex/watcher.ts"
LOG="${HOME}/.polymem/codex-watcher.log"

if ! command -v bun &>/dev/null; then
  echo "bun not found. Install: curl -fsSL https://bun.sh/install | bash" >&2
  exit 1
fi

# Engine reachable?
if ! curl -s --max-time 3 "${POLYMEM_BASE_URL:-http://127.0.0.1:37700}/v1/health" >/dev/null; then
  echo "PolyMem engine not reachable. Start with:"
  echo "  ${POLYMEM_ROOT}/scripts/start-engine.sh"
  exit 1
fi

if [ "${1:-}" = "--background" ]; then
  # Idempotent: kill existing watcher first
  pkill -f "$WATCHER" 2>/dev/null || true
  mkdir -p "$(dirname "$LOG")"
  nohup bun "$WATCHER" >> "$LOG" 2>&1 &
  echo "✓ Codex watcher started in background (PID $!)"
  echo "  Log: $LOG"
  echo "  Stop: pkill -f codex/watcher.ts"
else
  echo "▶ Codex watcher (foreground)"
  echo "  Log goes to stdout — Ctrl+C to stop"
  echo ""
  exec bun "$WATCHER"
fi
