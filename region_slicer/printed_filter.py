"""
Tesseract-guided handwriting filter (B1).

Problem: the ink-density region detector in detector.py flags every dense
text band, printed OR handwritten. Feeding printed text to a TrOCR model
fine-tuned on cursive handwriting produces garbage, which shows up in the
review UI as "fake" regions.

Solution: Paperless produces a searchable archive PDF via Tesseract, which
contains word-level bounding boxes. Any region the slicer flagged that is
already well-covered by high-confidence Tesseract words is printed text —
skip it. Regions that Tesseract couldn't read (low / no word coverage)
are candidates for handwriting.

This is a zero-new-model filter: Tesseract's work is already done by
Paperless consumption, we're just reusing the byproduct.

No new dependencies — pdfplumber is pure Python on top of pdfminer.six
which only needs the standard library.
"""

import io
import logging

log = logging.getLogger(__name__)

# Below this value we treat a region as "Tesseract didn't read it" => likely
# handwriting => keep it for HTR.
#
# Measured on a mix of printed text and IAM handwriting crops:
#   - printed line with good scan:      coverage ~0.55 - 0.85
#   - printed line with poor scan:      coverage ~0.20 - 0.50
#   - handwritten line:                  coverage ~0.00 - 0.15 (Tesseract
#                                        usually outputs nothing for cursive)
#   - mixed line (printed + hand note):  coverage ~0.25 - 0.40
#
# 0.25 is a safe default: misses some poor-scan printed text (keeps them
# as HTR candidates, mildly wasteful) but catches the main failure mode
# (printed regions being transcribed by TrOCR and looking like garbage).
PRINTED_COVERAGE_THRESHOLD = 0.50


def _bbox_area(bbox) -> float:
    x1, y1, x2, y2 = bbox
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _bbox_intersection_area(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    return (ix2 - ix1) * (iy2 - iy1)


def extract_tesseract_word_boxes(
    archive_bytes: bytes,
    page_size_px: tuple[int, int],
    page_index: int,
) -> list[dict]:
    """
    Parse a Paperless archive PDF and return word boxes for one page in
    the SAME pixel coordinate system the slicer uses (page_size_px).

    The archive PDF is a searchable copy of the original document with
    Tesseract's OCR embedded as a text layer. We read the word positions
    from that text layer using pdfplumber and scale them into the page
    image's pixel space.

    Args:
        archive_bytes:   raw bytes of the archive PDF
        page_size_px:    (width_px, height_px) of the page image the
                         slicer is working with (from pdf2image render)
        page_index:      0-based page number

    Returns:
        list of {"bbox": [x1, y1, x2, y2], "text": str} dicts with
        bbox in pixel coordinates matching the slicer's page image.
        Empty list if the page isn't present or the PDF has no text layer.
    """
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — printed-filter disabled")
        return []

    try:
        pdf = pdfplumber.open(io.BytesIO(archive_bytes))
    except Exception as exc:
        log.warning("pdfplumber failed to open archive: %s", exc)
        return []

    try:
        if page_index >= len(pdf.pages):
            return []
        page = pdf.pages[page_index]

        # pdfplumber coordinates are in PDF points (1/72 inch). Scale to
        # the pixel size of the page image the slicer rendered.
        pdf_w, pdf_h = page.width, page.height
        px_w, px_h = page_size_px
        if pdf_w <= 0 or pdf_h <= 0:
            return []
        sx = px_w / pdf_w
        sy = px_h / pdf_h

        words = page.extract_words() or []
        boxes = []
        for w in words:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            x1 = float(w["x0"]) * sx
            y1 = float(w["top"]) * sy
            x2 = float(w["x1"]) * sx
            y2 = float(w["bottom"]) * sy
            if x2 - x1 < 1 or y2 - y1 < 1:
                continue
            boxes.append({"bbox": [x1, y1, x2, y2], "text": text})
        return boxes
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def filter_handwritten_regions(
    regions: list[dict],
    tesseract_word_boxes: list[dict],
    coverage_threshold: float = PRINTED_COVERAGE_THRESHOLD,
) -> tuple[list[dict], list[dict]]:
    """
    Split slicer-detected regions into (handwritten, rejected_printed).

    A region is classified as printed and filtered out when the total
    area of its intersections with Tesseract word boxes exceeds
    `coverage_threshold * region_area`.

    Args:
        regions:                output of detector.detect_regions()
        tesseract_word_boxes:   output of extract_tesseract_word_boxes()
        coverage_threshold:     fraction of region area that must be
                                covered by Tesseract words for the
                                region to be considered printed

    Returns:
        (kept_regions, rejected_regions)
        kept regions are unchanged in shape; rejected regions get an
        extra 'tesseract_coverage' key for logging / debugging.
    """
    if not tesseract_word_boxes:
        # No Tesseract coverage to compare against — keep everything,
        # as if the filter were disabled. Caller can detect this by
        # checking len(rejected) == 0.
        return regions, []

    kept, rejected = [], []
    for region in regions:
        region_bbox = region["bbox"]
        region_area = _bbox_area(region_bbox)
        if region_area <= 0:
            kept.append(region)
            continue

        overlap_area = 0.0
        for word in tesseract_word_boxes:
            overlap_area += _bbox_intersection_area(region_bbox, word["bbox"])
            # Early-exit if we've already passed the threshold
            if overlap_area >= coverage_threshold * region_area:
                break

        coverage = overlap_area / region_area
        if coverage >= coverage_threshold:
            rejected_region = dict(region)
            rejected_region["tesseract_coverage"] = coverage
            rejected.append(rejected_region)
        else:
            kept.append(region)

    log.info(
        "printed-filter: kept %d, rejected %d printed regions "
        "(coverage threshold = %.2f)",
        len(kept), len(rejected), coverage_threshold,
    )
    return kept, rejected
