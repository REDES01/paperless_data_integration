"""
Search feedback aggregation.

Populates the document_feedback_stats table from search_feedback +
query_sessions. Invoked hourly by the airflow `search_feedback_rerank` DAG.

Single-table output, TRUNCATE + INSERT pattern so results are always
current and nothing can get out of sync.

Algorithm:
  For each document_id:
    - Count thumbs_up events    (search_feedback.feedback_type = 'thumbs_up')
    - Count thumbs_down events  (search_feedback.feedback_type = 'thumbs_down')
    - Count click events        (search_feedback.feedback_type = 'click')
    - Count total impressions   (N times the doc appears in query_sessions.result_doc_ids[])
    - up_rate   = thumbs_up   / total_impressions  (0 if no impressions)
    - down_rate = thumbs_down / total_impressions

Used at query time by ml_gateway to apply:
    final_score = cosine_similarity * (1 + alpha_up*up_rate - alpha_down*down_rate)

No-feedback documents (total_impressions == 0) are skipped — reranker
falls back to pure cosine for them, which is the correct default.
"""

import logging
import os

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.getenv(
    "DB_DSN",
    "host=postgres dbname=paperless user=user password=paperless_postgres",
)

# Single statement: SELECT aggregate → INSERT into stats table.
# UNNEST(result_doc_ids) explodes the array so we can count per-doc impressions.
AGGREGATE_SQL = """
WITH impressions AS (
    -- One row per (document_id, session_id) where the doc was shown
    SELECT DISTINCT
        unnest(result_doc_ids)::uuid AS document_id,
        id                           AS session_id
    FROM query_sessions
),
impression_counts AS (
    SELECT document_id, COUNT(*) AS total_impressions
    FROM impressions
    GROUP BY document_id
),
feedback_counts AS (
    SELECT
        document_id,
        SUM(CASE WHEN feedback_type = 'thumbs_up'   THEN 1 ELSE 0 END) AS thumbs_up,
        SUM(CASE WHEN feedback_type = 'thumbs_down' THEN 1 ELSE 0 END) AS thumbs_down,
        SUM(CASE WHEN feedback_type = 'click'       THEN 1 ELSE 0 END) AS clicks
    FROM search_feedback
    GROUP BY document_id
)
INSERT INTO document_feedback_stats
    (document_id, thumbs_up, thumbs_down, clicks, total_impressions, up_rate, down_rate, computed_at)
SELECT
    ic.document_id,
    COALESCE(fc.thumbs_up,   0) AS thumbs_up,
    COALESCE(fc.thumbs_down, 0) AS thumbs_down,
    COALESCE(fc.clicks,      0) AS clicks,
    ic.total_impressions,
    CASE WHEN ic.total_impressions > 0
         THEN COALESCE(fc.thumbs_up,   0)::float / ic.total_impressions
         ELSE 0.0 END AS up_rate,
    CASE WHEN ic.total_impressions > 0
         THEN COALESCE(fc.thumbs_down, 0)::float / ic.total_impressions
         ELSE 0.0 END AS down_rate,
    NOW() AS computed_at
FROM impression_counts ic
LEFT JOIN feedback_counts fc USING (document_id)
WHERE ic.document_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM documents d WHERE d.id = ic.document_id AND d.deleted_at IS NULL);
"""


def main():
    log.info("aggregating search feedback into document_feedback_stats...")
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            # Full refresh — stats are always derivable from source tables
            cur.execute("TRUNCATE document_feedback_stats")
            cur.execute(AGGREGATE_SQL)
            conn.commit()

            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE thumbs_up   > 0) AS has_up,
                       COUNT(*) FILTER (WHERE thumbs_down > 0) AS has_down,
                       ROUND(AVG(up_rate)::numeric,   3) AS avg_up_rate,
                       ROUND(AVG(down_rate)::numeric, 3) AS avg_down_rate
                FROM document_feedback_stats
            """)
            total, has_up, has_down, avg_up, avg_down = cur.fetchone()
            log.info(
                "wrote %d doc stats rows (%d with thumbs_up, %d with thumbs_down, "
                "avg up_rate=%s, avg down_rate=%s)",
                total, has_up, has_down, avg_up, avg_down,
            )


if __name__ == "__main__":
    main()
