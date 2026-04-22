#!/usr/bin/env bash
#
# Deploy a registered MLflow model to ml_gateway.
#
# Usage:
#   bash scripts/deploy_model.sh [version|latest]
#
# If no argument is given, deploys the latest registered version of "htr".
# Writes the MLflow URI to the shared volume `ml_gateway_model_registry`
# (mounted at /models in both ml_gateway and rollback_ctrl), then sends
# SIGHUP to ml_gateway so it reloads.
#
# This is the "promote" action. The rollback controller does the reverse
# automatically when an alert fires.

set -euo pipefail

MODEL_NAME="${MODEL_NAME:-htr}"
MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:5000}"
VERSION="${1:-latest}"

if [ "$VERSION" = "latest" ]; then
    # Ask MLflow for the highest registered version number
    VERSION=$(curl -sf "${MLFLOW_TRACKING_URI}/api/2.0/mlflow/registered-models/get?name=${MODEL_NAME}" \
        | python3 -c "import sys,json; \
d=json.load(sys.stdin); \
vs=d.get('registered_model',{}).get('latest_versions',[]); \
print(max(int(v['version']) for v in vs) if vs else 0)")
    if [ "$VERSION" = "0" ]; then
        echo "ERROR: no registered versions for model '${MODEL_NAME}'" >&2
        exit 1
    fi
fi

URI="models:/${MODEL_NAME}/${VERSION}"
echo "Deploying: ${URI}"

# Write to the shared volume. The ml_gateway container reads this file on
# startup and on SIGHUP.
docker run --rm \
    -v ml_gateway_model_registry:/models \
    alpine:3 sh -c "echo '${URI}' > /models/current_htr.txt && cat /models/current_htr.txt"

# Kick ml_gateway to reload
docker kill --signal=HUP ml_gateway 2>/dev/null || {
    echo "WARN: SIGHUP to ml_gateway failed — it may not be running yet."
    echo "      The URI is written; ml_gateway will pick it up on next start."
}

echo "Deployed ${URI}"
