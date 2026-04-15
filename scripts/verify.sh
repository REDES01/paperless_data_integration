#!/usr/bin/env bash
# Verify that cross-stack DNS works from inside paperless-webserver-1.

set -uo pipefail

WEBSERVER="paperless-webserver-1"
TARGETS=(postgres minio redpanda qdrant)

echo "=== Phase 0 verification ==="

# Network exists
if ! docker network ls --format '{{.Name}}' | grep -q '^paperless_ml_net$'; then
    echo "FAIL: paperless_ml_net network does not exist." >&2
    exit 1
fi
echo "OK: paperless_ml_net exists."

# Paperless webserver is running
if ! docker ps --format '{{.Names}}' | grep -q "^${WEBSERVER}$"; then
    echo "FAIL: ${WEBSERVER} is not running." >&2
    exit 1
fi

echo ""
echo "Containers on paperless_ml_net:"
docker network inspect paperless_ml_net \
    --format '{{range .Containers}}  {{.Name}}{{println}}{{end}}'

echo ""
echo "DNS checks from ${WEBSERVER}:"
failed=0
for target in "${TARGETS[@]}"; do
    ip="$(docker exec "${WEBSERVER}" getent hosts "${target}" 2>/dev/null | awk '{print $1}' || true)"
    if [[ -z "${ip}" ]]; then
        printf "  %-12s -> NOT RESOLVED\n" "${target}"
        failed=$((failed + 1))
    else
        printf "  %-12s -> %s\n" "${target}" "${ip}"
    fi
done

echo ""
if [[ ${failed} -eq 0 ]]; then
    echo "Phase 0 OK: cross-stack DNS works."
    exit 0
else
    echo "Phase 0 INCOMPLETE: ${failed} target(s) unreachable."
    echo "If the data stack isn't running yet, run scripts/up_paperless_data.sh first."
    exit 2
fi
