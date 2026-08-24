#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env.production}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.production.yml")
BACKUP_DIR=$(sed -n 's/^SPONSORFLOW_BACKUP_DIR=//p' "$ENV_FILE")
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
case "$BACKUP_DIR" in
  /*) ;;
  *) BACKUP_DIR="$ROOT/${BACKUP_DIR#./}" ;;
esac

umask 077
mkdir -p "$BACKUP_DIR"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
final="$BACKUP_DIR/sponsorflow-$timestamp.dump"
temporary="$final.tmp"
trap 'rm -f "$temporary"' EXIT

"${COMPOSE[@]}" exec -T postgres \
  pg_dump --username sponsorflow --dbname sponsorflow --format custom > "$temporary"
test -s "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$final"
sha256sum "$final" > "$final.sha256"
chmod 600 "$final.sha256"
printf 'Created owner-only PostgreSQL dump (not encrypted): %s\n' "$final"
printf 'Move it immediately to approved encrypted off-host storage and run a restore drill.\n'
