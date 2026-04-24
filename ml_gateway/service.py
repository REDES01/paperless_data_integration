"""
ML Gateway — HTR + semantic search serving layer.

Single FastAPI process. Two endpoints that matter:
  POST /predict/htr
    body: {document_id, page_id, region_id, crop_s3_url, ...}
    pulls the crop from MinIO, runs pretrained TrOCR on it, returns transcription.
  POST /predict/search
    body: {query, k?}
    embeds query with mpnet, queries Qdrant, returns ranked results.

Plus:
  GET /health           liveness (models loaded + qdrant reachable)
  GET /metrics          Prometheus scrape endpoint

Model versioning:
  Pretrained defaults unless MODEL_REGISTRY_FILE points at a path containing
  an MLflow model URI, in which case the HTR model is reloaded from that URI.
  The deploy script writes that file; on SIGHUP or restart we pick it up.

Fail-loud policy: if TrOCR or mpnet fail to load at startup, we exit so
docker-compose restarts. A gateway that returns empty strings is worse than
one that's down.
"""
from __future__ import annotations

import io
import logging
import os
import signal
import sys
import time
from pathlib import Path
from threading import Lock

import numpy as np
from fastapi import FastAPI, HTTPException
from minio import Minio
from PIL import Image
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel
from starlette.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ml_gateway")

# ── Config ─────────────────────────────────────────────────────────

MINIO_ENDPOINT    = os.environ.get("MINIO_ENDPOINT",   "minio:9000")
MINIO_ACCESS_KEY  = os.environ.get("MINIO_ACCESS_KEY", "admin")
MINIO_SECRET_KEY  = os.environ.get("MINIO_SECRET_KEY", "paperless_minio")
MINIO_SECURE      = os.environ.get("MINIO_SECURE", "false").lower() == "true"

QDRANT_HOST       = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT       = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "document_chunks")

HTR_MODEL_NAME    = os.environ.get("HTR_MODEL_NAME",    "microsoft/trocr-base-handwritten")
RETR_MODEL_NAME   = os.environ.get("RETR_MODEL_NAME",   "sentence-transformers/all-mpnet-base-v2")

# MLflow-registered model override. If the file exists and contains a URI,
# reload HTR from there at startup. Deploy script writes this; rollback
# controller rewrites it; SIGHUP triggers a reload.
MODEL_REGISTRY_FILE = os.environ.get("MODEL_REGISTRY_FILE", "/models/current_htr.txt")

# ── Feedback-aware reranker ───────────────────────────────────────
# ml_gateway over-fetches 2x from Qdrant and reranks results using
# per-document thumbs-up/down feedback aggregated by the airflow
# search_feedback_rerank DAG. Formula:
#     final = cosine_similarity * (1 + alpha_up*up_rate - alpha_down*down_rate)
# Docs with no feedback fall back to pure cosine (boost factor 1.0).
#
# Stats table (document_feedback_stats) is read on demand with an in-memory
# TTL cache to avoid hitting Postgres on every request.
PAPERLESS_ML_DB_HOST = os.environ.get("PAPERLESS_ML_DB_HOST", "postgres")
PAPERLESS_ML_DB_PORT = int(os.environ.get("PAPERLESS_ML_DB_PORT", "5432"))
PAPERLESS_ML_DB_NAME = os.environ.get("PAPERLESS_ML_DB_NAME", "paperless")
PAPERLESS_ML_DB_USER = os.environ.get("PAPERLESS_ML_DB_USER", "user")
PAPERLESS_ML_DB_PASSWORD = os.environ.get("PAPERLESS_ML_DB_PASSWORD", "paperless_postgres")

RERANKER_ENABLED    = os.environ.get("RERANKER_ENABLED", "true").lower() == "true"
RERANKER_ALPHA_UP   = float(os.environ.get("RERANKER_ALPHA_UP",   "0.3"))
RERANKER_ALPHA_DOWN = float(os.environ.get("RERANKER_ALPHA_DOWN", "0.5"))
RERANKER_OVERFETCH  = int(os.environ.get("RERANKER_OVERFETCH", "2"))
RERANKER_STATS_TTL  = int(os.environ.get("RERANKER_STATS_TTL_SECONDS", "60"))


# ── Prometheus metrics ─────────────────────────────────────────────

htr_requests_total = Counter(
    "htr_requests_total", "HTR requests by outcome", ["status"]
)
search_requests_total = Counter(
    "search_requests_total", "Search requests by outcome", ["status"]
)
htr_latency = Histogram(
    "htr_latency_seconds", "Latency of /predict/htr",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)
search_latency = Histogram(
    "search_latency_seconds", "Latency of /predict/search",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0],
)
htr_confidence = Histogram(
    "htr_confidence", "Per-region HTR confidence",
    buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)
