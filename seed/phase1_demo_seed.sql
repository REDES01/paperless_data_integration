-- Phase 1 demo seed: 3 fake handwritten regions across 2 fake documents.
-- These let the HTR review page show real rows the moment Phase 1 is deployed,
-- before Phase 2 (the real HTR preprocessing service) exists.
--
-- Idempotent: uses fixed UUIDs and ON CONFLICT DO NOTHING.
-- Drop later by deleting rows with filename LIKE 'demo_%' (their UUIDs are fixed too).

INSERT INTO documents (id, filename, source, page_count, uploaded_at, tesseract_text, htr_text, merged_text, is_test_doc)
VALUES
  ('11111111-1111-4111-8111-111111111111',
   'demo_invoice_acme_2026_03.pdf', 'test', 1,
   NOW() - INTERVAL '6 hours',
   'INVOICE Acme Corp Date: 2026-03-15', NULL, NULL, FALSE),
  ('22222222-2222-4222-8222-222222222222',
   'demo_lease_350_jay_st.pdf',     'test', 1,
   NOW() - INTERVAL '1 day',
   'LEASE AGREEMENT 350 Jay St Term:',  NULL, NULL, FALSE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO document_pages (id, document_id, page_number, image_s3_url, tesseract_text, htr_text, htr_confidence, htr_flagged)
VALUES
  ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
   '11111111-1111-4111-8111-111111111111', 1,
   's3://paperless-images/seed/invoice_p1.png',
   'INVOICE Acme Corp Date: 2026-03-15', 'Total: $5,OOO Approved by J. Smith',
   0.54, TRUE),
  ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2',
   '22222222-2222-4222-8222-222222222222', 1,
   's3://paperless-images/seed/lease_p1.png',
   'LEASE AGREEMENT 350 Jay St Term:', 'Term ends June 3O, 2027',
   0.71, TRUE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO handwritten_regions (id, page_id, crop_s3_url, htr_output, htr_confidence)
VALUES
  ('cccccccc-cccc-4ccc-8ccc-cccccccccc01',
   'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
   's3://paperless-images/seed/invoice_p1_r1.png',
   'Total: $5,OOO',
   0.54),
  ('cccccccc-cccc-4ccc-8ccc-cccccccccc02',
   'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
   's3://paperless-images/seed/invoice_p1_r2.png',
   'Approved by J. Smith',
   0.92),
  ('cccccccc-cccc-4ccc-8ccc-cccccccccc03',
   'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2',
   's3://paperless-images/seed/lease_p1_r1.png',
   'Term ends June 3O, 2027',
   0.71)
ON CONFLICT (id) DO NOTHING;
