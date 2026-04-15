#!/usr/bin/env bash
# Stop both stacks. Leaves the shared network in place.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE="$(cd "${REPO_ROOT}/.." && pwd)"

PAPERLESS_COMPOSE="${REPO_ROOT}/paperless/docker-compose.yml"
PAPERLESS_OVERRIDE="${REPO_ROOT}/overrides/paperless.override.yml"
DATA_COMPOSE="${WORKSPACE}/paperless_data/docker/docker-compose.yaml"
DATA_OVERRIDE="${REPO_ROOT}/overrides/paperless_data.override.yml"

echo "Stopping Paperless stack..."
docker compose -p paperless -f "${PAPERLESS_COMPOSE}" -f "${PAPERLESS_OVERRIDE}" down || true

echo "Stopping data stack..."
docker compose -p paperless_data -f "${DATA_COMPOSE}" -f "${DATA_OVERRIDE}" down || true

echo "Done. Network paperless_ml_net left intact. Use 'docker network rm paperless_ml_net' to remove it."
