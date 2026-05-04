#!/usr/bin/env bash
# bring_up_full_stack.sh
#
# Bring up the complete Paperless-ngx ML platform on an already-provisioned VM.
# This is the bash translation of provision_chameleon.ipynb, with Part 1
# (Chameleon lease + node + Docker install) removed — the VM already exists,
# Docker is installed, and the current user is in the docker group.
#
# What this script does (matches the notebook step-for-step):
#   Part 2  — Step  7  clone both repos
#             Step  8  write Paperless secret key
#             Step  9  pull Paperless image from GHCR
#             Step 10  start data stack (postgres, minio, redpanda, qdrant) + ML schema migration
#             Step 11  start Paperless
#             Step 12  create superuser + fetch API token
#             Step 13  run scripts/up_all.sh (observability, ml_gateway, qdrant_indexer,
#                       drift_monitor, htr_consumer)
#             Step 14  wait for ml_gateway health
#             Step 15  upload sample documents
#   Part 3  — Step 19  ingest IAM into MinIO
#             Step 20  run ingestion validation
#             Step 21  build drift reference (MMDDriftOnline)
#             Step 22  upload OOD samples
#   Part 4  — print service URLs + token
#   Part 5  — Step 23  build trainer image, run baseline + finetune (LONG: ~45 min)
#             Step 24  deploy registered model to ml_gateway
#             Step 26  bring up Airflow
#             Step 27  start data generator (stability mode: 5 uploads/hr)
#             Step 28  start behavior emulator (BE_MODE=slow)
#
# Env vars (all optional):
#   STEP_FROM=N        skip ahead to step N (default 7). useful for resuming.
#   STEP_TO=N          stop after step N (default 99).
#   SKIP_TRAINING=1    skip step 23+24 (saves ~45 min — model registry will be empty)
#   SKIP_GENERATORS=1  skip steps 27+28 (no synthetic traffic)
#   REPO_PARENT        directory to clone into (default $HOME)
#   FORCE_RECLONE=1    blow away existing repo dirs and re-clone (default: skip clone if dirs exist)
#
# Usage:
#   bash bring_up_full_stack.sh                          # full run
#   STEP_FROM=13 bash bring_up_full_stack.sh             # resume at up_all.sh
#   SKIP_TRAINING=1 bash bring_up_full_stack.sh          # skip the slow training step
#   STEP_FROM=13 STEP_TO=15 bash bring_up_full_stack.sh  # only re-run ml services + samples

set -euo pipefail

# ───────────────────── config ─────────────────────
DATA_REPO="${DATA_REPO:-https://github.com/REDES01/paperless_data.git}"
INTEGRATION_REPO="${INTEGRATION_REPO:-https://github.com/REDES01/paperless_data_integration.git}"
REPO_PARENT="${REPO_PARENT:-$HOME}"
DATA_DIR="${REPO_PARENT}/paperless_data"
INTEG_DIR="${REPO_PARENT}/paperless_data_integration"

STEP_FROM="${STEP_FROM:-7}"
STEP_TO="${STEP_TO:-99}"
SKIP_TRAINING="${SKIP_TRAINING:-0}"
SKIP_GENERATORS="${SKIP_GENERATORS:-0}"
FORCE_RECLONE="${FORCE_RECLONE:-0}"

