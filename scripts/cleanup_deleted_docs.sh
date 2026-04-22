#!/usr/bin/env bash
# Cleanup orphaned ML data for Paperless documents that were deleted before
# the paperless_ml delete-signal handlers were deployed.
#
# What "orphaned" means here:
#   ML Postgres has documents rows with paperless_doc_id=N AND deleted_at IS NULL,
#   but Paperless's own DB has no active document with id=N (it was deleted or
#   soft-deleted in Paperless).
#
# This script:
#   1. Pulls the set of active paperless_doc_ids from Paperless's DB.
#   2. For every ML documents row whose paperless_doc_id is NOT in that set:
#      - UPDATE documents SET deleted_at = NOW()
#      - DELETE all matching Qdrant points
#   3. Reports counts.
#
# Run on the VM:
#   bash scripts/cleanup_deleted_docs.sh
#
# Requires: `sg docker` access, postgres + ml_postgres + qdrant running.

set -euo pipefail

echo "━━━ 1. Active paperless doc ids (from Paperless DB) ━━━"
active_ids=$(sg docker -c "docker exec db psql -U paperless -d paperless -t -A -c \
    \"SELECT id FROM documents_document WHERE deleted_at IS NULL;\"" | tr '\n' ',' | sed 's/,$//')
echo "count: $(echo "$active_ids" | tr ',' '\n' | grep -c . || true)"

echo ""
echo "━━━ 2. Find orphan ML rows (paperless_doc_id not in active set, deleted_at IS NULL) ━━━"
# Build the SQL: ML documents where paperless_doc_id NOT IN active_ids AND not already marked deleted
if [ -z "$active_ids" ]; then
    # No active docs at all — every ML row is orphan.
    orphan_sql="SELECT paperless_doc_id FROM documents WHERE deleted_at IS NULL;"
else
    orphan_sql="SELECT paperless_doc_id FROM documents WHERE deleted_at IS NULL AND paperless_doc_id NOT IN ($active_ids);"
fi
orphan_ids=$(sg docker -c "docker exec postgres psql -U user -d paperless -t -A -c \"$orphan_sql\"" | tr '\n' ' ')
echo "orphan paperless_doc_ids: ${orphan_ids:-<none>}"

if [ -z "${orphan_ids// }" ]; then
    echo "nothing to clean up"
    exit 0
fi

echo ""
echo "━━━ 3. Mark ML documents deleted ━━━"
for id in $orphan_ids; do
    sg docker -c "docker exec postgres psql -U user -d paperless -c \
        \"UPDATE documents SET deleted_at = NOW() WHERE paperless_doc_id = $id AND deleted_at IS NULL;\""
done

echo ""
echo "━━━ 4. Delete Qdrant points ━━━"
for id in $orphan_ids; do
    echo "  deleting qdrant points for paperless_doc_id=$id"
    sg docker -c "docker exec qdrant sh -c 'wget -qO- --post-data=\"{\\\"filter\\\":{\\\"must\\\":[{\\\"key\\\":\\\"paperless_doc_id\\\",\\\"match\\\":{\\\"value\\\":$id}}]}}\" --header=\"Content-Type: application/json\" http://localhost:6333/collections/document_chunks/points/delete || echo qdrant-err'"
done

echo ""
echo "━━━ 5. Post-cleanup counts ━━━"
sg docker -c 'docker exec postgres psql -U user -d paperless -c "
SELECT
    COUNT(*) FILTER (WHERE deleted_at IS NULL) AS active_ml_docs,
    COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) AS deleted_ml_docs
FROM documents;"'

echo "done."
