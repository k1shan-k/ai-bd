#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env.production}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.production.yml")

cd "$ROOT"
PYTHONPATH=backend python3 scripts/production-preflight.py \
  --env-file "$ENV_FILE" --require-docker --check-dns

if [ -n "$("${COMPOSE[@]}" ps -q postgres 2>/dev/null || true)" ]; then
  "$ROOT/scripts/production-backup.sh" "$ENV_FILE"
fi

"${COMPOSE[@]}" build --pull
"${COMPOSE[@]}" up -d --wait

DOMAIN=$(sed -n 's/^SPONSORFLOW_DOMAIN=//p' "$ENV_FILE")
if [ -z "$DOMAIN" ]; then
  echo "SPONSORFLOW_DOMAIN is missing" >&2
  exit 1
fi

for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 10 \
    --output /dev/null "https://$DOMAIN/login"; then
    printf 'SponsorFlow is ready: https://%s/login\n' "$DOMAIN"
    exit 0
  fi
  [ "$attempt" -eq 30 ] || sleep 10
done

echo "HTTPS readiness failed; inspect: ${COMPOSE[*]} ps && ${COMPOSE[*]} logs" >&2
exit 1
