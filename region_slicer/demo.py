"""
Demo script — run the region slicer against a real Paperless document.

Usage:
  # From inside the Docker container on paperless_ml_net:
  python demo.py --doc-id 1

  # Or with explicit connection details:
  python demo.py --doc-id 1 \
    --paperless-url http://paperless-webserver-1:8000 \
    --paperless-token abc123 \
    --minio-endpoint minio:9000

  # Process ALL documents in Paperless:
  python demo.py --all

  # Dry run: detect regions but don't upload to MinIO:
  python demo.py --doc-id 1 --dry-run
"""

import argparse
import io
import json
import logging
import sys

import requests
from PIL import Image

from detector import detect_regions, crop_region

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def dry_run(args):
    """Detect regions without uploading — useful for quick testing."""
    headers = {}
    if args.paperless_token:
        headers["Authorization"] = f"Token {args.paperless_token}"

    base = args.paperless_url.rstrip("/")

    # Get doc metadata
    meta = requests.get(f"{base}/api/documents/{args.doc_id}/", headers=headers, timeout=10).json()
    print(f"Document: {meta.get('title', 'unknown')} (id={args.doc_id})")
    tesseract_text = meta.get("content", "") or ""
    print(f"Tesseract OCR text: {len(tesseract_text)} chars")
    if args.print_ocr:
        print("  " + "-" * 60)
        preview = tesseract_text[:500] + ("..." if len(tesseract_text) > 500 else "")
        for line in preview.split("\n"):
            print(f"  {line}")
        print("  " + "-" * 60)

    # Download file
    resp = requests.get(f"{base}/api/documents/{args.doc_id}/download/", headers=headers, timeout=60)
    file_bytes = resp.content
    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    print(f"File: {len(file_bytes)} bytes ({content_type})")

    # Convert to page images (handles both PDF and image files)
    ct = content_type.lower()
    if ct.startswith("image/") or not (ct == "application/pdf" or file_bytes[:5] == b"%PDF-"):
        try:
            from PIL import Image as _Img
            img = _Img.open(io.BytesIO(file_bytes)).convert("RGB")
            pages = [img]
            print(f"Opened as image: {img.width}x{img.height}")
        except Exception:
            from pdf2image import convert_from_bytes
            pages = convert_from_bytes(file_bytes, dpi=args.dpi)
            print(f"Pages: {len(pages)}")
    else:
        from pdf2image import convert_from_bytes
        pages = convert_from_bytes(file_bytes, dpi=args.dpi)
        print(f"Pages: {len(pages)}")

    total_regions = 0
    for page_num, page_image in enumerate(pages, start=1):
        regions = detect_regions(page_image)
        total_regions += len(regions)
        print(f"\n  Page {page_num} ({page_image.width}x{page_image.height}): {len(regions)} regions")
        for idx, region in enumerate(regions):
            bbox = region["bbox"]
            print(f"    Region {idx}: bbox={bbox}  size={region['width']}x{region['height']}")

    print(f"\nTotal: {total_regions} regions across {len(pages)} pages")
    return total_regions


def full_run(args):
    """Full pipeline: detect + crop + upload to MinIO."""
    from slicer import RegionSlicer

    slicer = RegionSlicer(
        paperless_url=args.paperless_url,
        paperless_token=args.paperless_token,
        minio_endpoint=args.minio_endpoint,
        minio_access_key=args.minio_access_key,
        minio_secret_key=args.minio_secret_key,
        dpi=args.dpi,
    )

    if args.all:
        # Fetch all document IDs from Paperless
        headers = {}
        if args.paperless_token:
            headers["Authorization"] = f"Token {args.paperless_token}"
        base = args.paperless_url.rstrip("/")
        resp = requests.get(f"{base}/api/documents/?page_size=1000", headers=headers, timeout=10).json()
        doc_ids = [doc["id"] for doc in resp.get("results", [])]
        print(f"Found {len(doc_ids)} documents in Paperless")
        if not doc_ids:
            print("No documents to process.")
            return

        for doc_id in doc_ids:
            result = slicer.process_document(doc_id)
            print(f"  {result.summary()}")
            for r in result.regions:
                print(f"    p{r.page_number} r{r.region_index}: {r.crop_s3_url}")
    else:
        result = slicer.process_document(args.doc_id)
        print(f"\n{result.summary()}")
        print(f"\nRegions:")
        for r in result.regions:
            print(f"  Page {r.page_number}, Region {r.region_index}:")
            print(f"    ID:       {r.region_id}")
            print(f"    Bbox:     {r.bbox}")
            print(f"    Size:     {r.width}x{r.height}")
            print(f"    MinIO:    {r.crop_s3_url}")

        # Merge demo (shows what merged_text will look like when the consumer
        # wires HTR outputs in; for now we use placeholder transcriptions)
        if args.demo_merge and result.regions:
            print(f"\n--- merge_text() demo (Tesseract + placeholder HTR) ---")
            placeholder_htr = [
                f"[handwriting for region {r.region_index}]"
                for r in result.regions
            ]
            merged = result.merge_text(placeholder_htr)
            print(merged[:800] + ("...\n[truncated]" if len(merged) > 800 else ""))
            print(f"merged length: {len(merged)} chars")

        # Print JSON for piping
        print(f"\nJSON output:")
        output = {
            "paperless_doc_id": result.paperless_doc_id,
            "title": result.title,
            "total_pages": result.total_pages,
            "num_regions": len(result.regions),
            "regions": [
                {
                    "region_id": r.region_id,
                    "page_number": r.page_number,
                    "region_index": r.region_index,
                    "bbox": r.bbox,
                    "width": r.width,
                    "height": r.height,
                    "crop_s3_url": r.crop_s3_url,
                }
                for r in result.regions
            ],
        }
        print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Region slicer demo")
    parser.add_argument("--doc-id", type=int, default=1, help="Paperless document ID")
    parser.add_argument("--all", action="store_true", help="Process all documents")
    parser.add_argument("--dry-run", action="store_true", help="Detect only, no MinIO upload")
    parser.add_argument("--print-ocr", action="store_true", help="Print Tesseract OCR content from Paperless")
    parser.add_argument("--demo-merge", action="store_true", help="After slicing, demonstrate merge_text() with fake HTR outputs")
    parser.add_argument("--dpi", type=int, default=200, help="PDF rendering DPI")
    parser.add_argument("--paperless-url", default="http://paperless-webserver-1:8000")
    parser.add_argument("--paperless-token", default="")
    parser.add_argument("--minio-endpoint", default="minio:9000")
    parser.add_argument("--minio-access-key", default="admin")
    parser.add_argument("--minio-secret-key", default="paperless_minio")
    args = parser.parse_args()

    if args.dry_run:
        dry_run(args)
    else:
        full_run(args)


if __name__ == "__main__":
    main()
