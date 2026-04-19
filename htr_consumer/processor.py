"""
Per-event processing: takes a `paperless.uploads` event and runs the full HTR
preprocessing pipeline against it.

  1. Slice the Paperless document into handwritten region crops (via slicer.py,
     which also pulls Tesseract output from Paperless's REST API).
  2. For each region, POST to the configured HTR endpoint (see HTR_ENDPOINT env
     var; defaults to /predict/htr) with the documented schema, get back
     {htr_output, htr_confidence, htr_flagged}.
  3. Call SlicerResult.merge_text() to build the merged_text string.
  4. Write rows into the data-stack Postgres:
       - documents           (one row, upserted on paperless_doc_id)
       - document_pages      (one row per page)
       - handwritten_regions (one row per detected region)

On failure we log loudly and re-raise, letting the consumer decide whether to
commit the Kafka offset. For the Apr 20 milestone we commit regardless (the
"fail loud, move on" policy documented in the integration plan); a DLQ can be
added later without changing this module.
"""

import logging
import os
import time

import requests

from slicer import RegionSlicer, SlicerResult
import db

log = logging.getLogger(__name__)


FASTAPI_URL   = os.environ.get("FASTAPI_URL", "http://fastapi_server:8000").rstrip("/")
HTR_ENDPOINT  = "/" + os.environ.get("HTR_ENDPOINT", "/predict/htr").lstrip("/")
HTR_TIMEOUT   = int(os.environ.get("HTR_TIMEOUT_SECONDS", "30"))


