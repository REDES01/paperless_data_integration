"""
Handwritten region detector.

Vendored from paperless_data/online_features/htr_features.py with the same
algorithm: horizontal projection on a binarized grayscale image to find
contiguous ink-dense rows, then vertical projection to trim each band
horizontally. Filters by minimum region size.

This module has zero external dependencies beyond numpy + Pillow so it can
be imported anywhere without pulling in the full data stack.
"""

import logging

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# ── Tunable thresholds ─────────────────────────
BINARIZATION_THRESHOLD = 180   # pixels darker than this are "ink"
INK_START_RATIO = 0.02         # row is "ink" if ink pixels > 2% of row width
INK_END_RATIO = 0.01           # row is "gap" if ink pixels <= 1% of row width
MIN_REGION_WIDTH = 50          # ignore regions narrower than this (px)
MIN_REGION_HEIGHT = 15         # ignore regions shorter than this (px)
PADDING = 5                    # extra pixels around each crop


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
