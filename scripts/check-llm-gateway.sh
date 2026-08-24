#!/usr/bin/env bash
set -euo pipefail

base="${SPONSORFLOW_LLM_GATEWAY_URL:-}"
base="${base%/}"
attempts="${SPONSORFLOW_GATEWAY_READINESS_ATTEMPTS:-45}"

if [ -z "$base" ]; then
  echo "gateway provider selected but SPONSORFLOW_LLM_GATEWAY_URL is empty" >&2
  exit 1
fi
if ! [[ "$attempts" =~ ^[1-9][0-9]*$ ]]; then
  echo "SPONSORFLOW_GATEWAY_READINESS_ATTEMPTS must be a positive integer" >&2
  exit 1
fi

for ((attempt=1; attempt<=attempts; attempt++)); do
  if [ -n "${SPONSORFLOW_LLM_API_KEY:-}" ]; then
    status="$(printf 'header = "Authorization: Bearer %s"\n' "$SPONSORFLOW_LLM_API_KEY" | \
      curl --config - -sS -m 3 -o /dev/null -w '%{http_code}' "$base/v1/models" 2>/dev/null || true)"
  else
    status="$(curl -sS -m 3 -o /dev/null -w '%{http_code}' "$base/v1/models" 2>/dev/null || true)"
  fi
  case "$status" in 2??) exit 0 ;; esac
  [ "$attempt" -lt "$attempts" ] && sleep 1
done

echo "selected LLM gateway did not become ready" >&2
exit 1
