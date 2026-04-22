"""
Drift monitoring FastAPI service.

Loads a pre-built alibi-detect MMDDriftOnline detector from MinIO at startup,
and exposes three HTTP endpoints:

    POST /drift/check
        Body: {"crop_s3_url": "s3://paperless-images/..."}
        Fetches the crop from MinIO, runs it through the detector,
        increments Prometheus counters if drift is detected.
        Returns 202 Accepted with {"is_drift": bool, "test_stat": float}.

    GET /health
        Liveness probe. Returns 200 if the detector is loaded.

    GET /metrics
        Prometheus scrape endpoint. Exposes:
            drift_events_total     Counter — incremented when is_drift=1
            drift_test_stat        Histogram — raw MMD test statistic
            drift_checks_total     Counter — every check, pass or fail
            drift_check_errors_total Counter — exceptions during check

The detector is built offline by paperless_data/scripts/build_drift_reference.py
and published to s3://paperless-datalake/warehouse/drift_reference/htr_v1/.
On startup we download the `cd/` directory to a temp dir and hand the path to
alibi_detect.saving.load_detector.

Fail-loud policy: if the detector can't load, the service exits so Docker
Compose restarts it. A drift monitor that silently returns "no drift" is
worse than one that's down.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from minio import Minio
from PIL import Image
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel
from starlette.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("drift_monitor")

# ── Config ─────────────────────────────────────────────────────────

MINIO_ENDPOINT    = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY  = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY  = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE      = os.environ.get("MINIO_SECURE", "false").lower() == "true"

# Where the pre-built detector lives. Published by build_drift_reference.py.
REF_BUCKET = os.environ.get("DRIFT_REF_BUCKET", "paperless-datalake")
REF_PREFIX = os.environ.get("DRIFT_REF_PREFIX", "warehouse/drift_reference/htr_v1/cd")

# Preprocessing must match what build_drift_reference.py used.
CROP_HEIGHT = int(os.environ.get("DRIFT_CROP_HEIGHT", "64"))
CROP_WIDTH  = int(os.environ.get("DRIFT_CROP_WIDTH",  "512"))


# ── Prometheus metrics ─────────────────────────────────────────────

drift_checks_total = Counter(
    "drift_checks_total",
    "Total drift checks performed (success + failure).",
)
drift_events_total = Counter(
    "drift_events_total",
    "Drift test triggered (is_drift = 1).",
)
drift_test_stat = Histogram(
    "drift_test_stat",
    "Raw MMD test statistic per check.",
    buckets=[0.0, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0],
)
drift_check_errors_total = Counter(
    "drift_check_errors_total",
    "Exceptions thrown during drift check.",
    ["reason"],
)


# ── Model holder ───────────────────────────────────────────────────

class _State:
    detector = None
    minio: Minio | None = None


state = _State()


def _get_minio() -> Minio:
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def _download_detector_dir(mc: Minio, target: Path) -> Path:
    """Mirror s3://REF_BUCKET/REF_PREFIX/ into `target/` and return the path."""
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for obj in mc.list_objects(REF_BUCKET, prefix=REF_PREFIX + "/", recursive=True):
        rel = Path(obj.object_name).relative_to(REF_PREFIX)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        resp = mc.get_object(REF_BUCKET, obj.object_name)
        try:
            dst.write_bytes(resp.read())
        finally:
            resp.close()
            resp.release_conn()
        count += 1
    if count == 0:
        raise RuntimeError(f"no objects under s3://{REF_BUCKET}/{REF_PREFIX}/")
    log.info("downloaded %d detector files into %s", count, target)
    return target


