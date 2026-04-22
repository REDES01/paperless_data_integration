"""
Training data loaders for HTR fine-tuning.

Two data sources, unified behind one interface:

  1. IAM parquet shards  (s3://paperless-datalake/warehouse/iam_dataset/)
     Schema: image_id, image_png (bytes), transcription, split
     Used by: baseline_pretrained (val only), finetune_iam, finetune_combined

  2. HTR correction snapshot (s3://paperless-datalake/warehouse/htr_training/v_<ts>/)
     Schema: region_id, crop_s3_url, corrected_text, original_text, ...
     Used by: finetune_corrections, finetune_combined

Both adapt to the same (image_bytes, text) pairs that TrOCR expects.

The IAM val split is the evaluation benchmark for all candidates — CER/WER
on a frozen held-out set is the single quality metric across the candidate
table. This is what makes runs comparable.
"""
from __future__ import annotations

import io
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyarrow.parquet as pq
from minio import Minio
from PIL import Image

log = logging.getLogger(__name__)


@dataclass
class Example:
    """One (image, text) training pair."""
    image: Image.Image
    text: str
    source: str    # "iam" or "corrections"
    key: str       # IAM image_id, or snapshot region_id


# ── MinIO helpers ─────────────────────────────────────────────────

def _minio(endpoint: str, access: str, secret: str, secure: bool = False) -> Minio:
    return Minio(endpoint, access_key=access, secret_key=secret, secure=secure)


def _list_parquet_shards(mc: Minio, bucket: str, prefix: str) -> list[str]:
    """Return all parquet object names under prefix/."""
    out = []
    for obj in mc.list_objects(bucket, prefix=prefix.rstrip("/") + "/", recursive=True):
        if obj.object_name.endswith(".parquet"):
            out.append(obj.object_name)
    return sorted(out)


def _read_parquet_from_minio(mc: Minio, bucket: str, obj_name: str) -> "pa.Table":
    """Download a parquet shard and return as a PyArrow table."""
    import pyarrow as pa
    resp = mc.get_object(bucket, obj_name)
    try:
        raw = resp.read()
    finally:
        resp.close()
        resp.release_conn()
    return pq.read_table(io.BytesIO(raw))


