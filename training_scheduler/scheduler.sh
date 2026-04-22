#!/bin/sh
# Recurring training scheduler.
#
# Fires the configured training candidate every INTERVAL_MIN minutes by
# invoking `docker compose run --rm <candidate>` via the mounted docker
# socket. Skips firing if a training container is already running so we
# never double-fire (two concurrent trainers would fight over the same
# MLflow artifacts and the same GPU/CPU budget).
#
# Config via env vars (see Dockerfile defaults):
#   INTERVAL_MIN       — minutes between retraining attempts (default 60)
#   CANDIDATE          — which compose service to run (default finetune_combined)
#   COMPOSE_PROJECT    — compose project name (default "training")
#   COMPOSE_FILE       — path to compose file inside this container
#
# Production recommendation: INTERVAL_MIN=1440 (daily).
# Demo recommendation:       INTERVAL_MIN=60   (easy to catch on camera).

set -u

log() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

training_running() {
    # Any containers from the training compose project currently running?
    count=$(docker ps \
        --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" \
        --filter "status=running" \
        -q | wc -l)
    [ "$count" -gt 0 ]
}

fire_training() {
    log "triggering: candidate=${CANDIDATE}"
    # --rm: clean up the container when done
    # We capture the exit code and log it — exit 1 (gate failed) is a normal
    # outcome and not an error of the scheduler itself.
    if docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" \
            run --rm "${CANDIDATE}"; then
        log "training completed: gate PASSED (model registered)"
    else
        rc=$?
        log "training completed: exit=${rc} (gate failed or error — not registered)"
    fi
}

log "======================================================"
log "training scheduler starting"
log "  candidate:    ${CANDIDATE}"
log "  interval_min: ${INTERVAL_MIN}"
log "  compose:      ${COMPOSE_FILE}"
log "  project:      ${COMPOSE_PROJECT}"
log "======================================================"

# Sanity checks on boot
if ! docker info > /dev/null 2>&1; then
    log "FATAL: cannot reach docker daemon (socket not mounted?)"
    exit 1
fi
if [ ! -f "${COMPOSE_FILE}" ]; then
    log "FATAL: compose file not found at ${COMPOSE_FILE}"
    exit 1
fi

# Delay the first fire by 30s so the rest of the stack is up first
log "initial boot delay 30s..."
sleep 30

while true; do
    log "── tick ──"
    if training_running; then
        log "SKIP: training container already running"
    else
        fire_training
    fi
    log "sleeping ${INTERVAL_MIN} minutes until next tick"
    sleep $((INTERVAL_MIN * 60))
done
