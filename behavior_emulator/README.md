# Behavior Emulator

Synthesizes realistic user activity — corrections + search feedback —
to exercise the feedback loops in the ML platform unattended.

## What it emulates

**Corrections bot**
Polls `handwritten_regions` for flagged regions without corrections.
For each one, picks a persona (careful / sloppy / lazy) and generates a
plausible "user correction" by running the HTR output through one or
more cleanup rules (collapse character repeats, strip trailing garbage,
normalize whitespace). Writes to `htr_corrections` directly.

Lazy persona submits no-ops — these get rejected by `batch_htr.py`'s
R1 quality filter, which is a great demo beat: "not every user click
becomes training data."

**Search bot**
Picks a query from a curated pool (16 realistic queries with keyword
tags), hits `ml_gateway /predict/search`, then submits feedback based
on a relevance heuristic: if any keyword appears in the result
snippet, the bot biases toward thumbs-up; otherwise toward thumbs-down.

Crucially, the bot writes `query_sessions` rows with populated
`result_doc_ids` — without this, the
`search_feedback_rerank` DAG's impression-count UNNEST returns empty
and every doc's up_rate / down_rate stays at 0.

## Rates

Controlled by `BE_MODE` env var. Base rates are `3 corrections/min`
and `2 searches/min`. Multipliers:

| `BE_MODE` | Rate |                                                |
|-----------|------|------------------------------------------------|
| `off`     | 0×   | Bots stay alive but sleep. For pausing traffic. |
| `slow`    | 0.1× | Minimal load for long-duration stability runs. |
| `normal`  | 1×   | Default. Realistic. Can run for days.          |
| `demo`    | 10×  | Heavy traffic for ~10 min of video recording.  |

## Why direct DB writes (not POSTing to `/api/ml/*`)

The bot bypasses Paperless's Django endpoints and writes the same rows
the endpoints would write. Three reasons:

1. **Auth**: Django endpoints are `@login_required` (session-based).
   Running a session-login dance from a headless bot is fragile.
2. **`result_doc_ids`**: The `/api/ml/search/feedback/` view
   upserts `query_sessions` with an empty `result_doc_ids` array —
   breaks the reranker's impression-count math. The bot fixes this by
   writing session rows itself.
3. **Operational symmetry**: Downstream DAGs
   (`archive_corrections`, `search_feedback_rerank`, `htr_retraining`)
   read from the tables regardless of write origin. Same rows, same
   pipeline behavior.

## Running

```bash
cd ~/paperless_data_integration

# Normal unattended run
sg docker -c 'docker compose -p behavior_emulator -f behavior_emulator/compose.yml up -d --build'

# Demo-rate burst
sg docker -c 'BE_MODE=demo docker compose -p behavior_emulator -f behavior_emulator/compose.yml up -d --force-recreate'

# Watch what it's doing
sg docker -c 'docker logs -f behavior_emulator'

# Pause without tearing down
sg docker -c 'BE_MODE=off docker compose -p behavior_emulator -f behavior_emulator/compose.yml up -d --force-recreate'

# Stop
sg docker -c 'docker compose -p behavior_emulator -f behavior_emulator/compose.yml down'
```

## What to expect after an hour (normal mode)

- `htr_corrections`: ~180 rows, ~15% flagged as no-op by `batch_htr`
  quality filter on next DAG run
- `query_sessions`: ~120 rows, each with a populated `result_doc_ids`
- `search_feedback`: ~300 rows across thumbs_up / thumbs_down / click
- `document_feedback_stats` (after next hourly DAG run): per-doc
  up_rate / down_rate reflecting the relevance heuristic
- `rerank_events_total{outcome="boosted"}` and `{outcome="demoted"}`
  both > 0 in Prometheus

## Dependencies

- `postgres` container up on `paperless_ml_net` with the ML schema
  (including `archived_at` column from migration
  `04_add_corrections_archived_at.sql`)
- `ml_gateway` container up and reachable at `http://ml_gateway:8000`
- At least 1 document processed by `htr_consumer` (so there's
  something to correct / search against) — the bots back off when
  there's no work to do.

## Operational telemetry

Every 60s the supervisor prints a single JSON line prefixed `[STATUS]`.
Example:

```
[STATUS] {"ts":"2026-04-24T05:12:00Z","uptime_s":600,"mode":"normal",
 "corrections":{"attempted":30,"inserted":28,"skipped":2,"errors":0},
 "searches":{"searches":20,"feedback_sent":42,"by_action":{"thumbs_up":14,"thumbs_down":11,"click":3,"ignore":54},"errors":0}}
```

Grep for these in `docker logs` to see the activity curve over time.
