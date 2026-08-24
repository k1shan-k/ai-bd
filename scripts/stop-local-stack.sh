#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="${SPONSORFLOW_LOG_DIR:-/tmp/sfl}"

declare -A trusted_pids=()

expected_command() {
  case "$1" in
    web) printf '%s' 'next-server|start-standalone\.mjs|standalone/server\.js' ;;
    worker) printf '%s' 'app\.worker' ;;
    api) printf '%s' 'uvicorn.*app\.main:app' ;;
  esac
}

for name in web worker api; do
  file="$LOGS/$name.pid"
  [ -f "$file" ] || continue
  read -r recorded_name pid < "$file" || true
  if [ "$recorded_name" != "$name" ] || ! [[ "${pid:-}" =~ ^[0-9]+$ ]]; then
    echo "ignoring invalid PID file: $file" >&2
    continue
  fi
  if [ ! -r "/proc/$pid/cmdline" ]; then
    rm -f "$file"
    continue
  fi
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
  process_cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  if ! grep -Eq "$(expected_command "$name")" <<< "$command_line"; then
    echo "refusing unexpected PID $pid for $name" >&2
    continue
  fi
  case "$process_cwd" in
    "$ROOT"|"$ROOT/frontend") ;;
    *) echo "refusing PID $pid outside SponsorFlow workspace" >&2; continue ;;
  esac
  trusted_pids["$name"]="$pid"
  kill -TERM -- "-$pid" >/dev/null 2>&1 || kill -TERM "$pid" >/dev/null 2>&1 || true
done

for _ in $(seq 1 20); do
  running=0
  for pid in "${trusted_pids[@]}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then running=1; fi
  done
  [ "$running" -eq 0 ] && break
  sleep 0.5
done

for name in "${!trusted_pids[@]}"; do
  pid="${trusted_pids[$name]}"
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL -- "-$pid" >/dev/null 2>&1 || kill -KILL "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$LOGS/$name.pid"
done

echo "stopped local SponsorFlow processes"
