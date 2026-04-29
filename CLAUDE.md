# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`paperless_data_integration` is the **deployment/integration layer** that glues three repos into a single runnable system on a shared Docker network. It is **not** the source of truth for the UI or the data platform — those live in sibling clones:

```
<workspace>/
├── paperless_data/               (REDES01/paperless_data — postgres, minio, redpanda, qdrant)
├── paperless-ngx-fork/           (Paperless-ngx fork with HTR review + semantic search UI)
└── paperless_data_integration/   (this repo)
```

The Path A integration variant (see `docs/PATH_A_INTEGRATION.md`) instead pairs this repo with `Palomarr/paperless-ml` (an integrated Paperless+ml-gateway stack) and uses `paperless_data` only for the HTR persistence Postgres. The two layouts coexist in this codebase — `htr_consumer/` lives on Path A, while `overrides/`, `paperless/`, `Makefile` remain wired for the original Phase 0 pattern.

## Bringing the stack up

The canonical "everything from cold" entry point is `scripts/up_all.sh`. It does the 8 steps in order with health-checks between them. For partial bring-up, the Makefile covers the network + the two original compose stacks only:

```bash
make up                                     # network + paperless_data + paperless (Phase 0 only)
make verify                                 # cross-stack DNS check from inside paperless-webserver-1
make down

PAPERLESS_TOKEN=<tok> bash scripts/up_all.sh   # full stack incl. ml_gateway, indexer, monitor, consumer
bash scripts/up_airflow.sh                     # Airflow on top of the data stack (auto-builds htr_trainer + htr_batch)
```

`PAPERLESS_TOKEN` is required by `htr_consumer` at runtime — it exits if unset. Get one from inside `paperless-webserver-1` (see `region_slicer/README.md` for the `manage.py shell` snippet) or follow the notebook step 16.

Each service is a self-contained `docker compose -p <project> -f <service>/compose.yml up -d --build` and can be brought up independently. They all share the **external** `paperless_ml_net` bridge, which `scripts/create_network.sh` creates idempotently. The `ml_gateway_model_registry` external volume is also created up-front by `up_all.sh`.

## Cross-service architecture

End-to-end flow on every Paperless upload:

```
Paperless UI upload
  └─▶ paperless.uploads (Redpanda topic)
        ├─▶ htr_consumer ──▶ region_slicer ──▶ MinIO crops
        │     ├─▶ ml_gateway POST /predict/htr      (or /htr on paperless-ml)
        │     ├─▶ drift_monitor POST /drift/check   (fire-and-forget, 2s timeout)
        │     └─▶ Postgres: documents / document_pages / handwritten_regions
        │           └─ on documents.merged_text write...
        └─▶ qdrant_indexer ──▶ poll merged_text ──▶ chunk + mpnet ──▶ Qdrant document_chunks

User corrects regions in Paperless /ml/htr-review
  └─▶ Django INSERT INTO htr_corrections
        └─▶ Airflow htr_retraining DAG (daily 02:00 UTC):
              build_snapshot (htr_batch image) ──▶ parquet shards on MinIO
              finetune_combined_stage1 (htr_trainer image) ──▶ MLflow "htr" registry on quality-gate pass
              notify_result ──▶ prints `scripts/deploy_model.sh <ver>` for the operator

Operator runs deploy_model.sh
  └─▶ writes models:/htr/<ver> URI to ml_gateway_model_registry volume
  └─▶ docker kill --signal=HUP ml_gateway
        └─▶ gateway reloads HTR model from MLflow URI

Drift alert (Alertmanager rule firing on drift_events_total)
  └─▶ rollback_ctrl webhook
        └─▶ rewrites current_htr.txt to the previous version + SIGHUPs ml_gateway
```

Two important non-obvious invariants:

- **`htr_consumer` writes `merged_text` in a SECOND transaction**, after the per-region HTR calls finish. `qdrant_indexer` polls for it with `MERGED_TEXT_POLL_SECONDS` (default 120s) — a race window that is intentional, not a bug.
- **The behavior_emulator writes directly to Postgres**, not through Paperless's REST API. This is deliberate: (1) skips session-auth dance, (2) the `/api/ml/search/feedback/` view writes empty `result_doc_ids`, which would break `search_feedback_rerank`'s impression-count math. See `behavior_emulator/README.md`.

## Service map

