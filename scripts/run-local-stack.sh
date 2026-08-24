#!/usr/bin/env bash
# Start the SponsorFlow stack for pre-production testing with fake providers.
# API stays on loopback; only the CRM may be reachable from outside this host.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="${SPONSORFLOW_LOG_DIR:-/tmp/sfl}"
cd "$ROOT"
mkdir -p "$LOGS"

set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [ "${SPONSORFLOW_PROVIDER_MODE:-fake}" != "fake" ]; then
  echo "refusing to start: this script is for fake-provider testing only" >&2
  exit 1
fi
case "${SPONSORFLOW_API_HOST:-127.0.0.1}" in
  127.0.0.1|localhost|::1) ;;
  *) echo "refusing to start: SPONSORFLOW_API_HOST must be loopback" >&2; exit 1 ;;
esac

started_pids=()
cleanup_partial_start() {
  local pid
  trap - ERR INT TERM
  for ((index=${#started_pids[@]}-1; index>=0; index--)); do
    pid="${started_pids[$index]}"
    kill -TERM -- "-$pid" >/dev/null 2>&1 || kill -TERM "$pid" >/dev/null 2>&1 || true
  done
  exit 1
}
trap cleanup_partial_start ERR INT TERM

start_detached() {
  local name="$1" log="$2"
  shift 2
  setsid nohup "$@" > "$log" 2>&1 &
  local pid=$!
  started_pids+=("$pid")
  printf '%s %s\n' "$name" "$pid" > "$LOGS/$name.pid"
}

wait_for_url() {
  local url="$1" label="$2"
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 -o /dev/null "$url" 2>/dev/null; then return 0; fi
    sleep 1
  done
  echo "$label did not become ready" >&2
  return 1
}

if [ "${SPONSORFLOW_LLM_PROVIDER:-fake}" = "gateway" ]; then
  "$ROOT/scripts/check-llm-gateway.sh"
fi

(
  cd "$ROOT/frontend"
  NODE_OPTIONS="${SPONSORFLOW_WEB_BUILD_NODE_OPTIONS:---max-old-space-size=1024}" npm run build
)

start_detached api "$LOGS/api.log" env PYTHONPATH=backend \
  .venv/bin/uvicorn app.main:app --host "${SPONSORFLOW_API_HOST:-127.0.0.1}" --port 8000
wait_for_url http://127.0.0.1:8000/health API

start_detached worker "$LOGS/worker.log" env PYTHONPATH=backend \
  .venv/bin/python -m app.worker --interval 15

start_detached web "$LOGS/web.log" env \
  HOSTNAME="${SPONSORFLOW_WEB_HOST:-127.0.0.1}" PORT=3000 \
  NODE_OPTIONS="${SPONSORFLOW_WEB_NODE_OPTIONS:---max-old-space-size=768}" \
  node frontend/scripts/start-standalone.mjs
wait_for_url http://127.0.0.1:3000/login CRM

trap - ERR INT TERM
echo "started; logs in $LOGS"
