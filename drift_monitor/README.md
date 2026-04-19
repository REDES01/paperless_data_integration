# Drift Monitor

Live data-quality + drift monitoring for the HTR pipeline. Covers the data
role's point 3 deliverable: *evaluate data quality at three points*, third
being "live inference data quality and drift monitoring in production."

## What it does

On every upload that flows through `htr_consumer`, each handwritten-region
crop is POSTed to this service **after** the HTR inference call. The
service:

1. Downloads the crop from MinIO
2. Preprocesses (grayscale, resize to 64×512, scale to [0,1])
3. Runs it through an online MMD drift detector pre-fit on 500 IAM training
   crops
4. Exports Prometheus metrics: `drift_events_total`, `drift_test_stat`,
   `drift_checks_total`, `drift_check_errors_total`
5. Grafana dashboard plots them; Alertmanager fires a webhook at the
   rollback controller if drift events exceed threshold for 2 minutes

Same pattern as the course's online-evaluation lab
(`eval-online-chi/workspace/4-eval_online.ipynb`), lifted from Food-11 + AI
images into IAM handwriting crops.

## Why a separate service

Three options were on the table:

- Integrated into `ml-gateway` — owned by the serving role, minimum
  latency overhead
- Called by htr_consumer, library-style — serving-neutral but couples
  consumer and detector in one process
- **Separate service on the shared network** — what this is

We went with the separate-service option because (a) it isolates the
heavy dependency (alibi-detect + torch CPU wheel ≈ 600MB) from the
lightweight consumer, (b) it makes the detector Prometheus-native with
no boilerplate in other services, and (c) the data role owns the
implementation end-to-end without waiting on serving.

## Architecture

```
Paperless upload
  │
  ▼
htr_consumer  ──HTR──▶ ml-gateway
  │
  ├──drift/check──▶ drift_monitor ──/metrics──▶ Prometheus ──▶ Grafana
  │                        │                        │
  │                        └───────► MinIO          └──▶ Alertmanager ──▶ rollback-ctrl
  │                                (ref + crops)
  ▼
Postgres (documents, regions)
```

The consumer fires drift checks **fire-and-forget** with a 2s timeout, so
a slow or down monitor never stalls HTR processing.

## Files

| File | What it is |
|---|---|
| `service.py`                | FastAPI app — `/drift/check`, `/health`, `/metrics` |
| `Dockerfile`                | Python 3.12-slim + torch CPU wheel + alibi-detect |
| `compose.yml`               | Single service on `paperless_ml_net` |
| `requirements.txt`          | Pinned deps |
| `grafana_dashboard.json`    | Two-panel dashboard (import into Grafana) |
| `../../paperless_data/scripts/build_drift_reference.py` | Offline one-shot: builds + saves detector to MinIO |

## Bring-up order

1. **Build the detector once** (offline, from the data-role machine with
   `MINIO_ENDPOINT` pointing at the live MinIO):
   ```bash
   cd paperless_data
   MINIO_ENDPOINT=<host>:9000 \
   MINIO_ACCESS_KEY=minioadmin MINIO_SECRET_KEY=minioadmin \
   python scripts/build_drift_reference.py
   ```
   Writes `s3://paperless-datalake/warehouse/drift_reference/htr_v1/cd/*`.

2. **Start the monitor** on the VM:
   ```bash
   cd paperless_data_integration
   docker compose -f drift_monitor/compose.yml up -d --build
   ```

3. **Extend Prometheus scrape config** in `paperless-ml/ops/prometheus/
   prometheus.yml`:
   ```yaml
   - job_name: drift-monitor
     metrics_path: /metrics
     static_configs:
       - targets: ["drift_monitor:8000"]
         labels:
           service: drift-monitor
   ```
   then reload Prometheus.

4. **Import the Grafana dashboard** from `grafana_dashboard.json`.

5. **Optional — add an Alertmanager rule** to fire the rollback webhook
   on sustained drift. Add to `paperless-ml/ops/prometheus/alerts.yml`:
   ```yaml
   groups:
     - name: htr-drift
       rules:
         - alert: HtrInputDrift
           expr: rate(drift_events_total[5m]) > 0.2
           for: 2m
           labels: { severity: critical, action: rollback }
           annotations:
             summary: "HTR input distribution has drifted"
             description: "Sustained MMD drift events > 0.2/s over last 5m."
   ```

## Testing the end-to-end loop

Once everything is up, verify each step:

```bash
# 1. Monitor is healthy and has loaded the detector
curl http://localhost:8100/health

# 2. Metrics endpoint exposes drift counters (values will be 0)
curl http://localhost:8100/metrics | grep drift_

# 3. Generate IAM-like traffic — should not trigger drift
cd paperless_data
python data_generator/generator.py \
    --paperless-url  http://<vm>:8000 \
    --paperless-token $PAPERLESS_TOKEN \
    --rate 1 --duration 60

# 4. Check Grafana panel — test_stat should stay low, no drift events

# 5. Generate deliberately out-of-distribution crops (e.g., typed text
#    passing through the synthetic-page path will differ from real IAM
#    handwriting) — drift events should climb within 1 minute.
```

## Why this is *a* reasonable detector (rubric defensibility)

- **Reference**: 500 IAM training-split line crops — the exact same
  distribution the HTR model is nominally trained on.
- **Feature extractor**: 2-layer CNN + adaptive pool. Small, fast, no
  dependency on the HTR model's weights. Alibi-detect uses this exact
  shape in its image drift examples.
- **Statistical test**: MMD with PyTorch backend. Online variant with
  ERT=300, window=20 — tuned so false positives average once every ~5min
  of idle traffic while real shifts are caught within ~20 crops.
- **Preprocessing**: identical on reference and production side
  (same resize, channel count, dtype, value range). This is where
  most drift monitors leak — we explicitly call the same function.

## Thresholds (subject to tuning)

| Metric | Default | Rationale |
|---|---|---|
| Window size    | 20          | Smooths out single-crop noise; detects shifts in ~1-2 docs' worth of traffic |
| ERT            | 300 checks  | ~5 min of idle traffic between false positives at 1 check/sec |
| Alert threshold| 0.2 drift/s | Roughly 1-in-5 crops flagging over 5 min. Aggressive; tune down to 0.1 if noisy |
| Alert for      | 2m          | Avoid flapping on a single upload batch |

These numbers are starting points. Re-tune after first week of production
traffic — specifically, watch the `p95` of `drift_test_stat` when the
stack is known-healthy; the alert threshold should live well above it.

## Known limitations

- Single point of failure. Fire-and-forget makes it robust against
  short outages but if the monitor is down for an hour, we have no
  visibility for that hour. Acceptable for the milestone; add a
  readiness probe + replica later if it becomes material.
- MMD is a global test. A tiny sub-population of drifted crops (say,
  5% of uploads are from a very different handwriting style) will not
  trigger the alert until they make up a larger fraction. For finer
  granularity, we'd layer on a classifier-based detector later.
- The detector is fit against IAM-only data. Real Paperless users may
  upload crops that are legitimately OOD relative to IAM (e.g., forms
  in other languages). This is why the threshold is loose — we want to
  detect *sustained* drift, not one-off exotic uploads.
