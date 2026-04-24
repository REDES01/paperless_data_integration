"""
Handwritten region detector.

Vendored from paperless_data/online_features/htr_features.py with the same
algorithm: horizontal projection on a binarized grayscale image to find
contiguous ink-dense rows, then vertical projection to trim each band
horizontally. Filters by minimum region size.

This module has zero external dependencies beyond numpy + Pillow so it can
be imported anywhere without pulling in the full data stack.

Tunable thresholds are all env-var overridable so the filter behavior can
be tweaked without rebuilding the container. Sensible defaults target
phone-photo-of-notebook scans where pen ink is clear (<150) and pencil
can be faint (180-210).
"""

import logging
import os

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# ── Tunable thresholds (env-var overridable) ─────────────────────────
# BINARIZATION_THRESHOLD: pixels with grayscale value BELOW this are "ink".
#   Default 210 catches pencil + faint pen ink on off-white paper.
#   Lower (120) if scans are over-triggering on paper texture.
#   Higher (230) if scans have extremely light ink like dry-erase or
#   heavily-faded old documents.
BINARIZATION_THRESHOLD = _env_int("SLICER_BINARIZATION_THRESHOLD", 210)

# INK_START/END_RATIO: fraction of row width that must be inked for a
# row to count as part of a region (start) or not (end). A row of 2000px
# width is "in region" if more than 2%=40 inked pixels, "out of region"
# if ≤ 1%=20 inked pixels.
INK_START_RATIO = _env_float("SLICER_INK_START_RATIO", 0.01)
INK_END_RATIO   = _env_float("SLICER_INK_END_RATIO",   0.005)

MIN_REGION_WIDTH  = _env_int("SLICER_MIN_REGION_WIDTH",  50)
MIN_REGION_HEIGHT = _env_int("SLICER_MIN_REGION_HEIGHT", 15)
PADDING           = _env_int("SLICER_PADDING",           5)


def detect_regions(page_image: Image.Image) -> list[dict]:
    """
    Detect handwritten regions in a page image.

    Returns a list of dicts, each with:
        bbox:   [x1, y1, x2, y2]  (pixel coordinates)
        width:  int
        height: int
    """
    gray = page_image.convert("L")
    arr = np.array(gray)
    w = arr.shape[1]

    # Binarize: 1 = ink, 0 = background
    binary = (arr < BINARIZATION_THRESHOLD).astype(np.uint8)

    # Horizontal projection: sum of ink pixels per row
    h_proj = binary.sum(axis=1)

    # Quick debug telemetry — if the threshold is wrong for this image,
    # the projection will either be all-zero (too dark threshold) or
    # near-uniform (too light threshold). Log the mean so the operator
    # can spot it in consumer logs.
    mean_ink_ratio = float(h_proj.mean()) / max(1, w)
    log.info(
        "slicer: %dx%d image, binarization<%d, mean ink ratio %.3f",
        w, arr.shape[0], BINARIZATION_THRESHOLD, mean_ink_ratio,
    )

    regions = []
    in_region = False
    y_start = 0

    def _finish_region(y_top, y_bottom):
        """Trim horizontally via vertical projection and emit if large enough."""
        if y_bottom - y_top < MIN_REGION_HEIGHT:
            return
        region_slice = binary[y_top:y_bottom, :]
        v_proj = region_slice.sum(axis=0)
        x_coords = np.where(v_proj > 0)[0]
        if len(x_coords) == 0:
            return
        x1 = max(0, int(x_coords[0]) - PADDING)
        x2 = min(w, int(x_coords[-1]) + PADDING)
        if x2 - x1 < MIN_REGION_WIDTH:
            return
        y1 = max(0, y_top - PADDING)
        y2 = min(arr.shape[0], y_bottom + PADDING)
        regions.append({
            "bbox": [x1, y1, x2, y2],
            "width": x2 - x1,
            "height": y2 - y1,
        })

    for y, count in enumerate(h_proj):
        if count > w * INK_START_RATIO and not in_region:
            in_region = True
            y_start = y
        elif count <= w * INK_END_RATIO and in_region:
            in_region = False
            _finish_region(y_start, y)

    # Handle region that extends to the bottom of the page
    if in_region:
        _finish_region(y_start, arr.shape[0])

    log.info("Detected %d regions in %dx%d image", len(regions), w, arr.shape[0])
    return regions


def crop_region(page_image: Image.Image, bbox: list[int]) -> Image.Image:
    """Crop a single region from the page image."""
    x1, y1, x2, y2 = bbox
    return page_image.crop((x1, y1, x2, y2))
