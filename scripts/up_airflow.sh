#!/usr/bin/env bash
# Bring up Airflow (postgres + init + webserver + scheduler) on paperless_ml_net.
#
# Prereqs:
#   - paperless_ml_net docker network exists (scripts/create_network.sh)
#   - htr_trainer:latest image is built (triggered by training/compose.yml)
#
# First boot builds paperless-airflow:2.10.4 (extends apache/airflow:2.10.4 with
# apache-airflow-providers-docker pre-installed). Takes ~30s. Subsequent boots
# are instant (image cached).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE="${REPO_ROOT}/airflow/compose.yml"

if [[ ! -f "${COMPOSE}" ]]; then
    echo "ERROR: ${COMPOSE} not found" >&2
    exit 1
fi

# Airflow needs ./logs and ./plugins dirs to exist
mkdir -p "${REPO_ROOT}/airflow/dags"
mkdir -p "${REPO_ROOT}/airflow/logs"
mkdir -p "${REPO_ROOT}/airflow/plugins"

echo "Ensuring htr_trainer:latest image exists (required by DockerOperator)..."
if ! docker image inspect htr_trainer:latest > /dev/null 2>&1; then
    echo "  htr_trainer not built — building now (~5 min on first build)"
    docker compose -p training -f "${REPO_ROOT}/training/compose.yml" build baseline_stage1
else
    echo "  htr_trainer:latest present"
fi

echo ""
echo "Building paperless-airflow:2.10.4 (apache/airflow + docker provider)..."
docker compose -p airflow -f "${COMPOSE}" build

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
echo "Trigger:  click Play icon in the UI"
