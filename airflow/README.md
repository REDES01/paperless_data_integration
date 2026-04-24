# Airflow

Scheduled orchestration for the HTR **closed-loop retraining** pipeline. Replaces
the earlier `training_scheduler` shell-loop with a real workflow engine that
visualizes each stage and captures failures cleanly.

## Quick start

```bash
# One-time: make sure the data stack is up and required trainer + batch images are built
cd ~/paperless_data_integration
bash scripts/up_paperless_data.sh
docker compose -p training -f training/compose.yml build        # builds htr_trainer:latest
docker build -t htr_batch:latest ~/paperless_data/batch_pipeline # builds htr_batch:latest

# Start Airflow (up_airflow.sh also auto-builds the two images above if missing)
bash scripts/up_airflow.sh
```

Then open http://your-vm:8080 — login `admin` / `admin`.

## DAGs

### `htr_retraining`

Closes the loop from UI correction → new model version.

**Task graph:**
```
build_snapshot  ──▶  finetune_combined_stage1  ──▶  notify_result
```

**`build_snapshot`** — image `htr_batch:latest`.
Reads `htr_corrections` from the ML Postgres. Applies quality filters
(`opted_in = true`, non-empty `corrected_text`, source = `user_upload`,
deduplicate by region). Splits via document-grouped sha256 hashing
(80/20) — no document appears in both train and val, preventing
writer-style leakage. Writes versioned parquet shards + manifest.json
to `s3://paperless-datalake/warehouse/htr_training/v_<timestamp>/`.
Exits cleanly with a warning when there are no corrections yet —
training then falls back to IAM-only.

**`finetune_combined_stage1`** — image `htr_trainer:latest`.
Reads IAM + the latest snapshot built above. Fine-tunes TrOCR-base-stage1.
Evaluates against a held-out val set. Registers as `htr` version in
MLflow if the quality gate passes (val CER below baseline). Exits 1 on
gate failure (expected outcome — caught by `notify_result`'s trigger_rule).

**`notify_result`** — PythonOperator, runs regardless of upstream status.
Queries MLflow for the latest registered `htr` version, prints the
`scripts/deploy_model.sh <N>` command the operator runs to promote it.

**Schedule:** daily at 02:00 UTC. Change the cron in
`airflow/dags/htr_training.py` — Airflow auto-reloads within 30s.

**Deployment stays manual** — the operator reviews MLflow metrics before
running `deploy_model.sh`. Matches real MLOps practice of decoupling
register from deploy.

## Architecture

```
Paperless-ngx /ml/htr-review UI
    └─ POST /api/ml/htr/corrections/
         └─ Django view: INSERT INTO htr_corrections (ML Postgres)
              └─ [sits here until next DAG run]

Airflow scheduler (daily at 02:00 UTC)
    └─ Reads /opt/airflow/dags/htr_training.py every 30s
    └─ Spawns fresh container per task via the docker socket mount:

          ┌──────────────────┐     ┌─────────────────────┐     ┌────────────┐
          │  htr_batch       │     │  htr_trainer        │     │  notify    │
          │  python          │ ==> │  Fine-tune          │ ==> │  Print     │
          │  batch_htr.py    │     │  TrOCR-base-stage1  │     │  deploy    │
          │                  │     │  on IAM + snapshot  │     │  command   │
          │  Reads:          │     │  Writes:            │     │            │
          │  htr_corrections │     │  MLflow registry    │     │  (always   │
          │  (ml_postgres)   │     │  'htr' model        │     │   runs)    │
          │  Writes:         │     │                     │     │            │
          │  parquet snapshot│     │                     │     │            │
          │  s3://paperless-│     │                     │     │            │
          │  datalake/...    │     │                     │     │            │
          └──────────────────┘     └─────────────────────┘     └────────────┘
```

Airflow scheduler runs as root (`user: "0:0"`) so it can read
`/var/run/docker.sock` without GID-specific mounting. Acceptable on a
single-tenant demo VM.

## Why DockerOperator everywhere

Each task spawns a fresh `htr_batch` or `htr_trainer` container that lives
and dies with the task. Benefits:

1. **No host-path dependency.** BashOperator calling `docker compose`
   would require mounting the repo dir into the scheduler, which is
   fragile across path changes.
2. **Clean task logs.** Each task's container stdout/stderr is linked
   directly from Airflow's UI Task → Logs tab.
3. **Fault isolation.** A failing `batch_htr` run doesn't pollute or
   lock the scheduler process.

## Closing the loop — step by step

1. Upload a document that contains handwriting via the Paperless UI.
2. Wait for `htr_consumer` to slice + transcribe it.
3. Open `/ml/htr-review`, correct the flagged regions, click Save on each.
4. Verify the correction landed:
   ```bash
   sg docker -c 'docker exec postgres psql -U user -d paperless -c "
   SELECT COUNT(*) FROM htr_corrections;"'
   ```
5. Trigger the DAG manually (or wait for 02:00 UTC).
6. Watch the `build_snapshot` task log for:
   ```
   Fetched N eligible corrections (after dedup + filtering)
   Document-grouped split: train=... val=...
   Uploaded warehouse/htr_training/v_<ts>/train/shard_0000.parquet
   ```
7. Watch the `finetune_combined_stage1` task — it will now include your
   corrections in the training set alongside IAM.
8. If the quality gate passes, watch `notify_result` print a `deploy_model.sh`
   command. Run that on the VM to promote the model to ml_gateway.

## Manual trigger

From the UI: navigate to `htr_retraining`, click ▶️ on the top right.

From the API:
```bash
curl -u admin:admin -X POST \
    http://localhost:8080/api/v1/dags/htr_retraining/dagRuns \
    -H "Content-Type: application/json" \
    -d '{"conf":{}}'
```

## Changing the schedule

Edit `dags/htr_training.py`:
```python
schedule="0 2 * * *"   # cron format; change this line
```
Save — Airflow reloads DAGs every 30s, no restart needed.

Common alternatives:
- `"@hourly"` — every hour
- `"*/15 * * * *"` — every 15 min (demo-friendly, watch multiple runs)
- `None` — only manual triggers

## Debugging a failing task

1. Airflow UI → `htr_retraining` → the failed run → task box → Logs tab
2. Scroll to the bottom for stdout + stderr from the task container
3. For the container itself (still running):
   ```bash
   docker ps --filter ancestor=htr_batch:latest
   docker ps --filter ancestor=htr_trainer:latest
   docker logs <container_id>
   ```

## Teardown

```bash
# Stop Airflow, keep its data (DAG history, logs)
docker compose -p airflow -f airflow/compose.yml down

# Nuke everything including Airflow's postgres + logs
docker compose -p airflow -f airflow/compose.yml down -v
```
