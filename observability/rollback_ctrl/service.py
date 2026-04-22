"""
Rollback controller.

Receives two kinds of HTTP calls:
  POST /alert         — Alertmanager webhook. Body is Alertmanager's JSON.
                        If any firing alert has action=rollback, roll the
                        currently-deployed HTR model back to the previous
                        registered version in MLflow.
  POST /deploy        — promote a specific MLflow model version.
                        Body: {"version": 3}  or  {"uri": "models:/htr/3"}
  GET  /current       — return the currently-deployed URI.
  GET  /health        — liveness.

Mechanism: writes a file (/models/current_htr.txt by default) that the
ml_gateway container reads at startup and on SIGHUP. Signals via docker
socket are used to kick ml_gateway to reload.

Fail-loud: if MLflow can't enumerate models or docker can't signal, we log
and fall through — this service should never block an alert from firing
(Alertmanager will retry).
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rollback_ctrl")

MLFLOW_TRACKING_URI   = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000")
MODEL_NAME            = os.environ.get("MODEL_NAME", "htr")
MODEL_REGISTRY_FILE   = Path(os.environ.get("MODEL_REGISTRY_FILE", "/models/current_htr.txt"))
ML_GATEWAY_CONTAINER  = os.environ.get("ML_GATEWAY_CONTAINER", "ml_gateway")


app = FastAPI(title="Rollback Controller")


def _client():
    import mlflow
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    return mlflow.MlflowClient()


def _list_versions() -> list[int]:
    try:
        client = _client()
        versions = client.search_model_versions(f"name = '{MODEL_NAME}'")
        return sorted(int(v.version) for v in versions)
    except Exception as exc:
        log.warning("mlflow list versions failed: %s", exc)
        return []


def _write_registry(uri: str) -> None:
    MODEL_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_REGISTRY_FILE.write_text(uri + "\n", encoding="utf-8")
    log.info("wrote registry file %s <- %s", MODEL_REGISTRY_FILE, uri)


def _signal_ml_gateway() -> None:
    """Send SIGHUP to ml_gateway via docker CLI on the mounted socket."""
    try:
        subprocess.run(
            ["docker", "kill", "--signal=HUP", ML_GATEWAY_CONTAINER],
            check=True, capture_output=True, timeout=5,
        )
        log.info("sent SIGHUP to %s", ML_GATEWAY_CONTAINER)
    except Exception as exc:
        log.warning("SIGHUP to %s failed (ml_gateway will pick up on next restart): %s",
                    ML_GATEWAY_CONTAINER, exc)


def _current_uri() -> str:
    if MODEL_REGISTRY_FILE.exists():
        return MODEL_REGISTRY_FILE.read_text(encoding="utf-8").strip()
    return ""


@app.get("/health")
def health():
    return {"status": "ok", "current": _current_uri()}


@app.get("/current")
def current():
    return {"uri": _current_uri()}


class DeployRequest(BaseModel):
    version: int | None = None
    uri: str | None = None


@app.post("/deploy")
def deploy(req: DeployRequest):
    if req.uri:
        target = req.uri
    elif req.version is not None:
        target = f"models:/{MODEL_NAME}/{req.version}"
    else:
        raise HTTPException(status_code=400, detail="provide either version or uri")

    _write_registry(target)
    _signal_ml_gateway()
    return {"deployed": target}


@app.post("/alert")
def alert(payload: dict[str, Any]):
    """
    Alertmanager webhook payload looks like:
      {"alerts": [{"status": "firing", "labels": {"action": "rollback", ...}, ...}], ...}
    """
    firing_rollbacks = [
        a for a in payload.get("alerts", [])
        if a.get("status") == "firing"
        and (a.get("labels") or {}).get("action") == "rollback"
    ]
    if not firing_rollbacks:
        return {"rolled_back": False, "reason": "no firing rollback alerts"}

    versions = _list_versions()
    if len(versions) < 2:
        msg = f"need 2+ registered versions to roll back, got {len(versions)}"
        log.warning(msg)
        return {"rolled_back": False, "reason": msg}

    current = _current_uri()
    # Extract current version number from "models:/htr/N"
    current_version: int | None = None
    if current.startswith(f"models:/{MODEL_NAME}/"):
        try:
            current_version = int(current.rsplit("/", 1)[1])
        except ValueError:
            current_version = None

    # Roll back to the version immediately below current, or to the latest
    # non-current version if current is pretrained/unknown.
    target_version: int
    if current_version is not None and current_version in versions:
        below = [v for v in versions if v < current_version]
        if below:
            target_version = max(below)
        else:
            msg = f"no version below current={current_version} to roll back to"
            log.warning(msg)
            return {"rolled_back": False, "reason": msg}
    else:
        # Current is pretrained or unknown — roll to the latest registered.
        target_version = max(versions)

    target_uri = f"models:/{MODEL_NAME}/{target_version}"
    _write_registry(target_uri)
    _signal_ml_gateway()
    log.info("rollback: %s -> %s (triggered by %d firing alert(s))",
             current or "pretrained", target_uri, len(firing_rollbacks))
    return {
        "rolled_back": True,
        "from": current or "pretrained",
        "to": target_uri,
        "firing_alerts": len(firing_rollbacks),
    }
