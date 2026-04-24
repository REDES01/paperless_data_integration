"""
Correction bot.

Polls the ML Postgres for regions that are flagged for review and have no
correction yet. For each region, picks a persona, generates a plausible
"user correction" by perturbing the HTR output, and writes it directly to
htr_corrections.

Rationale for direct DB writes (rather than POSTing to /api/ml/htr/corrections/):
- The Django endpoint just runs the same INSERT we run here
- Avoids Paperless session auth complexity (bot has no browser session)
- The downstream pipeline (archive_corrections → build_snapshot → training)
  reads the htr_corrections table regardless of write origin

Persona-based perturbation:
- Careful: fix one or two common HTR artifacts (duplicate runs,
  trailing garbage, whitespace collapse). Produces mostly-correct text.
- Sloppy: apply only one fix, leaving some errors. Half the time ends up
  identical to original → quality filter rejects as no-op.
- Lazy: no edit at all. Always a no-op. Exercises the filter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import uuid
from dataclasses import dataclass

import psycopg2
import psycopg2.extras

from config import (
    CORRECTIONS_BATCH,
    CORRECTIONS_PER_MIN,
    DB_DSN,
    MIN_HTR_LEN,
    PERSONAS,
)

log = logging.getLogger("correction_bot")


# ── Fetch + insert SQL ───────────────────────────────────────────────────

FETCH_SQL = """
SELECT r.id           AS region_id,
       r.htr_output   AS original_text,
       d.id           AS document_id
FROM handwritten_regions r
JOIN document_pages  p  ON p.id = r.page_id
JOIN documents       d  ON d.id = p.document_id
WHERE p.htr_flagged = TRUE
  AND d.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM htr_corrections c WHERE c.region_id = r.id)
  AND COALESCE(length(r.htr_output), 0) >= %s
ORDER BY random()
LIMIT %s;
"""

INSERT_SQL = """
INSERT INTO htr_corrections (id, region_id, user_id, original_text, corrected_text, opted_in)
VALUES (%s, %s, NULL, %s, %s, TRUE);
"""


# ── Perturbation logic ───────────────────────────────────────────────────

# Common patterns seen in TrOCR output on messy handwriting:
#   "...ESESESES..." — runaway token repeats
#   "NOT NOT 12 ... s" — trailing garbage after real content
#   "  gets    squished  " — extra whitespace
# The "careful" user would clean these up.
_GARBAGE_TAIL = re.compile(r"[.…\s]*(?:[A-Z]{2,}\s+){2,}.*$")
_REPEAT_RUN   = re.compile(r"([A-Za-z])\1{3,}")
_MULTI_SPACE  = re.compile(r"\s{2,}")


def _clean_once(text: str) -> str:
    """Apply one plausible cleanup step. Returns the text unchanged if no
    pattern matches — caller can retry with a different rule."""
    for rule in (_apply_tail, _apply_repeat, _apply_whitespace):
        out = rule(text)
        if out != text:
            return out
    # Fallback: strip leading/trailing punctuation + whitespace
    out = text.strip(" .,;:-_\t\n")
    if out != text:
        return out
    return text


def _apply_tail(text: str) -> str:
    return _GARBAGE_TAIL.sub("", text).rstrip()


def _apply_repeat(text: str) -> str:
    # Collapse long same-char runs (NNNNN → NN)
    return _REPEAT_RUN.sub(lambda m: m.group(1) * 2, text)


def _apply_whitespace(text: str) -> str:
    return _MULTI_SPACE.sub(" ", text).strip()


def _pick_persona() -> str:
    r = random.random()
    running = 0.0
    for name, cfg in PERSONAS.items():
        running += cfg["weight"]
        if r < running:
            return name
    return "careful"


def _perturb(original: str) -> tuple[str, str]:
    """Return (corrected_text, persona_name). corrected_text may equal
    original (no-op) when the persona is lazy or a fix rule didn't apply."""
    persona = _pick_persona()
    cfg = PERSONAS[persona]

    # Lazy persona: straight no-op most of the time
    if random.random() < cfg["noop_rate"]:
        return original, persona

    # Other personas: try to apply a cleanup. fix_rate controls how many
    # passes we run (1 pass = often a single meaningful change).
    corrected = original
    passes = 2 if random.random() < cfg["fix_rate"] else 1
    for _ in range(passes):
        corrected = _clean_once(corrected)

    return corrected, persona


# ── Main loop ────────────────────────────────────────────────────────────

@dataclass
class Stats:
    attempted: int = 0
    inserted:  int = 0
    skipped:   int = 0   # no flagged regions to correct
    errors:    int = 0


async def run(stats: Stats) -> None:
    """Infinite loop; yields back to asyncio between iterations so the
    search bot can make progress on the same event loop."""
    if CORRECTIONS_PER_MIN <= 0:
        log.info("CORRECTIONS_PER_MIN is 0 — correction bot disabled")
        while True:
            await asyncio.sleep(3600)

    interval_s = 60.0 / CORRECTIONS_PER_MIN * CORRECTIONS_BATCH
    log.info(
        "correction_bot: %.2f corr/min (batch=%d, interval=%.1fs)",
        CORRECTIONS_PER_MIN, CORRECTIONS_BATCH, interval_s,
    )

    while True:
        try:
            n = await asyncio.to_thread(_tick, stats)
            if n == 0:
                # Nothing to correct yet — back off to 30s so we don't
                # hammer Postgres while the data generator is ramping up.
                await asyncio.sleep(30)
                continue
        except Exception as exc:
            stats.errors += 1
            log.exception("correction tick failed: %s", exc)
            await asyncio.sleep(10)
            continue

        await asyncio.sleep(interval_s)


def _tick(stats: Stats) -> int:
    """One iteration: fetch up to CORRECTIONS_BATCH flagged regions,
    perturb, write corrections. Returns number of inserts."""
    with psycopg2.connect(DB_DSN) as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(FETCH_SQL, (MIN_HTR_LEN, CORRECTIONS_BATCH))
        rows = cur.fetchall()
        if not rows:
            stats.skipped += 1
            return 0

        inserted = 0
        for row in rows:
            stats.attempted += 1
            corrected, persona = _perturb(row["original_text"] or "")
            try:
                cur.execute(
                    INSERT_SQL,
                    (
                        str(uuid.uuid4()),
                        row["region_id"],
                        row["original_text"],
                        corrected,
                    ),
                )
                inserted += 1
                log.info(
                    "[%s] correction on region=%s  '%s' -> '%s'",
                    persona,
                    str(row["region_id"])[:8],
                    (row["original_text"] or "")[:40],
                    corrected[:40],
                )
            except psycopg2.Error as exc:
                stats.errors += 1
                log.warning("insert failed for region=%s: %s", row["region_id"], exc)
                conn.rollback()
                continue

        conn.commit()
        stats.inserted += inserted
        return inserted
