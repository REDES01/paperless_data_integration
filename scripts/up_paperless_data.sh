#!/usr/bin/env bash
# Bring up the data stack with the Phase 0 network override applied.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE="$(cd "${REPO_ROOT}/.." && pwd)"

COMPOSE="${WORKSPACE}/paperless_data/docker/docker-compose.yaml"
OVERRIDE="${REPO_ROOT}/overrides/paperless_data.override.yml"

if [[ ! -f "${COMPOSE}" ]]; then
    echo "ERROR: ${COMPOSE} not found. Is paperless_data cloned as a sibling repo?" >&2
    exit 1
fi

echo "Bringing up data stack on paperless_ml_net..."
docker compose -p paperless_data -f "${COMPOSE}" -f "${OVERRIDE}" up -d
docker compose -p paperless_data -f "${COMPOSE}" -f "${OVERRIDE}" ps
