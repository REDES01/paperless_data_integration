#!/usr/bin/env bash
# Tear down the entire stack. Leaves volumes in place by default; pass
# --volumes as the first arg to also drop data.
#
# Usage:
#   bash scripts/down_all.sh
#   bash scripts/down_all.sh --volumes        # also wipe postgres, minio, etc.

set -uo pipefail       # no -e: keep tearing down even if one compose is already gone

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

V=""
if [ "${1:-}" = "--volumes" ]; then
    V="-v"
    echo "NOTE: --volumes given; persistent data (postgres, minio, qdrant, mlflow, grafana) will be DELETED."
fi

step() { echo ""; echo "── $* ──"; }

step "htr_consumer"
docker compose -p htr_consumer -f htr_consumer/compose.yml down ${V} || true

step "drift_monitor"
docker compose -p drift_monitor -f drift_monitor/compose.yml down ${V} || true

step "qdrant_indexer"
docker compose -p qdrant_indexer -f qdrant_indexer/compose.yml down ${V} || true

step "ml_gateway"
docker compose -p ml_gateway -f ml_gateway/compose.yml down ${V} || true

step "observability"
docker compose -p observability -f observability/compose.yml down ${V} || true

step "paperless"
docker compose -p paperless \
    -f paperless/docker-compose.yml \
    -f overrides/paperless.override.yml down ${V} || true

step "paperless_data"
if [ -d "${REPO_ROOT}/../paperless_data" ]; then
    docker compose -p paperless_data \
        -f ../paperless_data/docker/docker-compose.yaml \
        -f overrides/paperless_data.override.yml down ${V} || true
fi

echo ""
echo "done."
docker ps --format "table {{.Names}}\t{{.Status}}"