def _parse_s3(url: str) -> tuple[str, str]:
    if not url.startswith("s3://"):
        raise ValueError(f"not an s3 URL: {url!r}")
    rest = url[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not key:
        raise ValueError(f"s3 URL has no key: {url!r}")
    return bucket, key


def _fetch_and_preprocess(mc: Minio, crop_s3_url: str) -> np.ndarray:
    """Download a crop, convert to the same shape/dtype build_drift_reference used."""
    bucket, key = _parse_s3(crop_s3_url)
    resp = mc.get_object(bucket, key)
    try:
        raw = resp.read()
    finally:
        resp.close()
        resp.release_conn()

    img = Image.open(io.BytesIO(raw)).convert("L").resize((CROP_WIDTH, CROP_HEIGHT))
    arr = np.asarray(img, dtype=np.float32) / 255.0          # (H, W)
    arr = np.expand_dims(arr, axis=0)                        # (1, H, W)  single channel
    return arr


# ── FastAPI app ────────────────────────────────────────────────────

app = FastAPI(title="Drift Monitor", version="1.0")


DETECTOR_RETRY_SECONDS = int(os.environ.get("DRIFT_RETRY_SECONDS", "60"))


def _try_load_detector() -> bool:
    """One attempt at loading the detector from MinIO. Returns True on success.

    Non-fatal: logs warnings on failure. Caller decides whether to retry.
    """
    from alibi_detect.saving import load_detector    # lazy import: heavy

    try:
        if state.minio is None:
            state.minio = _get_minio()
        tmpdir = Path(tempfile.mkdtemp(prefix="drift_cd_"))
        detector_dir = _download_detector_dir(state.minio, tmpdir)
        state.detector = load_detector(str(detector_dir))
        log.info("detector loaded successfully")
        return True
    except Exception as exc:
        log.warning("detector load failed (will retry in %ds): %s",
                    DETECTOR_RETRY_SECONDS, exc)
        return False


def _retry_loop() -> None:
    """Background thread: poll MinIO until the detector loads successfully."""
    while state.detector is None:
        time.sleep(DETECTOR_RETRY_SECONDS)
        log.info("retrying detector load...")
        if _try_load_detector():
            return


@app.on_event("startup")
def _startup() -> None:
    """
    Non-fatal startup. If the detector is missing (reference not yet built),
    the service comes up with detector=None and retries in the background.
    /health reports 503 until the detector is loaded; /drift/check returns 503.
    Once the detector loads, the service becomes healthy automatically.
    """
    log.info("starting drift monitor")
    log.info("  MinIO endpoint:   %s", MINIO_ENDPOINT)
    log.info("  reference path:   s3://%s/%s", REF_BUCKET, REF_PREFIX)
    log.info("  crop shape:       %sx%s (HxW)", CROP_HEIGHT, CROP_WIDTH)

    if _try_load_detector():
        return

    # Not fatal — spawn a background retry thread so we become healthy as soon
    # as the reference appears in MinIO (typically right after
    # build_drift_reference.py runs).
    log.info("detector not yet available; starting background retry loop "
             "(every %ds)", DETECTOR_RETRY_SECONDS)
    t = threading.Thread(target=_retry_loop, name="drift-retry", daemon=True)
    t.start()


@app.get("/health")
def health():
    if state.detector is None:
        raise HTTPException(status_code=503, detail="detector not loaded")
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class CheckRequest(BaseModel):
    crop_s3_url: str


@app.post("/drift/check", status_code=202)
def drift_check(req: CheckRequest):
    if state.detector is None or state.minio is None:
        raise HTTPException(status_code=503, detail="detector not loaded")

    drift_checks_total.inc()
    try:
        x = _fetch_and_preprocess(state.minio, req.crop_s3_url)
    except ValueError as exc:
        drift_check_errors_total.labels(reason="bad_url").inc()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.warning("fetch failed for %s: %s", req.crop_s3_url, exc)
        drift_check_errors_total.labels(reason="fetch_failed").inc()
        raise HTTPException(status_code=502, detail="fetch failed") from exc

    try:
        pred = state.detector.predict(x)["data"]
    except Exception as exc:
        log.warning("detector error on %s: %s", req.crop_s3_url, exc)
        drift_check_errors_total.labels(reason="detector_error").inc()
        raise HTTPException(status_code=500, detail="detector error") from exc

    test_stat = float(pred.get("test_stat", 0.0))
    is_drift = bool(pred.get("is_drift", 0))

    drift_test_stat.observe(test_stat)
    if is_drift:
        drift_events_total.inc()
        log.info("DRIFT detected: %s (test_stat=%.4f)", req.crop_s3_url, test_stat)

    return {"is_drift": is_drift, "test_stat": test_stat}
