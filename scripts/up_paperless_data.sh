#!/usr/bin/env bash
# Bring up the data stack with the Phase 0 network override applied.
# Also ensures the paperless-images MinIO bucket is anonymous-readable
# (required for the htr-review UI to load region crop images via <img> tags).

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

# ── Ensure paperless-images bucket is anonymous-readable ─────────────────
# The minio-init container's entrypoint sets this too, but that only runs
# on first container creation. Re-running this block on every bringup is
# idempotent (mc returns "already set" the second time) and costs ~3s.
#
# Required so the /ml/htr-review Angular component can load crop images
# straight from MinIO via <img src="http://<vm>:9000/paperless-images/...">.
echo "Ensuring paperless-images bucket is anonymous-readable..."
docker run --rm --network paperless_ml_net \
    --entrypoint /bin/sh \
    minio/mc:RELEASE.2024-11-17T19-35-25Z \
    -c "mc alias set local http://minio:9000 admin paperless_minio > /dev/null && \
        mc anonymous set download local/paperless-images && \
        mc anonymous list   local/paperless-images"

echo "Data stack ready."
