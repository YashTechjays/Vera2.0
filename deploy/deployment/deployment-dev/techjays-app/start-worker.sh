#!/bin/bash
# Deploy script — run from jumpserver via SSH to start/restart the VERA agent worker.
# Expects the following env vars to be set by deploy.sh (jumpserver):
#   WORKER_IMAGE — full Artifact Registry image URL
#   VERA_ENV     — deployment environment (dev / staging / production)
set -euo pipefail

: "${WORKER_IMAGE:?WORKER_IMAGE must be set by deploy.sh}"
: "${VERA_ENV:=dev}"

export DEBIAN_FRONTEND=noninteractive

if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io
  systemctl enable docker
fi
systemctl start docker || true

gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

echo "Fetching secrets from Secret Manager..."
LK_URL=$(gcloud secrets versions access latest --secret=vera-livekit-url)
LK_API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-api-key)
LK_API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-api-secret)
DEEPGRAM_KEY=$(gcloud secrets versions access latest --secret=vera-deepgram-api-key)
CARTESIA_KEY=$(gcloud secrets versions access latest --secret=vera-cartesia-api-key)
REDIS_URL=$(gcloud secrets versions access latest --secret=vera-redis-url)

echo "Pulling image: $WORKER_IMAGE"
docker pull "$WORKER_IMAGE"

docker stop vera-worker 2>/dev/null || true
docker rm   vera-worker 2>/dev/null || true

echo "Starting vera-worker..."
docker run -d \
  --name vera-worker \
  --restart unless-stopped \
  -e VERA_ENV="$VERA_ENV" \
  -e LIVEKIT_URL="$LK_URL" \
  -e LIVEKIT_API_KEY="$LK_API_KEY" \
  -e LIVEKIT_API_SECRET="$LK_API_SECRET" \
  -e DEEPGRAM_API_KEY="$DEEPGRAM_KEY" \
  -e CARTESIA_API_KEY="$CARTESIA_KEY" \
  -e VERA_REDIS_URL="$REDIS_URL" \
  "$WORKER_IMAGE"

echo "Done. vera-worker status:"
docker ps --filter name=vera-worker
