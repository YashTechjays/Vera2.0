#!/usr/bin/env bash
# Sync the built frontend (vera-frontend/dist) to a GCS bucket for static hosting — the UAT
# analogue of bb-build-push.sh for the frontend (dev ships the SPA as an nginx image instead).
# Assumes bb-gcp-auth.sh has already authenticated this step. Required env:
#   FRONTEND_BUCKET  target GCS bucket name (the uat step maps it from UAT_FRONTEND_BUCKET)
set -euo pipefail

: "${FRONTEND_BUCKET:?FRONTEND_BUCKET is required}"

# Mirror dist/ into the bucket and prune objects no longer in the build (so a removed asset
# doesn't linger). gcloud sets each object's content-type from its extension automatically.
gcloud storage rsync vera-frontend/dist "gs://$FRONTEND_BUCKET" \
  --recursive --delete-unmatched-destination-objects

echo "Synced vera-frontend/dist -> gs://$FRONTEND_BUCKET"
