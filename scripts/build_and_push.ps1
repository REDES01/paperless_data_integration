# Build the Paperless custom image locally and push to GitHub Container Registry.
#
# Prerequisites:
#   - Docker Desktop running
#   - GitHub personal access token with write:packages scope
#   - Run: docker login ghcr.io -u REDES01
#
# Usage:
#   .\scripts\build_and_push.ps1
#   .\scripts\build_and_push.ps1 -SkipPush   # build only, no push

param(
    [switch]$SkipPush
)

$ErrorActionPreference = 'Stop'
$REPO_ROOT   = Split-Path -Parent $PSScriptRoot
$WORKSPACE   = Split-Path -Parent $REPO_ROOT
$FORK_DIR    = Join-Path $WORKSPACE 'paperless-ngx-fork'
$IMAGE_LOCAL = 'paperless-ngx-ml:latest'
$IMAGE_GHCR  = 'ghcr.io/redes01/paperless-ngx-ml:latest'

# ── Verify fork exists ──
if (-not (Test-Path "$FORK_DIR\Dockerfile")) {
    Write-Host "ERROR: $FORK_DIR\Dockerfile not found." -ForegroundColor Red
    Write-Host "Make sure paperless-ngx-fork is cloned as a sibling of paperless_data_integration." -ForegroundColor Red
    exit 1
}

if ($SkipPush) {
    # ── Build only (no push) ──
    Write-Host "Building $IMAGE_LOCAL from $FORK_DIR ..." -ForegroundColor Cyan
    Write-Host "(This takes 5-15 minutes on first build, <2 min on cached rebuilds)" -ForegroundColor DarkGray
    docker build -t $IMAGE_LOCAL $FORK_DIR
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Build failed." -ForegroundColor Red
        exit 1
    }
    $size = docker images $IMAGE_LOCAL --format '{{.Size}}'
    Write-Host "Build complete: $IMAGE_LOCAL ($size)" -ForegroundColor Green
    Write-Host "To push later: .\scripts\build_and_push.ps1  (without -SkipPush)" -ForegroundColor Yellow
    exit 0
}

# ── Build and push in one step ──
# --provenance=false prevents Docker Desktop from adding attestation manifests
# that older Docker versions on the VM can't unpack (the "mismatched image
# rootfs and manifest layers" error).
Write-Host "Building and pushing $IMAGE_GHCR from $FORK_DIR ..." -ForegroundColor Cyan
Write-Host "(This takes 5-15 minutes on first build, <2 min on cached rebuilds)" -ForegroundColor DarkGray
Write-Host "If this fails with 'denied', run: docker login ghcr.io -u REDES01" -ForegroundColor DarkGray

docker buildx build `
    --provenance=false `
    --tag $IMAGE_GHCR `
    --tag $IMAGE_LOCAL `
    --push `
    $FORK_DIR

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Build or push failed." -ForegroundColor Red
    Write-Host "Check your GHCR auth: docker login ghcr.io -u REDES01" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Done. Image available at $IMAGE_GHCR" -ForegroundColor Green
Write-Host ""
Write-Host "On the Chameleon VM, pull with:" -ForegroundColor Cyan
Write-Host "  docker pull $IMAGE_GHCR"
