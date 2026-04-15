#!/usr/bin/env bash
# Idempotently create the shared docker network used by both compose stacks.

set -euo pipefail

NETWORK="paperless_ml_net"

if docker network ls --format '{{.Name}}' | grep -q "^${NETWORK}$"; then
    echo "Network '${NETWORK}' already exists."
else
    docker network create --driver bridge "${NETWORK}" >/dev/null
    echo "Created network '${NETWORK}'."
fi

docker network inspect "${NETWORK}" --format '{{.Name}} ({{.Driver}})'