def _call_htr(
    document_id: str,
    page_id: str,
    region_id: str,
    crop_s3_url: str,
    image_width: int,
    image_height: int,
    source: str,
    uploaded_at: str,
) -> dict:
    """
    POST to serving's HTR endpoint (FASTAPI_URL + HTR_ENDPOINT; defaults to
    /predict/htr) with the data-team's documented contract
    (htr_input_sample.json). Returns the parsed response dict:
      {region_id, htr_output, htr_confidence, htr_flagged,
       model_version, inference_time_ms}

    Raises on HTTP error or timeout.
    """
    payload = {
        "document_id":   document_id,
        "page_id":       page_id,
        "region_id":     region_id,
        "crop_s3_url":   crop_s3_url,
        "image_width":   image_width,
        "image_height":  image_height,
        "image_format":  "PNG",
        "source":        source,
        "uploaded_at":   uploaded_at,
    }
    log.debug("POST %s%s region_id=%s", FASTAPI_URL, HTR_ENDPOINT, region_id)
    resp = requests.post(
        f"{FASTAPI_URL}{HTR_ENDPOINT}",
        json=payload,
        timeout=HTR_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def process_event(event: dict, slicer: RegionSlicer) -> None:
    """
    Run the full pipeline for a single paperless.uploads event.
    The event is shaped per Phase 4's producer:
        {paperless_doc_id, title, page_count, uploaded_at, source}
    """
    paperless_doc_id = event["paperless_doc_id"]
    title            = event.get("title", f"document_{paperless_doc_id}")
    uploaded_at      = event.get("uploaded_at", "")
    source           = event.get("source", "user_upload")
    t0 = time.time()

    # 1. Slice the document
    result: SlicerResult = slicer.process_document(paperless_doc_id)
    log.info(
        "paperless_doc_id=%s sliced: %d pages, %d regions, %d chars tesseract",
        paperless_doc_id, result.total_pages, len(result.regions), len(result.tesseract_text),
    )

    # 2. Pre-allocate IDs by writing the documents row first. We need the ML-side
    #    UUID (documents.id) to pass to the HTR endpoint per the sample contract,
    #    AND we need page UUIDs per region. So: do one DB transaction that creates
    #    the documents + pages + (blank) regions, then call HTR, then a second
    #    txn to fill in the HTR outputs.
    htr_text_all = ""   # updated after HTR
    merged_text  = result.tesseract_text or ""
    
    with db.conn() as c:
        with c.cursor() as cur:
            # Documents row (skinny — no filename duplication beyond title)
            document_id = db.upsert_document(
                cur,
                paperless_doc_id=paperless_doc_id,
                title=title,
                page_count=result.total_pages,
                tesseract_text=result.tesseract_text or "",
                htr_text="",  # filled in next pass
                merged_text=merged_text,
                source=source,
            )
            db.delete_existing_pages_and_regions(cur, document_id)
            
            # Build pages and regions
            page_ids_by_number = {}
            region_placeholders = []  # (page_uuid, region_uuid, slicer_region)
            for page_num in range(1, result.total_pages + 1):
                image_s3_url = f"s3://{slicer.minio_bucket}/documents/{paperless_doc_id}/pages/p{page_num}.png"
                page_uuid = db.insert_page(
                    cur, document_id,
                    db.PageRow(
                        page_number=page_num,
                        image_s3_url=image_s3_url,
                        tesseract_text=result.tesseract_text or "",
                    ),
                )
                page_ids_by_number[page_num] = page_uuid

            for sr in result.regions:
                page_uuid = page_ids_by_number[sr.page_number]
                region_uuid = db.insert_region(
                    cur, page_uuid,
                    db.RegionRow(
                        crop_s3_url=sr.crop_s3_url,
                        page_number=sr.page_number,
                    ),
                )
                region_placeholders.append((page_uuid, region_uuid, sr))

    # 3. Call the HTR endpoint for each region. Serving fetches crops from MinIO
    #    itself (via crop_s3_url), so we don't send image bytes.
    htr_responses = []
    for page_uuid, region_uuid, sr in region_placeholders:
        try:
            r = _call_htr(
                document_id=document_id,
                page_id=page_uuid,
                region_id=region_uuid,
                crop_s3_url=sr.crop_s3_url,
                image_width=sr.width,
                image_height=sr.height,
                source=source,
                uploaded_at=uploaded_at,
            )
            htr_responses.append((page_uuid, region_uuid, sr, r))
            log.info(
                "  HTR region_id=%s conf=%.3f flagged=%s output=%r",
                region_uuid,
                r.get("htr_confidence", 0.0),
                r.get("htr_flagged", False),
                (r.get("htr_output") or "")[:60],
            )
        except Exception as exc:
            log.warning("  HTR call failed for region_id=%s: %s", region_uuid, exc)
            # Record an empty result so we still flag the region for review
            htr_responses.append((page_uuid, region_uuid, sr, {
                "htr_output": "",
                "htr_confidence": 0.0,
                "htr_flagged": True,
                "model_version": "error",
                "inference_time_ms": 0,
            }))

    # 4. Second transaction: write HTR outputs into regions + pages, and update
    #    the documents row with htr_text + merged_text.
    htr_text_all = "\n".join(
        (r.get("htr_output") or "").strip()
        for _, _, _, r in htr_responses
        if (r.get("htr_output") or "").strip()
    )
    merged_text = result.merge_text([r.get("htr_output", "") for _, _, _, r in htr_responses])

    with db.conn() as c:
        with c.cursor() as cur:
            # Update each region with its HTR output
            for page_uuid, region_uuid, sr, r in htr_responses:
                cur.execute(
                    """
                    UPDATE handwritten_regions
                    SET htr_output = %s, htr_confidence = %s
                    WHERE id = %s
                    """,
                    (r.get("htr_output", ""), r.get("htr_confidence"), region_uuid),
                )

            # Update pages with aggregated HTR info (any region flagged → page flagged)
            page_flags = {}   # page_uuid -> (htr_text_joined, max_confidence_of_flagged, any_flagged)
            for page_uuid, _, _, r in htr_responses:
                out = (r.get("htr_output") or "").strip()
                conf = r.get("htr_confidence")
                flagged = bool(r.get("htr_flagged", False))
                slot = page_flags.setdefault(page_uuid, ["", [], False])
                if out:
                    slot[0] = (slot[0] + "\n" + out).strip() if slot[0] else out
                if conf is not None:
                    slot[1].append(conf)
                slot[2] = slot[2] or flagged

            for page_uuid, (ptext, confs, flagged) in page_flags.items():
                avg_conf = (sum(confs) / len(confs)) if confs else None
                cur.execute(
                    """
                    UPDATE document_pages
                    SET htr_text = %s, htr_confidence = %s, htr_flagged = %s
                    WHERE id = %s
                    """,
                    (ptext, avg_conf, flagged, page_uuid),
                )

            # Update documents row's aggregate text
            cur.execute(
                """
                UPDATE documents
                SET htr_text = %s, merged_text = %s
                WHERE id = %s
                """,
                (htr_text_all, merged_text, document_id),
            )

    elapsed = time.time() - t0
    log.info(
        "paperless_doc_id=%s processed in %.2fs: doc_uuid=%s, regions=%d, flagged_pages=%d",
        paperless_doc_id, elapsed, document_id,
        len(region_placeholders),
        sum(1 for _, (_, _, flagged) in page_flags.items() if flagged) if region_placeholders else 0,
    )
