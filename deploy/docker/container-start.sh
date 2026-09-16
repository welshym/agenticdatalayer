#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# container-start.sh — run ALL Business Context Layer (BCL) services in one
# container (PID 1). Mirrors start.sh but uses the image's uvicorn on PATH,
# no venv, no lsof pre-check. Inter-service calls use the hardcoded
# http://localhost:<port> URLs, which resolve inside this single container.
# ---------------------------------------------------------------------------
set -e
ROOT="/app"
export PYTHONPATH="$ROOT:$ROOT/ontology:$ROOT/rules"
export PYTHONUNBUFFERED=1

start() { # <dir> <module> <port> <name>
  ( cd "$ROOT/$1" && uvicorn "$2:app" --host 0.0.0.0 --port "$3" --log-level warning ) &
  echo "  [$3] $4 started (pid $!)"
}

echo "Starting BCL services..."
start context_cache  cache_app          8012 "Context Cache"
start log            log_app            8015 "Logging Service"
start product        catalogue_app      8014 "Product Catalogue SoR"
start offer_engine   offer_engine_app   8016 "Offer Engine"
start cdc            cdc_app            8011 "CDC Assembly"
start crm            crm_app            8010 "CRM SoR"
start billing        billing_app        8017 "Billing SoR"
start action_broker  action_broker_app  8018 "Action Broker"
start acg            acg_app            8013 "ACG + Agent UI"

# Event-driven seeding happens automatically (CRM/Billing emit startup events
# -> CDC assembles -> cache populated). Run seed/seed.py too, best-effort.
sleep 10
( cd "$ROOT" && python seed/seed.py ) || echo "seed.py skipped/failed (non-fatal)"

echo "All BCL services started. Container will stay alive."
# Keep PID 1 alive as long as any service runs.
wait -n
echo "A service exited; keeping container up for the rest."
wait
