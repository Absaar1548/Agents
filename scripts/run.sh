#!/usr/bin/env bash
# Start all BRD agent services: Neo4j (container), Phoenix, FastAPI backend,
# Streamlit.
#
# Closing the terminal or pressing Ctrl+C cleanly stops all of them (trap on
# INT/TERM/HUP/EXIT kills child PIDs and runs `docker compose down`).
#
# Usage (from anywhere):
#   /path/to/brd_agent/scripts/run.sh
set -uo pipefail

# Always operate from the project root, even though this script lives in scripts/.
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
cd "$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(pwd)"
VENV="$PROJECT_ROOT/.venv"
LOG_DIR="$PROJECT_ROOT/.logs"
mkdir -p "$LOG_DIR"

PHOENIX_HTTP_PORT=6006
PHOENIX_GRPC_PORT=4347   # 4317 and 4327 are held by stale procs in this env
BACKEND_PORT=8010
FRONTEND_PORT=8501
NEO4J_BOLT_PORT=7687
NEO4J_HTTP_PORT=7474
INFRA_DIR="$PROJECT_ROOT/infra"

PIDS=()
NEO4J_STARTED_BY_US=0

# ---------- helpers ----------
log()  { printf '\033[1;36m[run.sh]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[run.sh]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[run.sh]\033[0m %s\n' "$*" >&2; }

free_port() {
  # Kill anything currently bound to the given port.
  local port=$1
  local pids
  pids=$(ss -tlnpH "sport = :$port" 2>/dev/null \
          | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)
  if [[ -n "${pids:-}" ]]; then
    warn "port :$port held by pid(s) $pids — terminating"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    # Force-kill if still alive.
    pids=$(ss -tlnpH "sport = :$port" 2>/dev/null \
            | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)
    if [[ -n "${pids:-}" ]]; then
      # shellcheck disable=SC2086
      kill -9 $pids 2>/dev/null || true
      sleep 1
    fi
  fi
}

wait_for() {
  # Poll a URL until it returns 2xx or we time out.
  local url=$1
  local timeout=${2:-20}
  local i
  for ((i = 0; i < timeout * 2; i++)); do
    if curl -sf -o /dev/null "$url"; then return 0; fi
    sleep 0.5
  done
  return 1
}

cleanup() {
  # Idempotent: trap fires on EXIT after INT/TERM/HUP, so guard against
  # double-execution.
  if [[ "${CLEANED_UP:-0}" == "1" ]]; then return; fi
  CLEANED_UP=1
  echo
  log "shutting down — killing child pids: ${PIDS[*]:-(none)}"
  for pid in "${PIDS[@]:-}"; do
    [[ -z "$pid" ]] && continue
    kill "$pid" 2>/dev/null || true
  done
  # Give them a moment to exit gracefully, then SIGKILL stragglers.
  sleep 1
  for pid in "${PIDS[@]:-}"; do
    [[ -z "$pid" ]] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
  # Tear down Neo4j only if we started it.
  if [[ "$NEO4J_STARTED_BY_US" == "1" ]]; then
    log "stopping neo4j container..."
    (cd "$INFRA_DIR" && docker compose down --remove-orphans >/dev/null 2>&1) || true
  fi
  log "done."
}
trap cleanup INT TERM HUP EXIT

# ---------- pre-flight ----------
if [[ ! -x "$VENV/bin/python" ]]; then
  err "venv missing at $VENV — run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  err ".env missing at $PROJECT_ROOT/.env — copy from .env.example and fill in Azure creds"
  exit 1
fi

log "freeing ports: $PHOENIX_HTTP_PORT, $BACKEND_PORT, $FRONTEND_PORT"
free_port "$PHOENIX_HTTP_PORT"
free_port "$BACKEND_PORT"
free_port "$FRONTEND_PORT"

# ---------- 0. Neo4j (Docker container) ----------
if [[ -f "$INFRA_DIR/docker-compose.yml" ]] && command -v docker >/dev/null 2>&1; then
  if docker exec brd_agent_neo4j true >/dev/null 2>&1; then
    log "neo4j container already running — reusing"
  else
    log "starting neo4j container (compose up -d)..."
    if (cd "$INFRA_DIR" && docker compose up -d neo4j >/dev/null 2>&1); then
      NEO4J_STARTED_BY_US=1
    else
      warn "  docker compose up failed — backend will start without KG layer"
    fi
  fi
  if [[ "$NEO4J_STARTED_BY_US" == "1" ]]; then
    log "  waiting for neo4j bolt :$NEO4J_BOLT_PORT..."
    for i in {1..40}; do
      if docker exec brd_agent_neo4j wget -qO- http://localhost:7474 >/dev/null 2>&1; then
        log "  neo4j ready"
        break
      fi
      sleep 1.5
    done
  fi
else
  warn "docker / compose file missing — running without KG layer"
fi

# ---------- 1. Phoenix ----------
log "starting Phoenix (http :$PHOENIX_HTTP_PORT, grpc :$PHOENIX_GRPC_PORT) — log: $LOG_DIR/phoenix.log"
"$VENV/bin/phoenix" serve --grpc-port "$PHOENIX_GRPC_PORT" \
  > "$LOG_DIR/phoenix.log" 2>&1 &
PIDS+=($!)
log "  phoenix pid=${PIDS[-1]}"

if wait_for "http://localhost:$PHOENIX_HTTP_PORT/v1/projects" 30; then
  log "  phoenix ready"
else
  err "  phoenix failed to come up — check $LOG_DIR/phoenix.log"
  exit 1
fi

# ---------- 2. FastAPI backend ----------
log "starting FastAPI on :$BACKEND_PORT — log: $LOG_DIR/backend.log"
"$VENV/bin/uvicorn" backend.main:app --port "$BACKEND_PORT" --log-level info \
  > "$LOG_DIR/backend.log" 2>&1 &
PIDS+=($!)
log "  backend pid=${PIDS[-1]}"

if wait_for "http://localhost:$BACKEND_PORT/health" 30; then
  log "  backend ready"
else
  err "  backend failed to come up — check $LOG_DIR/backend.log"
  exit 1
fi

# ---------- 3. Streamlit frontend ----------
log "starting Streamlit on :$FRONTEND_PORT — log: $LOG_DIR/streamlit.log"
BACKEND_URL="http://localhost:$BACKEND_PORT" \
  "$VENV/bin/streamlit" run frontend/streamlit_app.py \
  --server.port "$FRONTEND_PORT" \
  --server.headless true \
  --browser.gatherUsageStats false \
  > "$LOG_DIR/streamlit.log" 2>&1 &
PIDS+=($!)
log "  streamlit pid=${PIDS[-1]}"

if wait_for "http://localhost:$FRONTEND_PORT/_stcore/health" 30; then
  log "  streamlit ready"
else
  err "  streamlit failed to come up — check $LOG_DIR/streamlit.log"
  exit 1
fi

# ---------- summary ----------
cat <<EOF

==========================================================
  BRD Agent — all services running
----------------------------------------------------------
  Streamlit UI    : http://localhost:$FRONTEND_PORT
  FastAPI backend : http://localhost:$BACKEND_PORT  (/health, /memory)
  Phoenix traces  : http://localhost:$PHOENIX_HTTP_PORT  (service.name=brd-agent)
  Neo4j browser   : http://localhost:$NEO4J_HTTP_PORT  (user: neo4j)
  Logs            : $LOG_DIR/{phoenix,backend,streamlit}.log

  Press Ctrl+C, or close this terminal, to stop everything.
==========================================================

EOF

# Block until any child exits or a signal arrives. The trap kills the rest.
wait -n 2>/dev/null || wait
