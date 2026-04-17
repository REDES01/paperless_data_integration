"""
Postgres writer for the HTR consumer.

Uses psycopg 3 against the data-stack Postgres (container name `postgres`
on paperless_ml_net, DB `paperless`, user `user`).
"""

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg

log = logging.getLogger(__name__)


def _conn_info() -> dict:
    return {
        "host":     os.environ.get("ML_DB_HOST", "postgres"),
        "port":     int(os.environ.get("ML_DB_PORT", "5432")),
        "dbname":   os.environ.get("ML_DB_NAME", "paperless"),
        "user":     os.environ.get("ML_DB_USER", "user"),
        "password": os.environ.get("ML_DB_PASSWORD", "paperless_postgres"),
    }


@contextmanager
def conn():
    c = psycopg.connect(**_conn_info(), autocommit=False)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ── Data structures ────────────────────────────

@dataclass
class PageRow:
    page_number: int
    image_s3_url: str
    tesseract_text: str = ""
    # Page-level HTR fields (filled in by the consumer after HTR)
    htr_text: str = ""
    htr_confidence: float | None = None
    htr_flagged: bool = False


@dataclass
class RegionRow:
    crop_s3_url: str
    htr_output: str = ""
    htr_confidence: float | None = None
    # Tracked so the consumer can attribute the HTR call to a page
    page_number: int = 1


# ── Write operations ───────────────────────────

def upsert_document(
    cur,
    paperless_doc_id: int,
    title: str,
    page_count: int,
    tesseract_text: str,
    htr_text: str,
    merged_text: str,
    source: str = "user_upload",
) -> str:
    """
    Insert or update a documents row keyed by paperless_doc_id.
    Returns the ML-side UUID (documents.id).
    """
    cur.execute(
        """
        INSERT INTO documents (
            filename, source, page_count, tesseract_text, htr_text,
            merged_text, paperless_doc_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (paperless_doc_id) DO UPDATE SET
            page_count      = EXCLUDED.page_count,
            tesseract_text  = EXCLUDED.tesseract_text,
            htr_text        = EXCLUDED.htr_text,
            merged_text     = EXCLUDED.merged_text,
            source          = EXCLUDED.source
        RETURNING id
        """,
        (title, source, page_count, tesseract_text, htr_text,
         merged_text, paperless_doc_id),
    )
    (ml_id,) = cur.fetchone()
    return str(ml_id)


def delete_existing_pages_and_regions(cur, document_id: str) -> None:
    """
    Clean slate for a document before re-inserting pages and regions.
    Called before inserts so that reprocessing a document doesn't accumulate
    duplicate page/region rows. Cascade via a DELETE on pages removes
    FK-dependent regions via a separate explicit delete first.
    """
    cur.execute(
        """
        DELETE FROM handwritten_regions
        WHERE page_id IN (SELECT id FROM document_pages WHERE document_id = %s)
        """,
        (document_id,),
    )
    cur.execute(
        "DELETE FROM document_pages WHERE document_id = %s",
        (document_id,),
    )


def insert_page(cur, document_id: str, page: PageRow) -> str:
    """Insert a document_pages row. Returns the new page UUID."""
    cur.execute(
        """
        INSERT INTO document_pages (
            document_id, page_number, image_s3_url, tesseract_text,
            htr_text, htr_confidence, htr_flagged
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            document_id,
            page.page_number,
            page.image_s3_url,
            page.tesseract_text,
            page.htr_text,
            page.htr_confidence,
            page.htr_flagged,
        ),
    )
    (page_id,) = cur.fetchone()
    return str(page_id)


def insert_region(cur, page_id: str, region: RegionRow) -> str:
    """Insert a handwritten_regions row. Returns the new region UUID."""
    cur.execute(
        """
        INSERT INTO handwritten_regions (
            page_id, crop_s3_url, htr_output, htr_confidence
        )
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (page_id, region.crop_s3_url, region.htr_output, region.htr_confidence),
    )
    (region_id,) = cur.fetchone()
    return str(region_id)
