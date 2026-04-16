"""
Region slicer — takes a Paperless document, detects handwritten regions,
crops them, and uploads the crops to MinIO.

This is the core image-processing component of the HTR preprocessing pipeline.
It has no Kafka or Postgres dependency — those are added by the consumer wrapper
in Phase 2.

Usage (as a library):
    from slicer import RegionSlicer
    s = RegionSlicer(paperless_url="http://...", paperless_token="...", minio_endpoint="...")
    results = s.process_document(document_id=42)

Usage (CLI):
    python slicer.py --doc-id 42
"""

import io
import logging
import os
import uuid
from dataclasses import dataclass, field

import requests
from minio import Minio
from pdf2image import convert_from_bytes
from PIL import Image

from detector import detect_regions, crop_region

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@dataclass
class SlicedRegion:
    """One detected handwritten region."""
    page_number: int
    region_index: int
    bbox: list[int]
    width: int
    height: int
    crop_s3_url: str
    region_id: str


@dataclass
class SlicerResult:
    """Result of processing one document."""
    paperless_doc_id: int
    title: str
    total_pages: int
    tesseract_text: str = ""
    regions: list[SlicedRegion] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Document {self.paperless_doc_id} ({self.title}): "
            f"{self.total_pages} pages, {len(self.regions)} regions detected, "
            f"{len(self.tesseract_text)} chars of Tesseract text"
        )

    def merge_text(self, htr_outputs: list[str]) -> str:
        """
        Build merged_text per the data design doc:
            merged_text = Tesseract printed-text output + HTR transcriptions.

        htr_outputs should be the decoded text for each region in self.regions,
        in the same order (typically emitted by the HTR preprocessing consumer
        after calling /predict/htr for each region).

        Returns a single string ready to be chunked and indexed.
        """
        base = (self.tesseract_text or "").strip()
        htr_clean = [t.strip() for t in htr_outputs if t and t.strip()]
        if not htr_clean:
            return base
        htr_block = "[HANDWRITTEN]\n" + "\n".join(htr_clean)
        return f"{base}\n\n{htr_block}" if base else htr_block