def _fetch_crop_bytes(mc: Minio, s3_url: str) -> bytes:
    """Given 's3://<bucket>/<key>', return raw bytes."""
    if not s3_url.startswith("s3://"):
        raise ValueError(f"not an s3 URL: {s3_url!r}")
    rest = s3_url[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not key:
        raise ValueError(f"s3 URL missing key: {s3_url!r}")
    resp = mc.get_object(bucket, key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


# ── IAM reader ────────────────────────────────────────────────────

def load_iam_examples(
    mc: Minio,
    split: str,
    max_examples: int | None = None,
    seed: int = 42,
    bucket: str = "paperless-datalake",
    prefix: str = "warehouse/iam_dataset",
) -> list[Example]:
    """
    Load up to `max_examples` from the IAM Parquet shards for a given split.
    Samples uniformly across shards with a deterministic seed.
    """
    rng = random.Random(seed)
    split_prefix = f"{prefix}/{split}"
    shards = _list_parquet_shards(mc, bucket, split_prefix)
    if not shards:
        raise RuntimeError(f"no IAM shards under s3://{bucket}/{split_prefix}/")
    log.info("IAM %s: found %d shards", split, len(shards))

    examples: list[Example] = []
    target = max_examples if max_examples is not None else float("inf")

    for shard in shards:
        if len(examples) >= target:
            break
        table = _read_parquet_from_minio(mc, bucket, shard)
        image_ids = table.column("image_id").to_pylist()
        image_pngs = table.column("image_png").to_pylist()
        transcriptions = table.column("transcription").to_pylist()

        # Shuffle indices within the shard so we don't bias toward early rows
        indices = list(range(len(image_ids)))
        rng.shuffle(indices)

        for i in indices:
            if len(examples) >= target:
                break
            txt = (transcriptions[i] or "").strip()
            if not txt:
                continue
            try:
                img = Image.open(io.BytesIO(image_pngs[i])).convert("RGB")
            except Exception as exc:
                log.warning("IAM image decode failed for %s: %s", image_ids[i], exc)
                continue
            examples.append(Example(image=img, text=txt, source="iam", key=image_ids[i]))

    log.info("IAM %s: loaded %d examples", split, len(examples))
    return examples


# ── Snapshot (user corrections) reader ────────────────────────────

def load_correction_examples(
    mc: Minio,
    version: str,
    split: str = "train",
    max_examples: int | None = None,
    bucket: str = "paperless-datalake",
    prefix: str = "warehouse/htr_training",
) -> list[Example]:
    """
    Load corrections from a specific snapshot version.

    Args:
        version: e.g., "v_20260420T153045Z" or "latest" (we resolve that).
        split:   "train" or "val" — matches batch_htr.py's directory layout.
    """
    if version == "latest":
        version = _resolve_latest_snapshot(mc, bucket, prefix)
        log.info("resolved 'latest' snapshot: %s", version)

    shard_prefix = f"{prefix}/{version}/{split}"
    shards = _list_parquet_shards(mc, bucket, shard_prefix)
    if not shards:
        log.warning("no correction shards under s3://%s/%s/ (returning empty)",
                    bucket, shard_prefix)
        return []
    log.info("corrections %s/%s: found %d shards", version, split, len(shards))

    examples: list[Example] = []
    target = max_examples if max_examples is not None else float("inf")

    for shard in shards:
        if len(examples) >= target:
            break
        table = _read_parquet_from_minio(mc, bucket, shard)
        region_ids = table.column("region_id").to_pylist()
        crop_urls = table.column("crop_s3_url").to_pylist()
        texts = table.column("corrected_text").to_pylist()

        for region_id, url, text in zip(region_ids, crop_urls, texts):
            if len(examples) >= target:
                break
            txt = (text or "").strip()
            if not txt:
                continue
            try:
                img_bytes = _fetch_crop_bytes(mc, url)
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            except Exception as exc:
                log.warning("correction crop fetch failed for %s (%s): %s",
                            region_id, url, exc)
                continue
            examples.append(Example(image=img, text=txt, source="corrections",
                                    key=region_id))

    log.info("corrections %s/%s: loaded %d examples", version, split, len(examples))
    return examples


def _resolve_latest_snapshot(mc: Minio, bucket: str, prefix: str) -> str:
    """Find the most recent v_<timestamp> directory under prefix/."""
    versions = set()
    for obj in mc.list_objects(bucket, prefix=prefix.rstrip("/") + "/", recursive=True):
        parts = obj.object_name.split("/")
        # expect .../htr_training/v_<ts>/<split>/<file>.parquet
        for part in parts:
            if part.startswith("v_"):
                versions.add(part)
                break
    if not versions:
        raise RuntimeError(f"no snapshots under s3://{bucket}/{prefix}/")
    return sorted(versions)[-1]


# ── Unified loader ────────────────────────────────────────────────

def load_training_examples(
    mc: Minio,
    sources: list[str],
    max_per_source: int | None = None,
    snapshot_version: str = "latest",
    iam_split: str = "train",
    seed: int = 42,
) -> list[Example]:
    """
    Given a list like ["iam"] or ["corrections"] or ["iam", "corrections"],
    return the concatenated training examples.

    Returns an empty list rather than raising if "corrections" is requested
    but no snapshot exists yet (new system, no user corrections yet). This
    lets the "finetune_corrections" candidate fail gracefully at quality-gate
    time rather than at data-load time.
    """
    combined: list[Example] = []
    for src in sources:
        if src == "iam":
            combined.extend(load_iam_examples(
                mc, split=iam_split, max_examples=max_per_source, seed=seed,
            ))
        elif src == "corrections":
            combined.extend(load_correction_examples(
                mc, version=snapshot_version, split="train",
                max_examples=max_per_source,
            ))
        else:
            raise ValueError(f"unknown training source: {src!r}")

    random.Random(seed).shuffle(combined)
    log.info("TOTAL training examples: %d (from %d sources)", len(combined), len(sources))
    return combined
