#!/usr/bin/env bash
# Bring up the Paperless stack with the Phase 0 network override applied.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE="${REPO_ROOT}/paperless/docker-compose.yml"
OVERRIDE="${REPO_ROOT}/overrides/paperless.override.yml"

echo "Bringing up Paperless stack on paperless_ml_net..."
docker compose -p paperless -f "${COMPOSE}" -f "${OVERRIDE}" up -d
docker compose -p paperless -f "${COMPOSE}" -f "${OVERRIDE}" ps