class RegionSlicer:
    """
    Fetches a PDF from Paperless, converts pages to images, detects
    handwritten regions, crops them, and uploads crops to MinIO.
    """

    def __init__(
        self,
        paperless_url: str = None,
        paperless_token: str = None,
        minio_endpoint: str = None,
        minio_access_key: str = None,
        minio_secret_key: str = None,
        minio_bucket: str = None,
        dpi: int = 200,
    ):
        self.paperless_url = (paperless_url or os.environ.get("PAPERLESS_URL", "http://paperless-webserver-1:8000")).rstrip("/")
        self.paperless_token = paperless_token or os.environ.get("PAPERLESS_TOKEN", "")
        self.minio_endpoint = minio_endpoint or os.environ.get("MINIO_ENDPOINT", "minio:9000")
        self.minio_access_key = minio_access_key or os.environ.get("MINIO_ACCESS_KEY", "admin")
        self.minio_secret_key = minio_secret_key or os.environ.get("MINIO_SECRET_KEY", "paperless_minio")
        self.minio_bucket = minio_bucket or os.environ.get("MINIO_BUCKET", "paperless-images")
        self.dpi = dpi

        self.minio_client = Minio(
            self.minio_endpoint,
            access_key=self.minio_access_key,
            secret_key=self.minio_secret_key,
            secure=False,
        )
        # Ensure bucket exists
        if not self.minio_client.bucket_exists(self.minio_bucket):
            self.minio_client.make_bucket(self.minio_bucket)
            log.info("Created MinIO bucket: %s", self.minio_bucket)

    def _paperless_headers(self) -> dict:
        headers = {}
        if self.paperless_token:
            headers["Authorization"] = f"Token {self.paperless_token}"
        return headers

    def fetch_document_metadata(self, doc_id: int) -> dict:
        """Fetch document metadata from Paperless REST API."""
        url = f"{self.paperless_url}/api/documents/{doc_id}/"
        resp = requests.get(url, headers=self._paperless_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def fetch_document_file(self, doc_id: int) -> tuple[bytes, str]:
        """
        Download the document file from Paperless.
        Returns (file_bytes, content_type).
        """
        url = f"{self.paperless_url}/api/documents/{doc_id}/download/"
        resp = requests.get(url, headers=self._paperless_headers(), timeout=60)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        return resp.content, content_type

    def file_to_pages(self, file_bytes: bytes, content_type: str) -> list[Image.Image]:
        """
        Convert a document file to a list of PIL Images.
        Handles PDFs (via pdf2image/poppler) and images (JPEG, PNG, TIFF, etc.).
        """
        ct = content_type.lower()

        # Image files → single "page"
        if ct.startswith("image/") or ct in ("application/octet-stream",):
            try:
                img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
                log.info("Opened image file as single page (%dx%d, %s)", img.width, img.height, ct)
                return [img]
            except Exception:
                pass  # fall through to PDF path

        # PDF files → multiple pages
        if ct == "application/pdf" or file_bytes[:5] == b"%PDF-":
            pages = convert_from_bytes(file_bytes, dpi=self.dpi)
            log.info("Converted PDF to %d page image(s) at %d DPI", len(pages), self.dpi)
            return pages

        # Unknown type — try as image first, then PDF
        try:
            img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
            log.info("Opened unknown file type as image (%dx%d)", img.width, img.height)
            return [img]
        except Exception:
            pages = convert_from_bytes(file_bytes, dpi=self.dpi)
            log.info("Converted unknown file type as PDF to %d page(s)", len(pages))
            return pages

    def upload_crop(self, crop: Image.Image, doc_id: int, page_num: int, region_idx: int) -> str:
        """
        Upload a cropped region image to MinIO.
        Returns the s3:// URL for the stored crop.
        """
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        buf.seek(0)
        size = buf.getbuffer().nbytes

        obj_path = f"documents/{doc_id}/regions/p{page_num}_r{region_idx}.png"
        self.minio_client.put_object(
            self.minio_bucket,
            obj_path,
            buf,
            length=size,
            content_type="image/png",
        )
        s3_url = f"s3://{self.minio_bucket}/{obj_path}"
        log.debug("Uploaded crop: %s (%d bytes)", s3_url, size)
        return s3_url

    def upload_page_image(self, page_image: Image.Image, doc_id: int, page_num: int) -> str:
        """Upload the full page image to MinIO for reference."""
        buf = io.BytesIO()
        page_image.save(buf, format="PNG")
        buf.seek(0)
        size = buf.getbuffer().nbytes

        obj_path = f"documents/{doc_id}/pages/p{page_num}.png"
        self.minio_client.put_object(
            self.minio_bucket,
            obj_path,
            buf,
            length=size,
            content_type="image/png",
        )
        return f"s3://{self.minio_bucket}/{obj_path}"

    def process_document(self, doc_id: int) -> SlicerResult:
        """
        Full pipeline: fetch PDF → pages → detect → crop → upload.

        Returns a SlicerResult with metadata about every detected region
        and where its crop is stored in MinIO.
        """
        log.info("Processing document %d ...", doc_id)

        # 1. Fetch metadata (including Tesseract OCR content)
        meta = self.fetch_document_metadata(doc_id)
        title = meta.get("title", f"document_{doc_id}")
        tesseract_text = meta.get("content", "") or ""
        log.info("  Title: %s", title)
        log.info("  Tesseract text: %d chars", len(tesseract_text))

        # 2. Download file (PDF or image)
        file_bytes, content_type = self.fetch_document_file(doc_id)
        log.info("  Downloaded %d bytes (%s)", len(file_bytes), content_type)

        # 3. Convert to page images
        pages = self.file_to_pages(file_bytes, content_type)

        result = SlicerResult(
            paperless_doc_id=doc_id,
            title=title,
            total_pages=len(pages),
            tesseract_text=tesseract_text,
        )

        # 4. Process each page
        for page_num, page_image in enumerate(pages, start=1):
            log.info("  Page %d/%d (%dx%d) ...", page_num, len(pages), page_image.width, page_image.height)

            # Upload full page image
            self.upload_page_image(page_image, doc_id, page_num)

            # Detect regions
            regions = detect_regions(page_image)
            log.info("    %d region(s) detected", len(regions))

            # Crop, upload, and collect results
            for idx, region in enumerate(regions):
                crop = crop_region(page_image, region["bbox"])
                crop_url = self.upload_crop(crop, doc_id, page_num, idx)

                region_id = str(uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"{doc_id}:{page_num}:{idx}"
                ))

                result.regions.append(SlicedRegion(
                    page_number=page_num,
                    region_index=idx,
                    bbox=region["bbox"],
                    width=region["width"],
                    height=region["height"],
                    crop_s3_url=crop_url,
                    region_id=region_id,
                ))

        log.info(result.summary())
        return result
