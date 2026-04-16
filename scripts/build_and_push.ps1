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

# ── Build ──
Write-Host "Building $IMAGE_LOCAL from $FORK_DIR ..." -ForegroundColor Cyan
Write-Host "(This takes 5-15 minutes on first build, <2 min on cached rebuilds)" -ForegroundColor DarkGray
docker build -t $IMAGE_LOCAL $FORK_DIR
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Build failed." -ForegroundColor Red
    exit 1
}

$size = docker images $IMAGE_LOCAL --format '{{.Size}}'
Write-Host "Build complete: $IMAGE_LOCAL ($size)" -ForegroundColor Green

if ($SkipPush) {
    Write-Host "Skipping push (-SkipPush flag set)." -ForegroundColor Yellow
    Write-Host "To push later: docker tag $IMAGE_LOCAL $IMAGE_GHCR && docker push $IMAGE_GHCR"
    exit 0
}

# ── Tag and push ──
Write-Host ""
Write-Host "Tagging as $IMAGE_GHCR ..." -ForegroundColor Cyan
docker tag $IMAGE_LOCAL $IMAGE_GHCR

Write-Host "Pushing to GHCR (~3.5 GB, depends on upload speed) ..." -ForegroundColor Cyan
Write-Host "If this fails with 'denied', run: docker login ghcr.io -u REDES01" -ForegroundColor DarkGray
docker push $IMAGE_GHCR
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Push failed. Check your GHCR auth." -ForegroundColor Red
    Write-Host "Run: docker login ghcr.io -u REDES01" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Done. Image available at $IMAGE_GHCR" -ForegroundColor Green
Write-Host ""
Write-Host "On the Chameleon VM, pull with:" -ForegroundColor Cyan
Write-Host "  docker pull $IMAGE_GHCR"
Write-Host "  docker tag $IMAGE_GHCR $IMAGE_LOCAL"
