#!/usr/bin/env bash
#
# Run all 4 training candidates in sequence. Produces the candidate table
# visible in MLflow, and registers (up to) three model versions under the
# "htr" registry, one per passing candidate.
#
# Total runtime on CPU (m1.xlarge):
#   baseline             ~  2 min
#   finetune_iam         ~ 15 min
#   finetune_corrections ~  5-10 min  (depends on snapshot size, may be 0)
#   finetune_combined    ~ 20-25 min
# Total                  ~ 45-55 min
#
# Usage:
#   cd ~/paperless_data_integration
#   bash scripts/run_all_candidates.sh

set -uo pipefail   # not -e: we want to continue past a failed candidate

cd "$(dirname "$0")/.."

run_candidate() {
    local name="$1"
    echo ""
    echo "══════════════════════════════════════════════════════════"
    echo "  ${name}"
    echo "══════════════════════════════════════════════════════════"
    docker compose -p training -f training/compose.yml run --rm "${name}" \
        || echo "WARN: ${name} did not pass quality gate (or errored) — continuing"
}

echo "Building trainer image (first time ~10 min)..."
docker compose -p training -f training/compose.yml build baseline

# Baseline MUST run first — it establishes the reference CER that every
# subsequent candidate's quality gate compares against.
run_candidate baseline
run_candidate finetune_iam
run_candidate finetune_corrections
run_candidate finetune_combined

echo ""
echo "All candidates finished. See results at http://<vm>:5000"
echo "Candidate table is the 'htr_training' experiment."
echo "Registered models are visible under the 'Models' tab, name 'htr'."
