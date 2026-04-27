#!/usr/bin/env bash
# Print today's PolyMem daily report to stdout.
#
# Usage:
#   daily-report.sh                       # today, all clients
#   daily-report.sh 2026-04-20            # specific date
#   daily-report.sh 2026-04-22 polymem    # specific date + project filter
#   daily-report.sh "" cses-client        # today + project filter
#
# Options via env:
#   POLYMEM_BASE=http://host:port    (default http://127.0.0.1:37700)

DATE="${1:-}"
PROJECT="${2:-}"
CLIENT="${3:-}"
BASE="${POLYMEM_BASE:-http://127.0.0.1:37700}"

Q=""
[ -n "$DATE" ]    && Q="${Q}&date=${DATE}"
[ -n "$PROJECT" ] && Q="${Q}&project=${PROJECT}"
[ -n "$CLIENT" ]  && Q="${Q}&client=${CLIENT}"
Q="${Q#&}"

URL="${BASE}/v1/report"
[ -n "$Q" ] && URL="${URL}?${Q}"

RESP=$(curl -s --max-time 10 "$URL")
if [ -z "$RESP" ]; then
  echo "ERROR: Engine not reachable at $BASE" >&2
  exit 1
fi

echo "$RESP" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.stderr.write('Unexpected response:\n')
    sys.stderr.write(sys.stdin.read() + '\n')
    sys.exit(1)
if 'report' in d:
    print(d['report'])
else:
    print(d.get('detail', d))
"
