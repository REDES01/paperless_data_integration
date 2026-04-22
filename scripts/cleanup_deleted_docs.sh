#!/usr/bin/env bash
# Cleanup orphaned ML-side data for Paperless documents that are no longer
# active in Paperless. "Orphaned" means:
#
#     ML Postgres has a `documents` row with paperless_doc_id=N and
#     deleted_at IS NULL, but Paperless's own DB has no active document
#     with id=N (either hard-deleted or soft-deleted in Paperless).
#
# Needed because the paperless_ml delete-signal handlers only fire going
# forward; docs deleted BEFORE that code shipped have live ML-side rows
# and live Qdrant vectors.
#
# What this script does:
#   1. List active paperless doc ids from Paperless's db
#   2. Find ML documents rows whose paperless_doc_id is NOT in that set
#      AND whose deleted_at is still NULL
#   3. UPDATE documents SET deleted_at = NOW() for those
#   4. DELETE matching Qdrant points via the collection's REST API
#      (called from inside paperless-webserver-1, which has `curl` and
#      is on paperless_ml_net so it can reach qdrant:6333)
#
# Idempotent: safe to run again. Already-cleaned rows are filtered out.
#
# Run on the VM:
#   cd ~/paperless_data_integration
#   bash scripts/cleanup_deleted_docs.sh

set -euo pipefail

PAPERLESS_DB_CONTAINER="${PAPERLESS_DB_CONTAINER:-paperless-db-1}"
ML_DB_CONTAINER="${ML_DB_CONTAINER:-postgres}"
WEBSERVER_CONTAINER="${WEBSERVER_CONTAINER:-paperless-webserver-1}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-document_chunks}"

# ─── 1. Active paperless doc ids ───────────────────────────────────────
echo "━━━ 1. Active paperless doc ids (from Paperless DB) ━━━"
active_ids=$(sg docker -c "docker exec ${PAPERLESS_DB_CONTAINER} psql -U paperless -d paperless -t -A -c \
    \"SELECT id FROM documents_document;\"" | tr '\n' ',' | sed 's/,$//')
active_count=$(echo "$active_ids" | tr ',' '\n' | grep -c . || true)
echo "count: $active_count"
echo "ids:   ${active_ids:-<none>}"

# ─── 2. Find orphan ML rows ────────────────────────────────────────────
echo ""
echo "━━━ 2. Find orphan ML rows (not in active set, deleted_at IS NULL) ━━━"
if [ -z "$active_ids" ]; then
    orphan_sql="SELECT paperless_doc_id FROM documents WHERE deleted_at IS NULL AND paperless_doc_id IS NOT NULL;"
else
    orphan_sql="SELECT paperless_doc_id FROM documents WHERE deleted_at IS NULL AND paperless_doc_id IS NOT NULL AND paperless_doc_id NOT IN ($active_ids);"
fi
orphan_ids=$(sg docker -c "docker exec ${ML_DB_CONTAINER} psql -U user -d paperless -t -A -c \"$orphan_sql\"" | tr '\n' ' ')

# Trim whitespace
orphan_ids=$(echo "$orphan_ids" | xargs)
if [ -z "$orphan_ids" ]; then
    echo "orphan paperless_doc_ids: <none>"
    echo ""
    echo "Nothing to clean up. Exiting."
    exit 0
fi
echo "orphan paperless_doc_ids: $orphan_ids"

# ─── 3. Mark orphans deleted in ML Postgres ────────────────────────────
echo ""
echo "━━━ 3. Soft-delete ML documents ━━━"
ids_csv=$(echo "$orphan_ids" | tr ' ' ',')
sg docker -c "docker exec ${ML_DB_CONTAINER} psql -U user -d paperless -c \
    \"UPDATE documents SET deleted_at = NOW() WHERE paperless_doc_id IN ($ids_csv) AND deleted_at IS NULL;\""

# ─── 4. Delete Qdrant points for each orphan id ────────────────────────
echo ""
echo "━━━ 4. Delete Qdrant points ━━━"
for id in $orphan_ids; do
    payload="{\"filter\":{\"must\":[{\"key\":\"paperless_doc_id\",\"match\":{\"value\":$id}}]}}"
    result=$(sg docker -c "docker exec ${WEBSERVER_CONTAINER} curl -s -X POST \
        http://qdrant:6333/collections/${QDRANT_COLLECTION}/points/delete \
        -H 'Content-Type: application/json' \
        -d '$payload'" || echo '{"error":"exec failed"}')
    status=$(echo "$result" | grep -oE '"status":"[^"]*"' | head -1 || echo '?')
    echo "  id=$id $status"
done

# ─── 5. Report counts after ────────────────────────────────────────────
echo ""
echo "━━━ 5. Post-cleanup ML docs counts ━━━"
sg docker -c "docker exec ${ML_DB_CONTAINER} psql -U user -d paperless -c \"
SELECT
    COUNT(*) FILTER (WHERE deleted_at IS NULL) AS active_ml_docs,
    COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) AS deleted_ml_docs
FROM documents;\""

echo ""
echo "━━━ 6. Qdrant collection info ━━━"
sg docker -c "docker exec ${WEBSERVER_CONTAINER} curl -s \
    http://qdrant:6333/collections/${QDRANT_COLLECTION} | \
    grep -oE '\"points_count\":[0-9]+' | head -1"

echo ""
echo "Done."
