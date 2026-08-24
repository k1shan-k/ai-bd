#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env.production}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.production.yml")
DOMAIN=$(sed -n 's/^SPONSORFLOW_DOMAIN=//p' "$ENV_FILE")

"${COMPOSE[@]}" ps
"${COMPOSE[@]}" exec -T api python -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"
"${COMPOSE[@]}" exec -T worker python -m app.worker --healthcheck
curl --fail --silent --show-error --max-time 10 --output /dev/null "https://$DOMAIN/login"
printf 'production_status=healthy domain=%s\n' "$DOMAIN"
