# Airflow

Scheduled orchestration for the HTR retraining pipeline. Replaces the earlier
`training_scheduler` shell-loop with an actual workflow engine.

## Quick start

```bash
# One-time: make sure the data stack + training image are built
cd ~/paperless_data_integration
bash scripts/up_paperless_data.sh
docker compose -p training -f training/compose.yml build

# Start Airflow
bash scripts/up_airflow.sh
```

Then open http://your-vm:8080 — login `admin` / `admin`.

## DAGs

### `htr_retraining`

Fine-tunes TrOCR-base-stage1 on IAM + user corrections, evaluates, and
registers a new `htr` version in MLflow if the quality gate passes.

- **Schedule:** daily at 02:00 UTC
- **Task graph:** `finetune_combined_stage1` → `notify_result`
- **Deployment:** manual — after a run passes the gate, `notify_result` prints
  the `deploy_model.sh` command the operator runs on the VM

## Architecture

```
Airflow webserver (:8080)           ← operator UI
Airflow scheduler                   ← reads /opt/airflow/dags/*.py every 30s
    ↓ uses docker.sock to spawn:
htr_trainer:latest container        ← one per DAG run
    ↓ mounts model_registry volume, joins paperless_ml_net
    ↓ reads IAM + corrections from the data-stack Postgres
    ↓ trains, evaluates, logs to MLflow, registers if gate passes
exits 0 (gate passed) or 1 (failed) — Airflow records the outcome
```

Airflow scheduler runs as root (`user: "0:0"`) so it can read `/var/run/docker.sock`
without needing a host-GID-specific mount. Acceptable on a single-tenant demo VM.

## Why DockerOperator instead of BashOperator

Two reasons:

1. **No host-path dependency.** BashOperator calling `docker compose` would
   require mounting `~/paperless_data_integration` into the scheduler
   container — fragile across host path changes.
2. **Clean isolation.** Each DAG run spawns a fresh `htr_trainer:latest`
   container that lives/dies with that task. Its logs are linked in Airflow.

## Manual trigger

From the UI:
1. Navigate to `htr_retraining`
2. Click the ▶️ Play icon on the top right
3. Watch Grid view for task progression

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
- `"@hourly"` — every hour (for demo)
- `"@daily"` — midnight local time
- `"*/15 * * * *"` — every 15 min (demo-friendly, watch multiple runs in one session)
- `None` — only manual triggers

## Debugging a failing task

1. Airflow UI → `htr_retraining` → the failed run → task box → Logs tab
2. Scroll to the bottom for stdout + stderr from the training container
3. For the training container itself (still running):
   ```bash
   docker ps --filter ancestor=htr_trainer:latest
   docker logs <container_id>
   ```

## Teardown

```bash
# Stop Airflow, keep its data (DAG history, logs)
docker compose -p airflow -f airflow/compose.yml down

# Nuke everything including Airflow's own postgres + logs
docker compose -p airflow -f airflow/compose.yml down -v
```
