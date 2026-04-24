#!/usr/bin/env bash
# Bring up Airflow (postgres + init + webserver + scheduler) on paperless_ml_net.
#
# Prereqs:
#   - paperless_ml_net docker network exists (scripts/create_network.sh)
#   - htr_trainer:latest image is built (via training/compose.yml)
#   - htr_batch:latest image is built (via paperless_data/batch_pipeline/)
#
# First boot takes ~60s: 30s to build paperless-airflow:2.10.4 (pre-installs
# apache-airflow-providers-docker so the startup _PIP_ADDITIONAL_REQUIREMENTS
# flakiness is avoided) plus ~30s for Airflow init. Subsequent boots are
# near-instant (images cached).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE="$(cd "${REPO_ROOT}/.." && pwd)"

COMPOSE="${REPO_ROOT}/airflow/compose.yml"
BATCH_PIPELINE_DIR="${WORKSPACE}/paperless_data/batch_pipeline"

if [[ ! -f "${COMPOSE}" ]]; then
    echo "ERROR: ${COMPOSE} not found" >&2
    exit 1
fi

# Airflow needs these dirs to exist
mkdir -p "${REPO_ROOT}/airflow/dags"
mkdir -p "${REPO_ROOT}/airflow/logs"
mkdir -p "${REPO_ROOT}/airflow/plugins"

# ── Ensure htr_trainer:latest (DAG task 'finetune_combined_stage1') ────────
echo "Ensuring htr_trainer:latest image exists..."
if ! docker image inspect htr_trainer:latest > /dev/null 2>&1; then
    echo "  htr_trainer not built — building now (~5 min on first build)"
    docker compose -p training -f "${REPO_ROOT}/training/compose.yml" build baseline_stage1
else
    echo "  htr_trainer:latest present"
fi

# ── Ensure htr_batch:latest (DAG task 'build_snapshot') ────────────────────
# The batch_pipeline lives in the paperless_data sibling repo. Its Dockerfile
# copies quality.py + batch_htr.py + batch_retrieval.py into /app. The DAG
# calls only batch_htr.py — batch_retrieval.py is ignored.
echo "Ensuring htr_batch:latest image exists..."
if ! docker image inspect htr_batch:latest > /dev/null 2>&1; then
    if [[ ! -f "${BATCH_PIPELINE_DIR}/Dockerfile" ]]; then
        echo "ERROR: ${BATCH_PIPELINE_DIR}/Dockerfile not found — is paperless_data cloned as a sibling repo?" >&2
        exit 1
    fi
    echo "  htr_batch not built — building now (~1 min)"
    docker build -t htr_batch:latest "${BATCH_PIPELINE_DIR}"
else
    echo "  htr_batch:latest present"
fi

# ── Build paperless-airflow:2.10.4 ─────────────────────────────────────────
echo ""
echo "Building paperless-airflow:2.10.4 (apache/airflow + docker provider)..."
docker compose -p airflow -f "${COMPOSE}" build

# ── Boot Airflow ───────────────────────────────────────────────────────────
echo ""
echo "Bringing up Airflow (airflow-postgres, airflow-init, airflow-webserver, airflow-scheduler)..."
docker compose -p airflow -f "${COMPOSE}" up -d airflow-postgres
for i in $(seq 1 20); do
    if docker compose -p airflow -f "${COMPOSE}" ps airflow-postgres 2>&1 | grep -q "healthy"; then
        echo "  airflow-postgres healthy"
        break
    fi
    sleep 2
done

# airflow-init is a one-shot — block on completion
docker compose -p airflow -f "${COMPOSE}" up airflow-init

docker compose -p airflow -f "${COMPOSE}" up -d airflow-webserver airflow-scheduler

echo ""
echo "Airflow services:"
docker compose -p airflow -f "${COMPOSE}" ps

echo ""
echo "Waiting for webserver health..."
for i in $(seq 1 30); do
    if curl -sf -m 3 http://localhost:8080/health >/dev/null 2>&1; then
        echo "  webserver ready (took ~$((i*3))s)"
        break
    fi
    sleep 3
done

echo ""
echo "Airflow ready. UI: http://$(hostname -I | awk '{print $1}'):8080"
echo "Login:            admin / admin"
echo ""
echo "DAG:    htr_retraining"
echo "Tasks:  build_snapshot -> finetune_combined_stage1 -> notify_result"
echo "Trigger: click Play icon in the UI"
