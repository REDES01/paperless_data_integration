#!/usr/bin/env bash
# Bring up the complete integrated stack in dependency order.
#
# Usage (from paperless_data_integration/):
#   PAPERLESS_TOKEN=xxx bash scripts/up_all.sh
#
# Assumes:
#   - Docker installed, user in docker group (or run under sg docker)
#   - Shared network paperless_ml_net already created by create_network.sh
#   - paperless_data repo cloned at ../paperless_data
#   - scripts/up_paperless_data.sh and scripts/up_paperless.sh exist and work

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

step() {
    echo ""
    echo "══════════════════════════════════════════════════════════"
    echo "  $*"
    echo "══════════════════════════════════════════════════════════"
}

wait_for_healthy() {
    # wait_for_healthy <container-name> <max-seconds>
    local name="$1"
    local max="${2:-180}"
    local elapsed=0
    while [ "${elapsed}" -lt "${max}" ]; do
        local state
        state="$(docker inspect --format '{{.State.Health.Status}}' "${name}" 2>/dev/null || echo "missing")"
        if [ "${state}" = "healthy" ]; then
            echo "  ${name}: healthy after ${elapsed}s"
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "  ${name}: NOT healthy after ${max}s (state=${state})" >&2
    docker logs --tail 20 "${name}" 2>&1 | sed 's/^/    /' >&2 || true
    return 1
}

# ── 1. Shared network ──────────────────────────
docker volume create ml_gateway_model_registry >/dev/null 2>&1 || true

step "1/8  Creating shared network (paperless_ml_net)"
bash scripts/create_network.sh

# ── 2. Data stack (postgres, minio, redpanda, qdrant) ──
step "2/8  Data stack (postgres + minio + redpanda + qdrant)"
bash scripts/up_paperless_data.sh
wait_for_healthy postgres 120
wait_for_healthy minio 120
wait_for_healthy redpanda 120
# qdrant has no healthcheck — just sleep a couple seconds
sleep 3

# ── 3. Paperless stack ────────────────────────
step "3/8  Paperless stack (paperless-web, redis, paperless-db)"
bash scripts/up_paperless.sh

# Paperless's healthcheck is internal to its own compose; just wait.
echo "  waiting 45s for Paperless to initialize..."
sleep 45

# ── 4. Observability (prometheus, grafana, alertmanager, mlflow, rollback_ctrl) ──
step "4/8  Observability (prometheus + grafana + alertmanager + mlflow + rollback)"
docker compose -p observability -f observability/compose.yml up -d --build
wait_for_healthy prometheus 120 || true
wait_for_healthy rollback_ctrl 120 || true

# ── 5. ML gateway (HTR + search) ───────────────
step "5/8  ML gateway (TrOCR + mpnet)"
docker compose -p ml_gateway -f ml_gateway/compose.yml up -d --build
# First boot loads both models — can take ~90s
wait_for_healthy ml_gateway 240 || true

# ── 6. Qdrant indexer ──────────────────────────
step "6/8  Qdrant indexer"
docker compose -p qdrant_indexer -f qdrant_indexer/compose.yml up -d --build
sleep 5

# ── 7. Drift monitor ───────────────────────────
step "7/8  Drift monitor"
# compose.yml reads from the built detector in MinIO; if it's not there yet,
# the service will crash-loop. That's expected until step 9 of the notebook
# runs build_drift_reference.py.
docker compose -p drift_monitor -f drift_monitor/compose.yml up -d --build || true

# ── 8. HTR consumer ────────────────────────────
step "8/8  HTR consumer"
if [ -z "${PAPERLESS_TOKEN:-}" ]; then
    echo "  WARNING: PAPERLESS_TOKEN not set; consumer will fail to start."
    echo "  Set PAPERLESS_TOKEN and re-run:"
    echo "    docker compose -p htr_consumer -f htr_consumer/compose.yml up -d --build"
else
    PAPERLESS_TOKEN="${PAPERLESS_TOKEN}" \
        docker compose -p htr_consumer -f htr_consumer/compose.yml up -d --build
fi

# ── Summary ────────────────────────────────────
step "Stack status"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
