# Sample Documents

Small sample files for testing the HTR + semantic search pipeline end-to-end. Committed into the repo so the Chameleon VM can upload them into Paperless without any external downloads.

## Files

| File | Type | Purpose |
|---|---|---|
| `sample_budget_memo.pdf` | 2-page PDF | Generated budget memo + lease agreement with both printed text (should be picked up by Paperless's Tesseract) and simulated handwritten annotations (should be detected by the region slicer) |
| `sample_scan.jpeg` | Single-page JPG | A scanned-image document for testing the slicer's image-file path |

## How the deployment notebook uses them

After Paperless is up and the API token is generated, the notebook uploads both files via `curl POST /api/documents/post_document/`. Paperless processes each upload (Tesseract OCR, thumbnail generation, classification) and assigns integer IDs. The slicer then fetches those documents by ID and runs region detection.
