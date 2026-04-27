#!/usr/bin/env bash
# Start the PolyMem engine HTTP server.
# Run once on your machine — collectors talk to this.

set -e

cd "$(dirname "$0")/.."

if ! command -v python3 &>/dev/null; then
  echo "python3 not found" >&2
  exit 1
fi

# Install dependencies if needed
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -e .
fi

export POLYMEM_PORT="${POLYMEM_PORT:-37700}"
# Default to claude_cli — uses the user's existing Claude Code subscription, no API key required.
# Override to openrouter/ollama/anthropic if you have a key configured.
export POLYMEM_PROVIDER="${POLYMEM_PROVIDER:-claude_cli}"
export POLYMEM_MODEL="${POLYMEM_MODEL:-claude-haiku-4-5-20251001}"

echo "▶ PolyMem engine starting on 127.0.0.1:${POLYMEM_PORT}"
echo "  provider: ${POLYMEM_PROVIDER}"
echo "  model:    ${POLYMEM_MODEL}"
echo "  data:     ${HOME}/.polymem/"
echo ""

exec .venv/bin/python -m engine.server