| Service / dir          | Role                                                                                |
|------------------------|-------------------------------------------------------------------------------------|
| `htr_consumer/`        | Long-lived Kafka consumer on `paperless.uploads` → slice → HTR → write to Postgres  |
| `region_slicer/`       | Handwritten-region detection (vendored into `htr_consumer` image at build time)     |
| `ml_gateway/`          | FastAPI: `/predict/htr` (TrOCR) + `/predict/search` (mpnet → Qdrant + feedback rerank) |
| `qdrant_indexer/`      | Kafka consumer that chunks `merged_text` → mpnet embeddings → Qdrant                |
| `drift_monitor/`       | FastAPI MMD detector pre-fit on 500 IAM crops; exposes `/metrics` for Prometheus    |
| `training/`            | TrOCR fine-tuning trainer + 12 candidate YAMLs in `configs/`                        |
| `airflow/`             | LocalExecutor + DockerOperator DAGs (`htr_retraining`, `archive_corrections`, `search_feedback_rerank`) |
| `observability/`       | Prometheus + Grafana + Alertmanager + MLflow + `rollback_ctrl`                      |
| `behavior_emulator/`   | Synthesizes correction + search-feedback traffic to exercise feedback loops         |
| `search_reranker/`     | One-off feedback aggregation script (the recurring version is the airflow DAG)     |
| `overrides/`           | Phase-0-style network overlays for the two sibling compose stacks                  |
| `paperless/`           | Compose that builds Paperless from `../../paperless-ngx-fork`                       |
| `seed/`                | SQL: `phase1_demo_seed.sql`, `phase2_add_paperless_doc_id.sql`                     |
| `provision_chameleon.ipynb` | Reserve VM + clone all three repos + bring stack up on Chameleon               |

## Failure-handling conventions

These are intentional, not accidental — don't "fix" them without thinking:

- **htr_consumer**: processing error → log + commit offset + move on (no DLQ). Per-region HTR failure → write empty output and flag region for review. Broker unavailable on startup → retry every 5s forever. Defaults `KAFKA_MAX_POLL_INTERVAL_MS=1800000` and `KAFKA_MAX_POLL_RECORDS=1` are sized for multi-hundred-page documents — shrinking them re-introduces the rebalance/reprocessing loop the milestone fix solved.
- **ml_gateway**: fail-loud on model load. A gateway returning empty strings is worse than one that's down — let docker-compose restart it.
- **drift_monitor**: called fire-and-forget by `htr_consumer` with a 2s timeout. A slow/down monitor must never stall HTR processing.
- **training**: the **first** run on a fresh MLflow tagged `role: baseline` establishes the reference CER. Subsequent candidates only register to MLflow if `val_cer <= baseline_cer * gate_tolerance` (default 1.05). Gate failures exit 1; this is expected and is what makes the registry safe to deploy from.
- **printed-text filter** (`region_slicer/printed_filter.py`): if the archive PDF is missing, fall back to passing all candidate regions through (logs "No archive PDF — printed-filter disabled"). Tunable via `PRINTED_COVERAGE_THRESHOLD = 0.25`.

## Defaults you'll keep tripping over

- MinIO credentials differ by topology: **original** stack uses `admin`/`paperless_minio`; **paperless-ml** Path A uses `minioadmin`/`minioadmin`. The compose files set the right one per service; preserve this when copying envs.
- HTR endpoint differs by topology: original = `/predict/htr`, paperless-ml = `/htr` (set via `HTR_ENDPOINT` env var on `htr_consumer`).
- Postgres is at `postgres:5432`, db `paperless`, user `user`, password `paperless_postgres`.
- mpnet vector dim is 768 — `qdrant_indexer` and `ml_gateway` MUST stay on the same `RETR_MODEL_NAME` or vectors won't line up.
- Behavior emulator rate is `BE_MODE`: `off`/`slow`/`normal`/`demo` (0×/0.1×/1×/10×).
- On Windows hosts: TCP ranges 8911–9010 and 50000–50059 are reserved for Hyper-V — MinIO is shipped on `19000`/`19001` to avoid the 9000 conflict.

## Working with the Airflow DAGs

DAGs are hot-reloaded every 30s — edit `airflow/dags/*.py` and they'll pick up. Tasks spawn fresh `htr_batch` / `htr_trainer` containers via the `DockerOperator` against the host docker socket (`/var/run/docker.sock` is mounted, scheduler runs as root). Per-task logs are visible in the Airflow UI at `http://<vm>:8080` (admin/admin).

To trigger manually: UI ▶️ button, or `POST /api/v1/dags/<dag_id>/dagRuns` with basic auth `admin:admin`.

## Running training candidates

```bash
# Build trainer image (first time ~10 min)
docker compose -p training -f training/compose.yml build baseline

# Run a single candidate
docker compose -p training -f training/compose.yml run --rm finetune_combined_stage1

# Run the full sweep — produces the candidate table in MLflow
bash scripts/run_all_candidates.sh
```

Baseline must run first on a fresh MLflow — it's what every later candidate's quality gate compares against.

## Promoting and rolling back models

```bash
bash scripts/deploy_model.sh           # latest registered "htr" version
bash scripts/deploy_model.sh 7         # specific version
```

Writes `models:/htr/<n>` to `current_htr.txt` on the `ml_gateway_model_registry` volume, then `docker kill --signal=HUP ml_gateway`. The gateway re-reads the file on SIGHUP and reloads from MLflow. The `rollback_ctrl` service in `observability/compose.yml` does the reverse automatically when an Alertmanager webhook fires (drift, error rate, etc.).

## Tearing things down

```bash
make down                                                       # original Phase 0 stacks only
bash scripts/down_all.sh                                        # everything started by up_all.sh
docker compose -p airflow -f airflow/compose.yml down [-v]      # Airflow (-v wipes its postgres + logs)
```

`make clean` additionally removes the shared network. Don't run it while any service is still attached.
