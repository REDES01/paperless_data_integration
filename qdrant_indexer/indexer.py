"""
Qdrant indexer — chunks merged_text, embeds with mpnet, upserts to Qdrant.

Consumes paperless.uploads from Redpanda. For each event:

  1. Poll the data-stack Postgres for documents.merged_text where
     paperless_doc_id == event.paperless_doc_id. Wait up to POLL_TIMEOUT
     seconds — htr_consumer writes merged_text in a second transaction
     AFTER calling the HTR endpoint, so there's a small race window.
  2. Chunk merged_text into overlapping ~500-char passages.
  3. Embed each chunk with sentence-transformers/all-mpnet-base-v2.
  4. Upsert points to the Qdrant collection document_chunks. Point payload
     includes {document_id, paperless_doc_id, snippet, chunk_idx}.

This is the reciprocal of the ml-gateway's /predict/search: queries get
embedded with the same model, searched against this collection.

Dependencies: uses the same embedding model as ml-gateway, so vectors line up.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

import psycopg2
import psycopg2.extras
from kafka import KafkaConsumer
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("qdrant_indexer")

# ── Config ─────────────────────────────────────────────────────────

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "redpanda:9092")
KAFKA_TOPIC     = os.environ.get("KAFKA_TOPIC", "paperless.uploads")
KAFKA_GROUP_ID  = os.environ.get("KAFKA_GROUP_ID", "qdrant-indexer")

DB_DSN = os.environ.get(
    "DB_DSN",
    "host=postgres dbname=paperless user=user password=paperless_postgres",
)
POLL_TIMEOUT = float(os.environ.get("MERGED_TEXT_POLL_SECONDS", "120"))
POLL_INTERVAL = 2.0

QDRANT_HOST       = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT       = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "document_chunks")
VECTOR_DIM        = 768        # mpnet-base output dim

MODEL_NAME        = os.environ.get("RETR_MODEL_NAME", "sentence-transformers/all-mpnet-base-v2")
CHUNK_SIZE        = int(os.environ.get("CHUNK_SIZE", "500"))
CHUNK_OVERLAP     = int(os.environ.get("CHUNK_OVERLAP", "100"))


# ── Chunking ───────────────────────────────────────────────────────

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        out.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return out


# ── DB + Qdrant helpers ────────────────────────────────────────────

def fetch_document(conn, paperless_doc_id: int) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, paperless_doc_id, merged_text, filename "
            "FROM documents WHERE paperless_doc_id = %s",
            (paperless_doc_id,),
        )
        return cur.fetchone()


def wait_for_merged_text(dsn: str, paperless_doc_id: int) -> dict | None:
    """
    Poll until htr_consumer writes the second transaction (merged_text non-empty).
    Returns the document row, or None if we time out.
    """
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        with psycopg2.connect(dsn) as conn:
            doc = fetch_document(conn, paperless_doc_id)
            if doc and (doc.get("merged_text") or "").strip():
                return doc
        time.sleep(POLL_INTERVAL)
    return None


def ensure_collection(qc: QdrantClient) -> None:
    existing = {c.name for c in qc.get_collections().collections}
    if QDRANT_COLLECTION in existing:
        return
    qc.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )
    log.info("created qdrant collection %s (dim=%d, cosine)", QDRANT_COLLECTION, VECTOR_DIM)


def index_document(qc: QdrantClient, model, doc: dict) -> int:
    chunks = chunk_text(doc["merged_text"])
    if not chunks:
        return 0

    vectors = model.encode(chunks, normalize_embeddings=True).tolist()
    points: list[PointStruct] = []
    for idx, (snippet, vec) in enumerate(zip(chunks, vectors)):
        points.append(PointStruct(
            id=str(uuid.uuid4()),
            vector=vec,
            payload={
                "document_id":      str(doc["id"]),
                "paperless_doc_id": int(doc["paperless_doc_id"]),
                "snippet":          snippet[:400],
                "chunk_idx":        idx,
            },
        ))
    qc.upsert(collection_name=QDRANT_COLLECTION, points=points)
    return len(points)


# ── Main loop ──────────────────────────────────────────────────────

def main() -> int:
    log.info("starting qdrant_indexer")
    log.info("  kafka:     %s topic=%s group=%s", KAFKA_BOOTSTRAP, KAFKA_TOPIC, KAFKA_GROUP_ID)
    log.info("  qdrant:    %s:%d collection=%s", QDRANT_HOST, QDRANT_PORT, QDRANT_COLLECTION)
    log.info("  model:     %s", MODEL_NAME)

    model = SentenceTransformer(MODEL_NAME)
    qc = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    ensure_collection(qc)

    consumer = KafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=KAFKA_GROUP_ID,
        enable_auto_commit=True,
        auto_offset_reset="earliest",
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
    )
    log.info("listening on %s...", KAFKA_TOPIC)

    for msg in consumer:
        try:
            event = msg.value
            paperless_doc_id = int(event["paperless_doc_id"])
        except Exception as exc:
            log.warning("bad event skipped: %s (%s)", msg.value, exc)
            continue

        log.info("event offset=%d partition=%d paperless_doc_id=%d",
                 msg.offset, msg.partition, paperless_doc_id)

        doc = wait_for_merged_text(DB_DSN, paperless_doc_id)
        if doc is None:
            log.warning("paperless_doc_id=%d: merged_text not available after %ds, skipping",
                        paperless_doc_id, POLL_TIMEOUT)
            continue

        try:
            n = index_document(qc, model, doc)
            log.info("paperless_doc_id=%d indexed: %d chunks", paperless_doc_id, n)
        except Exception as exc:
            log.exception("paperless_doc_id=%d indexing failed: %s", paperless_doc_id, exc)
            continue

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
