-- Phase 2 migration: add paperless_doc_id bridge column to the ML `documents` table.
--
-- This column stores the integer document ID from Paperless-ngx's own database,
-- so the ML pipeline can reference Paperless documents without duplicating rows.
-- It's UNIQUE (one ML document per Paperless document) and nullable for
-- backward compatibility with any existing rows (e.g. the Phase 1 demo seed).
--
-- Idempotent: safe to run repeatedly.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS paperless_doc_id BIGINT;

-- Unique constraint (can't use ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS; do it defensively)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'documents_paperless_doc_id_key'
    ) THEN
        ALTER TABLE documents
            ADD CONSTRAINT documents_paperless_doc_id_key UNIQUE (paperless_doc_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_documents_paperless_doc_id ON documents(paperless_doc_id);
