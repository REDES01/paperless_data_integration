# Region Slicer

Detects and crops handwritten regions from Paperless-ngx documents. This is the core image-processing component that Phase 2 (the HTR preprocessing service) wraps with a Kafka consumer.

## What it does

1. **Fetches a PDF** from Paperless via its REST API (`GET /api/documents/{id}/download/`)
2. **Converts each page** to a PIL image at 200 DPI using `pdf2image` (poppler)
3. **Detects handwritten regions** using horizontal ink-density projection (vendored from the data team's `htr_features.py`)
4. **Filters out printed text** using Tesseract's archive-PDF word boxes — regions with >25% Tesseract word coverage are dropped before cropping (see `printed_filter.py`)
5. **Crops each surviving region** and uploads the crop PNG to MinIO at `s3://paperless-images/documents/{doc_id}/regions/p{page}_r{idx}.png`
6. **Returns structured metadata** — region IDs, bounding boxes, sizes, and MinIO URLs

## Files

| File | Purpose |
|---|---|
| `detector.py` | Region detection algorithm (binarize → horizontal projection → vertical trim → filter by size) |
| `printed_filter.py` | Tesseract-guided filter: reads word boxes from Paperless's archive PDF, drops regions that are already well-covered by Tesseract-recognized printed text |
| `slicer.py` | Orchestrator: fetch PDF → pages → detect → filter → crop → upload to MinIO |
| `demo.py` | CLI for testing against real Paperless documents |
| `compose.yml` | Run as a one-shot container on `paperless_ml_net` |
| `Dockerfile` | Python 3.12 + poppler-utils + deps |

## Usage

```bash
# Build the slicer image
docker compose -f region_slicer/compose.yml build

# Get a Paperless API token (one-time)
docker exec paperless-webserver-1 python manage.py shell -c \
  "from rest_framework.authtoken.models import Token; from django.contrib.auth.models import User; \
   t, _ = Token.objects.get_or_create(user=User.objects.first()); print(t.key)"

# Dry run — detect regions, print bounding boxes, no MinIO upload
docker compose -f region_slicer/compose.yml run --rm slicer \
  demo.py --doc-id 1 --dry-run --paperless-token <TOKEN>

# Full run — detect + crop + upload to MinIO
docker compose -f region_slicer/compose.yml run --rm slicer \
  demo.py --doc-id 1 --paperless-token <TOKEN>

# Process all documents in Paperless
docker compose -f region_slicer/compose.yml run --rm slicer \
  demo.py --all --paperless-token <TOKEN>
```

## Detection algorithm

The detector uses horizontal projection on a binarized grayscale image:

1. Convert page to grayscale, threshold at pixel value 180 → binary (ink vs background)
2. For each row, count the number of ink pixels
3. When the count exceeds 2% of the row width, mark the start of a region
4. When the count drops below 1%, mark the end
5. For each band of rows, compute vertical projection to find the left/right ink boundaries
6. Filter out regions smaller than 50×15 pixels
7. Add 5px padding around each crop

This catches every ink-dense band — handwritten OR printed. The printed-filter step below is what keeps only the handwritten ones.

## Printed-text filter (B1)

The ink-density detector flags any dense text band. Without the filter, printed text regions would be sent to TrOCR — which is trained on handwriting — producing garbage in the review UI. Tesseract has already read the printed text (Paperless runs it on every upload), so we reuse Tesseract's work to decide which regions to SKIP.

### How it works

1. Fetch Paperless's archive PDF (`GET /api/documents/{id}/download/?original=false`). This is the searchable PDF Paperless generates post-consumption: same visual as the original but with a Tesseract text layer embedded.
2. Use `pdfplumber` to read word-level bounding boxes from the archive's text layer.
3. Scale the boxes from PDF points to the pixel coordinates the slicer is working in.
4. For each candidate region, compute the fraction of its area covered by Tesseract word boxes.
5. If coverage ≥ 0.25, classify as **printed** and drop the region. Otherwise, keep for HTR.

### Threshold choice

Coverage at 0.25 was chosen from observations on mixed handwritten/printed scans:
- Printed line with good scan: coverage ~0.55-0.85
- Printed line with poor scan: ~0.20-0.50
- Handwritten line: ~0.00-0.15 (Tesseract typically outputs nothing for cursive)
- Mixed line (printed + handwritten note): ~0.25-0.40

0.25 catches the main failure mode (printed regions → TrOCR → garbage) while keeping mixed-content regions. Tunable via the `PRINTED_COVERAGE_THRESHOLD` constant in `printed_filter.py`.

### Fallback behavior

If the archive PDF is missing (some uploads skip archive generation) or `pdfplumber` fails to parse it, the filter returns all candidate regions unchanged — the slicer degrades to its original behavior. The log makes this explicit: "No archive PDF — printed-filter disabled".

## Text merging (Tesseract + HTR)

Per the data design document, `merged_text = Tesseract output + HTR transcriptions`. Tesseract is already run by Paperless on every upload, so the slicer pulls Paperless's OCR `content` via the REST API instead of running Tesseract a second time.

`SlicerResult` exposes:
- `tesseract_text` — the full printed-text OCR from Paperless
- `merge_text(htr_outputs)` — combines `tesseract_text` with a list of HTR transcriptions (one per detected region) in this format:
  ```
  <tesseract output>

  [HANDWRITTEN]
  <htr output region 0>
  <htr output region 1>
  ...
  ```

This is the string the Phase 2 consumer will write to `documents.merged_text` in the data-stack Postgres, and that the indexing service will chunk and upsert into Qdrant.

### Test it

```bash
# Show just Paperless's Tesseract output for a document
docker compose -f region_slicer/compose.yml run --rm slicer \
  demo.py --doc-id 1 --dry-run --print-ocr --paperless-token <TOKEN>

# After slicing, show what merged_text would look like (uses placeholder HTR outputs)
docker compose -f region_slicer/compose.yml run --rm slicer \
  demo.py --doc-id 1 --demo-merge --paperless-token <TOKEN>
```
