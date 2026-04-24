"""
Search bot.

Picks a query from a curated pool, calls ml_gateway's /predict/search,
then writes a query_sessions row (so the reranker's impression-count
math works) and feedback rows based on each result's relevance to the
query.

Why write query_sessions directly rather than via the search endpoint:
The current /api/ml/search/feedback/ Django view upserts query_sessions
with result_doc_ids = ARRAY[]::uuid[], which breaks the
search_feedback_rerank DAG's impression-counting UNNEST. The bot closes
this gap by inserting a properly-populated session row itself — which
is also what a well-designed UI session-creation flow would do.

Relevance heuristic: each query in config.QUERY_POOL has a set of
keywords. For each search result, if any keyword appears in the
result's chunk_text snippet, the result is "relevant" → the bot uses
the P_FEEDBACK_RELEVANT table; otherwise P_FEEDBACK_IRRELEVANT. This
gives the reranker a stable target to learn: over many emulator runs,
relevant docs accumulate positive feedback and rise in rank.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass, field

import psycopg2
import psycopg2.extras
import requests

from config import (
    DB_DSN,
    ML_GATEWAY_URL,
    P_FEEDBACK_IRRELEVANT,
    P_FEEDBACK_RELEVANT,
    QUERY_POOL,
    SEARCH_K,
    SEARCHES_PER_MIN,
)

log = logging.getLogger("search_bot")


# ── SQL ─────────────────────────────────────────────────────────────────

# Upsert session with populated result_doc_ids — the critical bit the UI
# doesn't (yet) do correctly. ON CONFLICT DO NOTHING so retries are idempotent.
UPSERT_SESSION_SQL = """
INSERT INTO query_sessions (id, query_text, user_id, is_test_account, result_doc_ids)
VALUES (%s, %s, NULL, TRUE, %s::uuid[])
ON CONFLICT (id) DO NOTHING;
"""

INSERT_FEEDBACK_SQL = """
INSERT INTO search_feedback (id, session_id, document_id, feedback_type)
VALUES (%s, %s, %s, %s);
"""


# ── Feedback decision ────────────────────────────────────────────────────

def _pick_feedback(prob_table: dict[str, float]) -> str:
    """Return one of: thumbs_up, thumbs_down, click, ignore."""
    r = random.random()
    cum = 0.0
    for action, p in prob_table.items():
        cum += p
        if r < cum:
            return action
    return "ignore"


def _is_relevant(snippet: str, keywords: list[str]) -> bool:
    s = (snippet or "").lower()
    return any(k.lower() in s for k in keywords)


# ── Main loop ────────────────────────────────────────────────────────────

@dataclass
class Stats:
    searches:          int = 0
    results_inspected: int = 0
    feedback_sent:     int = 0
    by_action:         dict[str, int] = field(default_factory=lambda: {
        "thumbs_up": 0, "thumbs_down": 0, "click": 0, "ignore": 0,
    })
    errors:            int = 0


async def run(stats: Stats) -> None:
    if SEARCHES_PER_MIN <= 0:
        log.info("SEARCHES_PER_MIN is 0 — search bot disabled")
        while True:
            await asyncio.sleep(3600)

    interval_s = 60.0 / SEARCHES_PER_MIN
    log.info("search_bot: %.2f search/min (interval=%.1fs)", SEARCHES_PER_MIN, interval_s)

    http = requests.Session()

    while True:
        try:
            await asyncio.to_thread(_tick, http, stats)
        except Exception as exc:
            stats.errors += 1
            log.exception("search tick failed: %s", exc)
            await asyncio.sleep(10)
            continue

        await asyncio.sleep(interval_s)


def _tick(http: requests.Session, stats: Stats) -> None:
    spec = random.choice(QUERY_POOL)
    query = spec["query"]
    keywords = spec["keywords"]

    # 1. Hit ml_gateway /predict/search. The endpoint accepts both the
    # new (query_text, top_k) and legacy (query, k) field names — we use
    # the new shape for clarity.
    try:
        resp = http.post(
            f"{ML_GATEWAY_URL}/predict/search",
            json={"query_text": query, "top_k": SEARCH_K},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("ml_gateway search failed for %r: %s", query, exc)
        stats.errors += 1
        return

    results = resp.json().get("results", [])
    if not results:
        log.info("no results for %r — skipping feedback", query)
        return

    stats.searches += 1

    # 2. Dedupe document_ids (a doc can span multiple chunks, each a
    # separate hit) and drop anything missing a doc_id.
    unique_doc_ids: list[str] = []
    seen: set[str] = set()
    by_doc: dict[str, dict] = {}
    for r in results:
        doc_id = r.get("document_id")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        unique_doc_ids.append(doc_id)
        by_doc[doc_id] = r

    if not unique_doc_ids:
        log.info("results had no document_id — skipping feedback")
        return

    session_id = str(uuid.uuid4())

    # 3. Decide feedback per result BEFORE writing anything — this way
    # if the DB connection fails, we don't end up with a half-filled
    # session.
    actions: list[tuple[str, str]] = []  # (document_id, feedback_type)
    for doc_id in unique_doc_ids:
        snippet = by_doc[doc_id].get("chunk_text") or by_doc[doc_id].get("snippet") or ""
        prob_table = (
            P_FEEDBACK_RELEVANT if _is_relevant(snippet, keywords)
            else P_FEEDBACK_IRRELEVANT
        )
        action = _pick_feedback(prob_table)
        stats.by_action[action] += 1
        stats.results_inspected += 1
        if action != "ignore":
            actions.append((doc_id, action))

    # 4. Write session + feedback atomically.
    try:
        with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
            cur.execute(UPSERT_SESSION_SQL, (session_id, query, unique_doc_ids))
            for doc_id, action in actions:
                cur.execute(INSERT_FEEDBACK_SQL, (str(uuid.uuid4()), session_id, doc_id, action))
            conn.commit()
    except psycopg2.Error as exc:
        log.warning("session/feedback write failed: %s", exc)
        stats.errors += 1
        return

    stats.feedback_sent += len(actions)
    log.info(
        "query=%r results=%d feedback=%d (%s)",
        query, len(unique_doc_ids), len(actions),
        ",".join(f"{d[:8]}={a}" for d, a in actions),
    )
