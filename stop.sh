#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# stop.sh — Shut down all CX Product Holdings demo services
# ---------------------------------------------------------------------------
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR"
stopped=0

stop_service() {
  local name="$1"
  local pid_file="$PID_DIR/.pid_$name"

  if [ -f "$pid_file" ]; then
    pid=$(cat "$pid_file")
    if kill "$pid" 2>/dev/null; then
      echo "  Stopped $name (PID $pid)"
      ((stopped++))
    else
      echo "  $name not running (stale PID $pid)"
    fi
    rm -f "$pid_file"
  else
    echo "  $name — no PID file found (may not be running)"
  fi
}

echo "Stopping services..."
stop_service action_broker
stop_service acg
stop_service offer_engine
stop_service billing
stop_service catalogue
stop_service crm
stop_service cdc
stop_service log
stop_service cache
stop_service opa

# Belt-and-braces: catch any stragglers not tracked by PID files
for svc in acg action_broker offer_engine billing catalogue crm cdc log cache; do
  pkill -f "uvicorn ${svc}_app:app" 2>/dev/null || true
done
pkill -f "opa run --server" 2>/dev/null || true

echo ""
if [ "$stopped" -gt 0 ]; then
  echo "$stopped service(s) stopped."
else
  echo "No running services found."
fi
