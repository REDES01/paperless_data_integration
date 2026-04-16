# Region Slicer

Detects and crops handwritten regions from Paperless-ngx documents. This is the core image-processing component that Phase 2 (the HTR preprocessing service) wraps with a Kafka consumer.

## What it does

1. **Fetches a PDF** from Paperless via its REST API (`GET /api/documents/{id}/download/`)
2. **Converts each page** to a PIL image at 200 DPI using `pdf2image` (poppler)
3. **Detects handwritten regions** using horizontal ink-density projection (vendored from the data team's `htr_features.py`)
4. **Crops each region** and uploads the crop PNG to MinIO at `s3://paperless-images/documents/{doc_id}/regions/p{page}_r{idx}.png`
5. **Returns structured metadata** — region IDs, bounding boxes, sizes, and MinIO URLs

## Files

| File | Purpose |
|---|---|
| `detector.py` | Region detection algorithm (binarize → horizontal projection → vertical trim → filter by size) |
| `slicer.py` | Orchestrator: fetch PDF → pages → detect → crop → upload to MinIO |
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

This catches handwritten annotations (dark ink strokes) while ignoring printed text (which is typically lighter on scanned documents). It's a heuristic, not a learned model — the data design doc notes it would be replaced by a detection model in production.
