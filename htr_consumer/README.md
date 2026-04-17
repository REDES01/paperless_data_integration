# HTR Preprocessing Consumer (Phase 2)

Long-lived Kafka consumer that closes the loop from Paperless upload → HTR review queue.

## What it does

On every `paperless.uploads` event it:

1. Calls the **region slicer** (vendored from `region_slicer/`) to:
   - fetch the document from Paperless via REST API
   - convert pages to images (PDFs via poppler, single-page for JPGs/PNGs)
   - detect candidate handwritten regions
   - upload page and region PNGs to MinIO
   - pull Tesseract text from Paperless's document metadata (no re-OCR)
2. Calls **serving's `/predict/htr`** for every detected region using the documented contract (`htr_input_sample.json`).
3. Calls `SlicerResult.merge_text(htr_outputs)` to build the combined `merged_text`.
4. Writes everything to the data-stack Postgres:
   - `documents` — one row, upserted on `paperless_doc_id` (Phase 6 bridge column)
   - `document_pages` — one row per page, with aggregated HTR text/confidence/flagged
   - `handwritten_regions` — one row per detected region, with HTR output per region

After this runs, the HTR review page (Phase 1) immediately shows the newly uploaded document for any page that was flagged by serving.

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Python 3.12 + poppler + deps; vendors slicer |
| `requirements.txt` | kafka-python-ng, psycopg, requests, pdf2image, Pillow, numpy, minio |
| `db.py` | psycopg 3 helpers: connection, upserts, writes |
| `processor.py` | Per-event pipeline: slice → HTR → merge → write |
| `consumer.py` | Kafka subscribe loop + graceful shutdown |
| `compose.yml` | Service definition on `paperless_ml_net` |

## Failure policy (Apr 20 milestone)

- **Processing error** → log, commit offset, move on
- **Individual HTR call error** → write empty output, mark region as flagged for review, continue with other regions
- **Broker unavailable on startup** → retry every 5s forever (Redpanda may start slightly after the consumer)
- **Poison message (bad JSON)** → log and skip, commit offset

No dead-letter queue yet. Can be added in a later milestone without changing the event-handling code.

## Required env vars

- `PAPERLESS_TOKEN` — Paperless REST API token (must exist; the consumer exits if unset)

All other env vars have sensible defaults that match the integration compose files. See `compose.yml` for the full list.

## Running

```bash
# From paperless_data_integration/
export PAPERLESS_TOKEN=<token>

# Apply the Phase 2 migration (add paperless_doc_id column to documents table)
cat seed/phase2_add_paperless_doc_id.sql \
  | docker exec -i postgres psql -U user -d paperless

# Build + start the consumer
docker compose -f htr_consumer/compose.yml up -d --build

# Watch it process events
docker compose -f htr_consumer/compose.yml logs -f
```

## Verifying it works

1. Upload a document via the Paperless UI (triggers Phase 4's producer, event lands in Redpanda).
2. Watch the consumer log: you should see `recv offset=... paperless_doc_id=...` followed by slicer output and HTR calls.
3. Open the HTR review page (`/ml/htr-review`): any flagged pages from the new upload appear immediately.
4. Check Postgres:
   ```bash
   docker exec postgres psql -U user -d paperless -c \
     "SELECT d.paperless_doc_id, d.filename, COUNT(r.id) AS regions
      FROM documents d
      LEFT JOIN document_pages p ON p.document_id = d.id
      LEFT JOIN handwritten_regions r ON r.page_id = p.id
      WHERE d.paperless_doc_id IS NOT NULL
      GROUP BY d.id ORDER BY d.uploaded_at DESC;"
   ```
