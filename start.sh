#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start.sh — Launch all CX Product Holdings demo services
# ---------------------------------------------------------------------------
# Services:
#   8010  CRM SoR               (Systems of Record layer)
#   8011  CDC Assembly          (Business Context layer)
#   8012  Context Cache         (Business Context layer)
#   8013  ACG + Agent UI        (Agentic layer)
#   8014  Product Catalogue SoR (Systems of Record layer)
#   8015  Logging Service       (Cross-cutting)
#   8016  Offer Engine          (Business Context layer)
#   8017  Billing SoR           (Systems of Record layer)
#   8018  Action Broker         (Agentic layer — write governance)
#
# Usage:
#   ./start.sh           start all services and seed the cache
#   ./start.sh --no-seed start services but skip cache seeding
# ---------------------------------------------------------------------------
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="$SCRIPT_DIR/../../bin"         # shared venv
UVICORN="$VENV_BIN/uvicorn"
PYTHON="$VENV_BIN/python"
LOG_DIR="$SCRIPT_DIR/logs"
PID_DIR="$SCRIPT_DIR"

# Verify venv exists
if [ ! -x "$UVICORN" ]; then
  echo "ERROR: uvicorn not found at $UVICORN"
  echo "       Activate the venv and run: pip install -r requirements.txt"
  exit 1
fi

# Refuse to start if any service port is already in use
for port in 8010 8011 8012 8013 8014 8015 8016 8017 8018; do
  if lsof -i ":$port" -sTCP:LISTEN -t &>/dev/null; then
    echo "ERROR: port $port is already in use. Run ./stop.sh first."
    exit 1
  fi
done

mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# Each service runs inside a subshell that cd-s into its own folder.
# PYTHONPATH is set to the demo root so every service can import the shared
# modules (ontology, auth, commercial_rules, product_graph) that live there.
#
# Start order (dependency order):
#   Cache (8012)           — no upstream deps
#   Log   (8015)           — no upstream deps
#   Cat   (8014)           — no upstream deps (loads from YAML; calls CDC on PATCH)
#   Offer Engine (8016)    — depends on Catalogue (graph queries via /graph/compatible-candidates)
#   CDC   (8011)           — calls Cache + Log + Catalogue + Offer Engine
#   CRM SoR (8010)         — calls CDC + Log; emits startup events for all seed records
#   Billing SoR (8017)     — calls CDC + Log; emits startup events for all seed records
#   Action Broker (8018)   — calls Billing + Catalogue + Log; pure write governance
#   ACG   (8013)           — calls Cache + Log only (pure reader; no assembly path)
#
# Cache seeding is event-driven: CRM and Billing emit crm.customer.updated /
# billing.subscription.updated at startup → CDC assembles → both cache tiers
# populated. Run `python seed/seed.py` after startup to verify.
# ---------------------------------------------------------------------------
echo "Starting services..."

(cd "$SCRIPT_DIR/context_cache" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" cache_app:app --host 0.0.0.0 --port 8012 --log-level warning) \
  > "$LOG_DIR/cache.log" 2>&1 &
echo $! > "$PID_DIR/.pid_cache"
echo "  [8012] Context Cache         started (PID $(cat "$PID_DIR/.pid_cache"))"

(cd "$SCRIPT_DIR/log" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" log_app:app --host 0.0.0.0 --port 8015 --log-level warning) \
  > "$LOG_DIR/log.log" 2>&1 &
echo $! > "$PID_DIR/.pid_log"
echo "  [8015] Logging Service       started (PID $(cat "$PID_DIR/.pid_log"))"

(cd "$SCRIPT_DIR/product" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" catalogue_app:app --host 0.0.0.0 --port 8014 --log-level warning) \
  > "$LOG_DIR/catalogue.log" 2>&1 &
echo $! > "$PID_DIR/.pid_catalogue"
echo "  [8014] Product Catalogue SoR started (PID $(cat "$PID_DIR/.pid_catalogue"))"

(cd "$SCRIPT_DIR/offer_engine" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" offer_engine_app:app --host 0.0.0.0 --port 8016 --log-level warning) \
  > "$LOG_DIR/offer_engine.log" 2>&1 &
echo $! > "$PID_DIR/.pid_offer_engine"
echo "  [8016] Offer Engine          started (PID $(cat "$PID_DIR/.pid_offer_engine"))"

(cd "$SCRIPT_DIR/cdc" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" cdc_app:app --host 0.0.0.0 --port 8011 --log-level warning) \
  > "$LOG_DIR/cdc.log" 2>&1 &
echo $! > "$PID_DIR/.pid_cdc"
echo "  [8011] CDC Assembly          started (PID $(cat "$PID_DIR/.pid_cdc"))"

(cd "$SCRIPT_DIR/crm" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" crm_app:app --host 0.0.0.0 --port 8010 --log-level warning) \
  > "$LOG_DIR/crm.log" 2>&1 &
echo $! > "$PID_DIR/.pid_crm"
echo "  [8010] CRM SoR               started (PID $(cat "$PID_DIR/.pid_crm"))"

(cd "$SCRIPT_DIR/billing" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" billing_app:app --host 0.0.0.0 --port 8017 --log-level warning) \
  > "$LOG_DIR/billing.log" 2>&1 &
echo $! > "$PID_DIR/.pid_billing"
echo "  [8017] Billing SoR           started (PID $(cat "$PID_DIR/.pid_billing"))"

(cd "$SCRIPT_DIR/action_broker" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" action_broker_app:app --host 0.0.0.0 --port 8018 --log-level warning) \
  > "$LOG_DIR/action_broker.log" 2>&1 &
echo $! > "$PID_DIR/.pid_action_broker"
echo "  [8018] Action Broker         started (PID $(cat "$PID_DIR/.pid_action_broker"))"

(cd "$SCRIPT_DIR/acg" && PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/ontology:$SCRIPT_DIR/rules" \
  "$UVICORN" acg_app:app --host 0.0.0.0 --port 8013 --log-level warning) \
  > "$LOG_DIR/acg.log" 2>&1 &
echo $! > "$PID_DIR/.pid_acg"
echo "  [8013] ACG + Agent UI        started (PID $(cat "$PID_DIR/.pid_acg"))"

echo ""
echo "Services ready:"
echo "  Agent UI               ->  http://localhost:8013"
echo "  ACG API docs           ->  http://localhost:8013/docs"
echo "  Action Broker docs     ->  http://localhost:8018/docs"
echo "  Offer Engine docs      ->  http://localhost:8016/docs"
echo "  CRM SoR API docs       ->  http://localhost:8010/docs"
echo "  CDC API docs           ->  http://localhost:8011/docs"
echo "  Cache API docs         ->  http://localhost:8012/docs"
echo "  Product Catalogue docs ->  http://localhost:8014/docs"
echo "  Billing SoR docs       ->  http://localhost:8017/docs"
echo "  Logging Service        ->  http://localhost:8015/docs"
echo ""
echo "Logs:  $LOG_DIR/"
echo "Run ./stop.sh to shut down all services."
