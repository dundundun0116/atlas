#!/usr/bin/env bash
# One-command local demo: backend + frontend. Ctrl-C stops both.
#
#   bash scripts/demo.sh            # dataset indexes under data/cache
#   bash scripts/demo.sh --ingest   # latest version built from uploaded docs (runs/ingest)
#
# Docker version: docker compose up --build
set -euo pipefail
cd "$(dirname "$0")/.."

SOURCE="dataset"
if [ "${1:-}" = "--ingest" ]; then SOURCE="ingest"; fi

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"
API_PID=""
WEB_PID=""

cleanup() {
  echo
  echo "[demo] stopping"
  if [ -n "$API_PID" ]; then kill "$API_PID" 2>/dev/null || true; fi
  if [ -n "$WEB_PID" ]; then kill "$WEB_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

echo "[demo] backend on :$API_PORT (index-source=$SOURCE)"
.venv/bin/python scripts/serve_api.py --port "$API_PORT" --index-source "$SOURCE" \
  > runs/logs/demo_api.log 2>&1 &
API_PID=$!

for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
  echo "[demo] backend failed to start; see runs/logs/demo_api.log"
  exit 1
fi

echo "[demo] frontend on :$WEB_PORT"
( cd web && npm run dev -- --port "$WEB_PORT" --strictPort > ../runs/logs/demo_web.log 2>&1 ) &
WEB_PID=$!

cat <<EOF

[demo] ready
  web  http://127.0.0.1:$WEB_PORT
  api  http://127.0.0.1:$API_PORT/health
  logs runs/logs/demo_api.log / runs/logs/demo_web.log
Ctrl-C to stop.
EOF

wait "$API_PID" "$WEB_PID" 2>/dev/null || true