# ───────────────────── helpers ─────────────────────
say() { printf '\n\033[1;36m═══ %s ═══\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

step() {
    # step <N> "<label>" — returns 0 if step should run, 1 if it should be skipped
    local n="$1"; shift
    if [ "$n" -lt "$STEP_FROM" ] || [ "$n" -gt "$STEP_TO" ]; then
        printf '\033[2m──  skip step %2d  %s\033[0m\n' "$n" "$*"
        return 1
    fi
    printf '\n\033[1;32m▶▶  step %2d  %s\033[0m\n' "$n" "$*"
    return 0
}

# Sanity checks before we start
command -v docker >/dev/null || die "docker not on PATH — install docker first."
docker ps >/dev/null 2>&1 || die "cannot run docker as $(whoami) — re-login so docker group takes effect, or run with sg docker."
command -v git >/dev/null   || die "git not installed."

say "starting full-stack bring-up — STEP_FROM=$STEP_FROM STEP_TO=$STEP_TO"

# ═══════════════════════════════════════════════════════════════════════
# Part 2 — Deploy the stack
# ═══════════════════════════════════════════════════════════════════════

# ── Step 7: clone repos ───────────────────────────────────────────────
if step 7 "clone paperless_data + paperless_data_integration"; then
    if [ "$FORCE_RECLONE" = "1" ]; then
        rm -rf "$DATA_DIR" "$INTEG_DIR"
    fi
    if [ ! -d "$DATA_DIR" ]; then
        git clone "$DATA_REPO" "$DATA_DIR"
    else
        echo "  $DATA_DIR exists — git pull"
        git -C "$DATA_DIR" pull --ff-only || warn "git pull failed in $DATA_DIR (continuing)"
    fi
    if [ ! -d "$INTEG_DIR" ]; then
        git clone "$INTEGRATION_REPO" "$INTEG_DIR"
    else
        echo "  $INTEG_DIR exists — git pull"
        git -C "$INTEG_DIR" pull --ff-only || warn "git pull failed in $INTEG_DIR (continuing)"
    fi
    ( cd "$REPO_PARENT" && for d in paperless_data paperless_data_integration; do
        [ -d "$d" ] && echo "  $d/" || echo "  $d/ MISSING"
      done )
fi

# ── Step 8: write Paperless secret key ────────────────────────────────
if step 8 "write Paperless secret key"; then
    cd "$INTEG_DIR/paperless"
    if [ ! -f docker-compose.env ] || ! grep -q '^PAPERLESS_SECRET_KEY=[^r]' docker-compose.env; then
        cp docker-compose.env.example docker-compose.env
        SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')
        # | as the sed delimiter so the URL-safe base64 chars in $SECRET don't collide.
        sed -i "s|PAPERLESS_SECRET_KEY=replace-me-with-a-real-secret|PAPERLESS_SECRET_KEY=$SECRET|" docker-compose.env
    else
        echo "  secret key already written — leaving it alone"
    fi
    grep PAPERLESS_SECRET_KEY docker-compose.env | sed 's/=.*/=<redacted>/'
    cd "$INTEG_DIR"
fi

# ── Step 9: pull Paperless image from GHCR ────────────────────────────
if step 9 "pull paperless-ngx-ml image from GHCR"; then
    docker pull ghcr.io/redes01/paperless-ngx-ml:latest
fi

# ── Step 10: data stack + ML schema migration ─────────────────────────
if step 10 "start data stack (postgres, minio, redpanda, qdrant) + apply ML schema migration"; then
    cd "$INTEG_DIR"
    bash scripts/create_network.sh
    bash scripts/up_paperless_data.sh

    echo "  waiting 25s for postgres + minio to become healthy..."
    sleep 25

    # paperless_doc_id migration — idempotent (script uses IF NOT EXISTS)
    docker exec -i postgres psql -U user -d paperless < seed/phase2_add_paperless_doc_id.sql
    echo "  data stack up, ML schema migrated."
fi

# ── Step 11: bring up Paperless ───────────────────────────────────────
if step 11 "start Paperless (web + redis + db)"; then
    cd "$INTEG_DIR"
    bash scripts/up_paperless.sh
    echo "  waiting 45s for Paperless to finish starting..."
    sleep 45
    docker ps --filter name=paperless --format "table {{.Names}}\t{{.Status}}"
fi

# ── Step 12: superuser + API token ────────────────────────────────────
PAPERLESS_TOKEN="${PAPERLESS_TOKEN:-}"
if step 12 "create Paperless superuser + fetch API token"; then
    docker exec paperless-webserver-1 python manage.py shell -c '
from django.contrib.auth.models import User
if not User.objects.filter(username="admin").exists():
    User.objects.create_superuser("admin", "admin@example.com", "admin")
print("Superuser ready")
'

    PAPERLESS_TOKEN=$(docker exec paperless-webserver-1 python manage.py shell -c '
from rest_framework.authtoken.models import Token
from django.contrib.auth.models import User
t, _ = Token.objects.get_or_create(user=User.objects.get(username="admin"))
print(t.key)
' | tail -1 | tr -d '[:space:]')

    [ -n "$PAPERLESS_TOKEN" ] || die "failed to obtain Paperless API token."
    echo "  API Token: $PAPERLESS_TOKEN"

    # Persist for subsequent SSH sessions (idempotent)
    sed -i '/^export PAPERLESS_TOKEN=/d' "$HOME/.bashrc"
    echo "export PAPERLESS_TOKEN=$PAPERLESS_TOKEN" >> "$HOME/.bashrc"
    export PAPERLESS_TOKEN
elif [ -z "$PAPERLESS_TOKEN" ] && [ "$STEP_FROM" -gt 12 ]; then
    # Resuming past step 12 — recover token from running paperless DB so later steps work.
    say "resuming past step 12 — recovering PAPERLESS_TOKEN from paperless-db"
    PAPERLESS_TOKEN=$(docker exec paperless-db-1 psql -U paperless -d paperless -t -A \
        -c "SELECT key FROM authtoken_token LIMIT 1;" | tr -d '[:space:]')
    [ -n "$PAPERLESS_TOKEN" ] || die "could not recover token from paperless-db. set PAPERLESS_TOKEN manually."
    export PAPERLESS_TOKEN
fi

# ── Step 13: build + start ML services via up_all.sh ──────────────────
if step 13 "run scripts/up_all.sh (observability + ml_gateway + qdrant_indexer + drift_monitor + htr_consumer)"; then
    cd "$INTEG_DIR"
    echo "  first build is ~15 min (downloads + caches TrOCR + mpnet into ml_gateway image)"
    PAPERLESS_TOKEN="$PAPERLESS_TOKEN" bash scripts/up_all.sh

    echo
    echo "  verifying observability stack…"
    # Grafana dashboards are bind-mounted from observability/grafana/dashboards/.
    # Both should appear in /api/search within ~30s of grafana boot. If a datasource
    # name in dashboard JSON ever drifts out of sync (case mismatch with the
    # provisioned datasource), panels silently show "No data" without errors —
    # this check catches that class of regression early.
    docker exec paperless-webserver-1 curl -sf -u admin:admin \
        "http://grafana:3000/api/search?type=dash-db" 2>&1 \
        | python3 -m json.tool 2>&1 | head -40 || warn "grafana dashboard list unavailable (yet?)"

    docker exec paperless-webserver-1 curl -sf \
        "http://prometheus:9090/api/v1/query?query=drift_events_total" 2>&1 \
        | python3 -m json.tool 2>&1 | head -15 || warn "prometheus drift metric unavailable (yet?)"
fi

# ── Step 14: wait for ml_gateway health ───────────────────────────────
if step 14 "wait for ml_gateway to load TrOCR + mpnet"; then
    echo "  waiting 90s for ml_gateway startup + model load..."
    sleep 90
    docker ps --format "table {{.Names}}\t{{.Status}}"
    echo
    echo "  ml_gateway health check:"
    curl -sf http://localhost:8100/health || echo "  not ready yet"
fi

# ── Step 15: upload sample documents ──────────────────────────────────
if step 15 "upload sample documents"; then
    [ -n "$PAPERLESS_TOKEN" ] || die "PAPERLESS_TOKEN unset — cannot upload samples."
    SAMPLES=(
        "$INTEG_DIR/sample_documents/sample_budget_memo.pdf"
        "$INTEG_DIR/sample_documents/sample_scan.jpeg"
    )
    for f in "${SAMPLES[@]}"; do
        if [ ! -f "$f" ]; then warn "sample missing: $f — skipping"; continue; fi
        echo "  uploading $f ..."
        curl -s -X POST http://localhost:8000/api/documents/post_document/ \
            -H "Authorization: Token $PAPERLESS_TOKEN" \
            -F "document=@$f"
        echo
    done
    echo "  uploads submitted. Waiting 60s for Paperless to ingest..."
    sleep 60
fi

# ═══════════════════════════════════════════════════════════════════════
# Part 3 — Drift reference + data quality artifacts
# ═══════════════════════════════════════════════════════════════════════

# Helper: a transient python container on the ML net with MinIO creds preset.
# Mirrors the `docker run --rm --network paperless_ml_net -e MINIO_*=...` blocks
# from the notebook. Pass the bash-script-to-run as the only argument.
run_in_python_pod() {
    local workdir="$1"; shift
    local script="$1"; shift
    docker run --rm --network paperless_ml_net \
        -v "$workdir:/app" -w /app \
        -e MINIO_ENDPOINT=minio:9000 \
        -e MINIO_ACCESS_KEY=admin \
        -e MINIO_SECRET_KEY=paperless_minio \
        python:3.12-slim bash -c "$script"
}

# ── Step 19: ingest IAM ───────────────────────────────────────────────
if step 19 "ingest IAM dataset into MinIO (warehouse/iam_dataset/)"; then
    run_in_python_pod "$DATA_DIR" \
      "pip install -q pyarrow minio datasets tqdm Pillow && python ingestion/ingest_iam.py"
    echo "  IAM ingestion complete."
fi

# ── Step 20: ingestion validation ─────────────────────────────────────
if step 20 "run ingestion validation (writes _validation/<ts>.json)"; then
    run_in_python_pod "$DATA_DIR" \
      "pip install -q -r batch_pipeline/requirements.txt && python batch_pipeline/validate_ingestion.py"
fi

# ── Step 21: build drift reference ────────────────────────────────────
if step 21 "build MMDDriftOnline reference detector"; then
    # Torch-CPU wheel index speeds this up vs the default PyPI mirror.
    run_in_python_pod "$DATA_DIR" "
pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.4.1 \
  && pip install --no-cache-dir alibi-detect pyarrow minio Pillow \
  && python scripts/build_drift_reference.py"
    echo "  reference detector uploaded to MinIO."
    echo "  drift_monitor's background retry loop will load it within 60 seconds."
fi

# ── Step 22: upload OOD samples ───────────────────────────────────────
if step 22 "generate + upload OOD samples for the drift demo"; then
    docker run --rm --network paperless_ml_net \
        -v "$DATA_DIR/scripts:/app" -w /app \
        python:3.12-slim bash -c "
pip install -q faker Pillow minio \
  && python make_ood_samples.py \
  && MINIO_ENDPOINT=minio:9000 MINIO_ACCESS_KEY=admin MINIO_SECRET_KEY=paperless_minio \
     python upload_ood_to_minio.py"
fi

# ═══════════════════════════════════════════════════════════════════════
# Part 4 — Print access URLs
# ═══════════════════════════════════════════════════════════════════════
if step 99 "print service URLs"; then
    # Best-effort detection of the floating IP the VM was reached on.
    # On Chameleon the floating IP isn't bound to an interface; we read it
    # from the metadata service or fall back to the user's $SSH_CONNECTION.
    FIP=$(curl -s --max-time 2 http://169.254.169.254/latest/meta-data/public-ipv4 || true)
    if [ -z "$FIP" ] && [ -n "${SSH_CONNECTION:-}" ]; then
        FIP=$(echo "$SSH_CONNECTION" | awk '{print $3}')
    fi
    [ -n "$FIP" ] || FIP="<vm-public-ip>"

    cat <<EOF

══════════════════════════════════════════════════════════════════════
  Service URLs
══════════════════════════════════════════════════════════════════════
  Paperless UI        http://${FIP}:8000          admin / admin
  HTR review          http://${FIP}:8000/ml/htr-review
  Semantic search     http://${FIP}:8000/ml/search
  Grafana             http://${FIP}:3000          admin / admin
  Prometheus          http://${FIP}:9090
  Alertmanager        http://${FIP}:9093
  MLflow              http://${FIP}:5000
  MinIO Console       http://${FIP}:9001          admin / paperless_minio
  Adminer             http://${FIP}:5050          user / paperless_postgres
  Redpanda Console    http://${FIP}:8090
  Qdrant Dashboard    http://${FIP}:6333/dashboard
  Airflow             http://${FIP}:8080          admin / admin

  API Token: ${PAPERLESS_TOKEN:-<run step 12 to mint>}
EOF
fi

# ═══════════════════════════════════════════════════════════════════════
# Part 5 — Training pipeline + Airflow + synthetic traffic
# ═══════════════════════════════════════════════════════════════════════

# ── Step 23: build trainer + run baseline + finetune ──────────────────
if [ "$SKIP_TRAINING" = "1" ]; then
    warn "SKIP_TRAINING=1 — skipping steps 23 + 24 (model registry will be empty)"
elif step 23 "build trainer image, run baseline_stage1 (~3 min) + finetune_iam_stage1 (~30-45 min)"; then
    cd "$INTEG_DIR" && git pull --ff-only || true
    cd "$DATA_DIR"  && git pull --ff-only || true
    cd "$INTEG_DIR"

    echo "  building trainer image (~10 min)..."
    docker compose -p training -f training/compose.yml build baseline_stage1

    echo "  running baseline_stage1 (~3 min) — records reference CER..."
    docker compose -p training -f training/compose.yml run --rm baseline_stage1

    echo "  running finetune_iam_stage1 (~30-45 min on cascadelake) — should pass gate, register as htr/v1..."
    docker compose -p training -f training/compose.yml run --rm finetune_iam_stage1

    echo
    echo "  verifying registry..."
    curl -s "http://localhost:5000/api/2.0/mlflow/registered-models/get?name=htr" \
        | python3 -m json.tool || warn "registry lookup failed"
fi

# ── Step 24: deploy registered model to ml_gateway ────────────────────
if [ "$SKIP_TRAINING" != "1" ] && step 24 "deploy registered model (SIGHUP ml_gateway)"; then
    cd "$INTEG_DIR"
    chmod +x scripts/deploy_model.sh
    bash scripts/deploy_model.sh latest
    echo
    echo "  ml_gateway health after SIGHUP reload:"
    curl -s http://localhost:8100/health | python3 -m json.tool || warn "health probe failed"
fi

# ── Step 26: bring up Airflow ─────────────────────────────────────────
if step 26 "bring up Airflow (metadata DB + init + webserver + scheduler)"; then
    cd "$INTEG_DIR" && git pull --ff-only || true
    cd "$DATA_DIR"  && git pull --ff-only || true
    cd "$INTEG_DIR"

    echo "  first boot builds paperless-airflow:2.10.4 + htr_trainer:latest + htr_batch:latest..."
    chmod +x scripts/up_airflow.sh
    bash scripts/up_airflow.sh

    echo
    echo "  Airflow containers:"
    docker compose -p airflow -f "$INTEG_DIR/airflow/compose.yml" ps

    echo
    echo "  waiting for Airflow scheduler to parse DAGs..."
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if curl -sf -m 3 -u admin:admin http://localhost:8080/api/v1/dags -o /tmp/dags.json 2>/dev/null; then
            break
        fi
        sleep 3
    done

    echo
    echo "  registered DAGs (dag_id : schedule_interval):"
    python3 -m json.tool /tmp/dags.json 2>/dev/null \
        | grep -E '"dag_id"|"value"' || warn "couldn't parse /tmp/dags.json"
fi

# ── Step 27: data generator (stability mode) ──────────────────────────
if [ "$SKIP_GENERATORS" = "1" ]; then
    warn "SKIP_GENERATORS=1 — skipping steps 27 + 28 (no synthetic traffic)"
elif step 27 "start data_generator (DG_RATE=0.0014 = ~5 uploads/hr, 1-hour cycles)"; then
    [ -n "$PAPERLESS_TOKEN" ] || die "PAPERLESS_TOKEN unset — cannot start data_generator."
    cd "$DATA_DIR"

    # DG_RATE=0.0014 = ~5/hr. DG_CYCLE_DURATION=3600 = 1 hr (long enough for at
    # least a handful of uploads per cycle). These override compose.yml defaults
    # which are tuned for demo recording, not multi-day unattended runs.
    PAPERLESS_TOKEN="$PAPERLESS_TOKEN" DG_RATE=0.0014 DG_CYCLE_DURATION=3600 \
        docker compose -p data_generator -f data_generator/compose.yml up -d --build

    echo "  waiting 45s for loop.sh to wait-for-Paperless + load IAM pool from MinIO..."
    sleep 45

    echo
    echo "  generator state:"
    docker ps --filter name=data_generator --format "{{.Names}}\t{{.Status}}"

    echo
    echo "  rate + IAM pool load status:"
    docker exec data_generator printenv RATE CYCLE_DURATION || true
    docker logs data_generator --tail 200 2>&1 \
        | grep -E "IAM pool|Paperless reachable|cycle [0-9]+ starting" | head -10 || true

    echo "  first UPLOAD will land in ~12 min (5/hr = 720s between uploads)."
fi

# ── Step 28: behavior emulator (slow mode) ────────────────────────────
if [ "$SKIP_GENERATORS" != "1" ] && step 28 "start behavior_emulator (BE_MODE=slow — 0.3 corr/min + 0.2 search/min)"; then
    cd "$INTEG_DIR"
    BE_MODE=slow docker compose -p behavior_emulator \
        -f behavior_emulator/compose.yml up -d --build

    sleep 10

    echo
    echo "  emulator state:"
    docker ps --filter name=behavior_emulator --format "{{.Names}}\t{{.Status}}"

    echo
    echo "  BE_MODE confirm:"
    docker exec behavior_emulator printenv BE_MODE || true

    echo
    echo "  first bot actions (may take ~3 min at slow rate):"
    docker logs behavior_emulator --tail 100 2>&1 \
        | grep -E "correction_bot|search_bot|correction on|query=" | head -10 || true

    cat <<'EOF'

═══════════════════════════════════════════════════════════════════════
  All three continuous services now active:
    data_generator      uploads synthetic docs with IAM handwriting (5/hr)
    behavior_emulator   corrections (0.3/min) + search feedback (0.2/min)
    Airflow DAGs        archive (15min), search rerank (hourly), retraining (daily 02:00)

  Expected after a 24-hour unattended run:
    uploads:       ~120 docs
    corrections:   ~430, archived to MinIO immutably
    searches:      ~290, feedback events accumulated
    training runs: 1 scheduled DAG run overnight, builds snapshot from corrections

  SQL to check stability after running:
    SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL;
    SELECT COUNT(*), COUNT(archived_at) FROM htr_corrections;
    SELECT SUM(total_impressions) FROM document_feedback_stats;
═══════════════════════════════════════════════════════════════════════
EOF
fi

say "done."