# Reranker telemetry — every /predict/search hit is either reranked or not.
rerank_events = Counter(
    "rerank_events_total", "Search results reranked by feedback stats",
    ["outcome"],  # boosted, demoted, neutral, disabled
)


class FeedbackStatsCache:
    """Thread-safe TTL cache for per-document feedback stats.

    Reads from document_feedback_stats (populated by the airflow
    search_feedback_rerank DAG). Refreshes lazily on first access after
    TTL expiry. A single refresh holds a lock so concurrent requests
    don't all hammer Postgres during cache expiry.
    """

    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._stats: dict[str, dict] = {}
        self._expires_at = 0.0
        self._lock = Lock()

    def get(self) -> dict[str, dict]:
        now = time.time()
        if now < self._expires_at:
            return self._stats
        with self._lock:
            # Double-check under lock: another thread may have refreshed
            if now < self._expires_at:
                return self._stats
            try:
                self._stats = self._fetch()
                self._expires_at = now + self.ttl_seconds
            except Exception as exc:
                # On DB error, keep stale cache + extend TTL a bit so we
                # don't hammer Postgres. Reranker degrades to neutral for
                # any missing-from-cache docs, which is safe.
                log.warning("feedback stats refresh failed: %s — keeping stale cache", exc)
                self._expires_at = now + 5
        return self._stats

    def _fetch(self) -> dict[str, dict]:
        import psycopg2
        dsn = (
            f"host={PAPERLESS_ML_DB_HOST} port={PAPERLESS_ML_DB_PORT} "
            f"dbname={PAPERLESS_ML_DB_NAME} user={PAPERLESS_ML_DB_USER} "
            f"password={PAPERLESS_ML_DB_PASSWORD}"
        )
        with psycopg2.connect(dsn, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT document_id::text, thumbs_up, thumbs_down, total_impressions,
                       up_rate, down_rate
                FROM document_feedback_stats
            """)
            rows = cur.fetchall()
        out: dict[str, dict] = {}
        for doc_id, up, down, imp, up_rate, down_rate in rows:
            out[doc_id] = {
                "thumbs_up":         up,
                "thumbs_down":       down,
                "total_impressions": imp,
                "up_rate":           float(up_rate or 0.0),
                "down_rate":         float(down_rate or 0.0),
            }
        log.info("reranker cache refreshed: %d doc stats loaded", len(out))
        return out


feedback_cache = FeedbackStatsCache(ttl_seconds=RERANKER_STATS_TTL)


model_version_gauge = Counter(
    "model_reloads_total", "HTR model reload events", ["source"]
)


# ── Models ─────────────────────────────────────────────────────────

class _State:
    htr_processor = None
    htr_model = None
    htr_version = "pretrained"
    retrieval_model = None
    minio: Minio | None = None
    qdrant = None
    lock = Lock()


state = _State()


def _load_htr():
    """Load TrOCR. If MODEL_REGISTRY_FILE points at an MLflow URI, use that."""
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel

    source = HTR_MODEL_NAME
    version = "pretrained"

    reg_path = Path(MODEL_REGISTRY_FILE)
    if reg_path.exists():
        try:
            uri = reg_path.read_text(encoding="utf-8").strip()
            if uri:
                # MLflow model URIs look like "models:/htr/1" or "runs:/<id>/model"
                import mlflow.transformers
                model_dict = mlflow.transformers.load_model(uri, return_type="components")
                # MLflow's components dict exposes image_processor and
                # tokenizer separately. TrOCR's decoding calls batch_decode,
                # which is a TOKENIZER method — a ViTImageProcessor alone
                # does not have it. Reconstruct a TrOCRProcessor that
                # bundles both so predict_htr can call batch_decode on it.
                image_proc = model_dict.get("image_processor")
                tokenizer  = model_dict.get("tokenizer")
                if image_proc is not None and tokenizer is not None:
                    state.htr_processor = TrOCRProcessor(
                        image_processor=image_proc, tokenizer=tokenizer,
                    )
                else:
                    # Fall back: fine-tuning only changes model weights,
                    # so the base-checkpoint processor is still valid.
                    state.htr_processor = TrOCRProcessor.from_pretrained(HTR_MODEL_NAME)
                state.htr_model = model_dict["model"]
                version = uri
                log.info("HTR loaded from MLflow: %s", uri)
                model_version_gauge.labels(source="mlflow").inc()
                state.htr_version = version
                return
        except Exception as exc:
            log.warning("MLflow load from %s failed, falling back to pretrained: %s", uri, exc)

    log.info("HTR loading pretrained: %s", source)
    state.htr_processor = TrOCRProcessor.from_pretrained(source)
    state.htr_model = VisionEncoderDecoderModel.from_pretrained(source).eval()
    state.htr_version = version
    model_version_gauge.labels(source="pretrained").inc()


def _load_retrieval():
    from sentence_transformers import SentenceTransformer
    log.info("retrieval loading: %s", RETR_MODEL_NAME)
    state.retrieval_model = SentenceTransformer(RETR_MODEL_NAME)


def _connect_minio():
    state.minio = Minio(
        MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE,
    )


def _connect_qdrant():
    from qdrant_client import QdrantClient
    state.qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


# ── FastAPI app ────────────────────────────────────────────────────

app = FastAPI(title="ML Gateway", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    log.info("starting ml-gateway")
    try:
        _load_htr()
        _load_retrieval()
        _connect_minio()
        _connect_qdrant()
        log.info("all models + clients loaded")
    except Exception as exc:
        log.exception("startup failed: %s", exc)
        sys.exit(1)

    # Wire SIGHUP to model reload (deploy script sends this after registry update)
    def _sighup(*_):
        log.info("SIGHUP received — reloading HTR from %s", MODEL_REGISTRY_FILE)
        with state.lock:
            try:
                _load_htr()
                log.info("HTR reload complete: version=%s", state.htr_version)
            except Exception as exc:
                log.exception("HTR reload failed, keeping previous model: %s", exc)

    signal.signal(signal.SIGHUP, _sighup)


@app.get("/health")
def health():
    ok = all([state.htr_model, state.retrieval_model, state.minio, state.qdrant])
    if not ok:
        raise HTTPException(status_code=503, detail="not ready")
    return {
        "status": "ok",
        "htr_version": state.htr_version,
        "htr_model": HTR_MODEL_NAME,
        "retrieval_model": RETR_MODEL_NAME,
    }


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── HTR endpoint ───────────────────────────────────────────────────

class HtrRequest(BaseModel):
    document_id: str
    page_id: str
    region_id: str
    crop_s3_url: str
    image_width: int | None = None
    image_height: int | None = None
    image_format: str | None = None
    source: str | None = None
    uploaded_at: str | None = None


def _parse_s3(url: str) -> tuple[str, str]:
    if not url.startswith("s3://"):
        raise ValueError(f"not an s3 URL: {url!r}")
    rest = url[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not key:
        raise ValueError(f"s3 URL missing key: {url!r}")
    return bucket, key


def _fetch_crop(crop_s3_url: str) -> Image.Image:
    bucket, key = _parse_s3(crop_s3_url)
    resp = state.minio.get_object(bucket, key)
    try:
        raw = resp.read()
    finally:
        resp.close()
        resp.release_conn()
    return Image.open(io.BytesIO(raw)).convert("RGB")


@app.post("/predict/htr")
def predict_htr(req: HtrRequest):
    t0 = time.time()
    try:
        img = _fetch_crop(req.crop_s3_url)
    except ValueError as exc:
        htr_requests_total.labels(status="bad_url").inc()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.warning("minio fetch failed for %s: %s", req.crop_s3_url, exc)
        htr_requests_total.labels(status="minio_error").inc()
        raise HTTPException(status_code=502, detail="minio fetch failed") from exc

    try:
        with state.lock:
            pixel_values = state.htr_processor(images=img, return_tensors="pt").pixel_values
            # Deterministic greedy decode + return logits so we can estimate confidence
            generation = state.htr_model.generate(
                pixel_values,
                max_new_tokens=128,
                return_dict_in_generate=True,
                output_scores=True,
            )
            text = state.htr_processor.batch_decode(
                generation.sequences, skip_special_tokens=True,
            )[0]
            # Confidence = mean per-token softmax probability of the argmax path
            import torch
            if generation.scores:
                probs = [torch.softmax(s, dim=-1).max(dim=-1).values.item()
                         for s in generation.scores]
                confidence = float(np.mean(probs)) if probs else 0.0
            else:
                confidence = 0.0
    except Exception as exc:
        log.exception("htr inference error: %s", exc)
        htr_requests_total.labels(status="inference_error").inc()
        raise HTTPException(status_code=500, detail="inference error") from exc

    htr_requests_total.labels(status="ok").inc()
    htr_confidence.observe(confidence)
    htr_latency.observe(time.time() - t0)

    # Flag threshold: any region whose confidence falls BELOW this gets
    # surfaced in the /ml/htr-review UI for human correction. Tunable at
    # runtime via HTR_FLAG_THRESHOLD env var.
    #   0.70  default (rubric)
    #   0.85  more conservative (flag more for review — useful during demo
    #          when the model is quite confident even on odd crops)
    #   0.50  more permissive (only flag truly uncertain regions)
    _flag_threshold = float(os.environ.get("HTR_FLAG_THRESHOLD", "0.85"))
    flagged = confidence < _flag_threshold
    return {
        "region_id": req.region_id,
        "htr_output": text,
        "htr_confidence": confidence,
        "htr_flagged": flagged,
        "model_version": state.htr_version,
        "inference_time_ms": int((time.time() - t0) * 1000),
    }


# ── Semantic search endpoint ───────────────────────────────────────

class SearchRequest(BaseModel):
    # Dual contract: accept both the ml_gateway-native shape (query, k)
    # and the frontend/Yikai shape (query_text, top_k, session_id, user_id).
    # Whichever the caller sends, we normalise to query+k internally.
    query: str | None = None
    query_text: str | None = None
    k: int = 10
    top_k: int | None = None
    session_id: str | None = None
    user_id: str | None = None

    model_config = {"extra": "allow"}

    @property
    def effective_query(self) -> str:
        return (self.query or self.query_text or "").strip()

    @property
    def effective_k(self) -> int:
        return self.top_k if self.top_k is not None else self.k


@app.post("/predict/search")
def predict_search(req: SearchRequest):
    t0 = time.time()
    query = req.effective_query
    k = req.effective_k
    if not query:
        search_requests_total.labels(status="empty_query").inc()
        raise HTTPException(status_code=400, detail="empty query")

    try:
        vec = state.retrieval_model.encode(query, normalize_embeddings=True).tolist()
    except Exception as exc:
        log.exception("embed error: %s", exc)
        search_requests_total.labels(status="embed_error").inc()
        raise HTTPException(status_code=500, detail="embed error") from exc

    # Over-fetch so reranking can move things in from below the cutoff.
    fetch_limit = k * RERANKER_OVERFETCH if RERANKER_ENABLED else k
    try:
        hits = state.qdrant.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vec,
            limit=fetch_limit,
        )
    except Exception as exc:
        log.warning("qdrant search error: %s", exc)
        search_requests_total.labels(status="qdrant_error").inc()
        raise HTTPException(status_code=502, detail="qdrant error") from exc

    # Apply feedback-aware reranking if enabled. Each hit gets a boost
    # factor based on accumulated user feedback on that document; final
    # ranking is by boosted score, then we slice to top-k.
    stats_map = feedback_cache.get() if RERANKER_ENABLED else {}

    results = []
    for i, h in enumerate(hits):
        payload = h.payload or {}
        snippet = payload.get("snippet", "")
        original_score = float(h.score)

        doc_id = payload.get("document_id", "")
        stats = stats_map.get(str(doc_id)) if doc_id else None

        if stats and stats["total_impressions"] > 0:
            up_rate   = stats["up_rate"]
            down_rate = stats["down_rate"]
            boost = 1.0 + RERANKER_ALPHA_UP * up_rate - RERANKER_ALPHA_DOWN * down_rate
            final_score = original_score * boost
            if boost > 1.01:
                rerank_outcome = "boosted"
            elif boost < 0.99:
                rerank_outcome = "demoted"
            else:
                rerank_outcome = "neutral"
        else:
            boost = 1.0
            final_score = original_score
            rerank_outcome = "disabled" if not RERANKER_ENABLED else "neutral"

        rerank_events.labels(outcome=rerank_outcome).inc()

        # Each result carries both field-name variants so either client contract works.
        results.append({
            "document_id":       doc_id,
            "paperless_doc_id":  payload.get("paperless_doc_id"),
            "chunk_index":       payload.get("chunk_index", i),
            "chunk_text":        snippet,
            "snippet":           snippet,           # legacy alias
            "similarity_score":  final_score,
            "score":             final_score,       # legacy alias
            # Telemetry for the frontend: show users when feedback affected ranking
            "original_score":    original_score,
            "feedback_boost":    boost,
            "rerank_outcome":    rerank_outcome,
            "feedback_up":       (stats or {}).get("thumbs_up", 0),
            "feedback_down":     (stats or {}).get("thumbs_down", 0),
        })

    # Re-sort by boosted score (descending) and slice to requested k
    results.sort(key=lambda r: r["similarity_score"], reverse=True)
    results = results[:k]

    elapsed_ms = int((time.time() - t0) * 1000)
    search_requests_total.labels(status="ok").inc()
    search_latency.observe(time.time() - t0)
    return {
        # Dual contract fields so both frontend and ml_gateway clients work.
        "query":               query,
        "query_text":          query,
        "session_id":          req.session_id,
        "results":             results,
        "fallback_to_keyword": False,
        "model_version":       getattr(state, "retrieval_version", "mpnet-base-v2"),
        "took_ms":             elapsed_ms,
        "inference_time_ms":   elapsed_ms,
    }
