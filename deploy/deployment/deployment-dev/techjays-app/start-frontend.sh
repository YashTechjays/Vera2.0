#!/bin/bash
# Frontend deploy script — runs on the jumpserver (not SSH'd into a VM).
# Builds the React SPA via Cloud Build, pushes to GCS, then syncs
# to vera-control-plane-vm and reloads nginx.
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID must be set}"
: "${ZONE:?ZONE must be set}"
: "${BUCKET:?BUCKET must be set}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"   # techjays-app → deployment-dev → deployment → Vera2.0 (repo root)
CLOUDBUILD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/cloudbuild"

if [[ "${SKIP_BUILD:-false}" == "false" ]]; then
  echo "Building frontend and pushing to GCS..."
  gcloud builds submit "$REPO_ROOT" \
    --config="$CLOUDBUILD_DIR/cloudbuild-frontend.yaml" \
    --project="$PROJECT_ID"
else
  echo "Skipping frontend build (--skip-build set) — assuming GCS already has latest files."
fi

echo "Syncing frontend files on vera-control-plane-vm..."
gcloud compute ssh vera-control-plane-vm \
  --tunnel-through-iap \
  --zone="$ZONE" \
  --project="$PROJECT_ID" \
  -- "sudo gsutil -m rsync -r -d gs://${BUCKET} /var/www/vera-frontend/ && sudo nginx -s reload"

echo "Frontend deploy complete."
