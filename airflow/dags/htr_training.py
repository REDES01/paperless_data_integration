"""
HTR retraining DAG.

Runs `finetune_combined_stage1` — the same config the shell scheduler used —
but via Airflow so we get:
  - Visual run history (Grid / Graph view)
  - Manual trigger button
  - Retry on failure
  - Clear success/failure at each step
  - Airflow's task logs in the UI

The DAG fires every day at 02:00 UTC by default. To trigger manually, go to
http://<vm>:8080, click "htr_retraining" -> "Trigger DAG".

Task graph:
    finetune_combined_stage1
              |
              v
         notify_result

The training task uses DockerOperator to spawn a fresh `htr_trainer:latest`
container on the shared paperless_ml_net so it can reach MLflow + MinIO +
Postgres. The image must be pre-built (once, via
`docker compose -p training -f training/compose.yml build`).

notify_result parses MLflow for the latest htr version that was registered
and tells the user the command to deploy it. Deployment is kept manual
because the operator should review the training metrics in MLflow before
promoting the model to ml_gateway.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from urllib import request as urlrequest

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.docker.operators.docker import DockerOperator

log = logging.getLogger(__name__)

MLFLOW_URL = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000")


def _check_latest_registered_version(**_ctx):
    """Query MLflow for the latest registered htr version and print a deploy hint."""
    url = f"{MLFLOW_URL}/api/2.0/mlflow/registered-models/get-latest-versions"
    body = json.dumps({"name": "htr"}).encode()
    req = urlrequest.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        log.warning("couldn't query MLflow registry: %s", exc)
        return

    versions = data.get("model_versions", [])
    if not versions:
        log.info("no htr versions registered yet — training run likely failed the quality gate")
        return

    latest = max(int(v["version"]) for v in versions)
    log.info("=" * 60)
    log.info("Latest registered htr version: htr/v%s", latest)
    log.info("=" * 60)
    log.info("")
    log.info("To deploy this version to ml_gateway, run on the VM:")
    log.info("    cd ~/paperless_data_integration && \\")
    log.info("    sg docker -c 'bash scripts/deploy_model.sh %s'", latest)
    log.info("")
    log.info("Then verify:")
    log.info("    curl -s http://localhost:8100/health | python3 -m json.tool")
    log.info("    (htr_version should read 'models:/htr/%s')", latest)


default_args = {
    "owner": "paperless-ml",
    "retries": 0,
    "retry_delay": timedelta(minutes=10),
    "email_on_failure": False,
    "email_on_retry": False,
}

with DAG(
    dag_id="htr_retraining",
    default_args=default_args,
    description="Fine-tune TrOCR on IAM + user corrections. Register to MLflow if quality gate passes.",
    schedule="0 2 * * *",  # daily at 02:00 UTC
    start_date=datetime(2026, 4, 22),
    catchup=False,
    max_active_runs=1,
    tags=["htr", "training", "mlflow"],
    doc_md=__doc__,
) as dag:

    finetune = DockerOperator(
        task_id="finetune_combined_stage1",
        image="htr_trainer:latest",
        command=["--config", "/app/configs/finetune_combined_stage1.yaml"],
        network_mode="paperless_ml_net",
        auto_remove="force",
        docker_url="unix://var/run/docker.sock",
        mount_tmp_dir=False,
        environment={
            "MLFLOW_TRACKING_URI": MLFLOW_URL,
            "MINIO_ENDPOINT":   "minio:9000",
            "MINIO_ACCESS_KEY": "admin",
            "MINIO_SECRET_KEY": "paperless_minio",
            "PAPERLESS_ML_DBHOST": "postgres",
            "PAPERLESS_ML_DBPORT": "5432",
            "PAPERLESS_ML_DBNAME": "paperless",
            "PAPERLESS_ML_DBUSER": "user",
            "PAPERLESS_ML_DBPASSWORD": "paperless_postgres",
        },
        # The trainer returns exit 1 when the quality gate fails.
        # That's a legitimate outcome — log it but don't alarm the demo.
        retrieve_output=False,
    )

    notify = PythonOperator(
        task_id="notify_result",
        python_callable=_check_latest_registered_version,
        # Always run, even if the upstream task failed — we want to see what
        # version (if any) is the newest in MLflow after a bad run.
        trigger_rule="all_done",
    )

    finetune >> notify
