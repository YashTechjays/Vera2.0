#!/usr/bin/env bash
# Deploy, in dependency order, every component that has an image built for THIS commit:
#   control-plane (migrate + seed)  ->  agent-worker  ->  frontend (LAST)
#
# "Has an image" == the component's :$BITBUCKET_COMMIT tag exists in Artifact Registry, which
# is true iff its build step ran this pipeline (changeset match on the dev branch, or the
# manual build-all in the deploy-dev-all custom pipeline). Keying off AR existence keeps the
# deploy automatically consistent with what was built — no path filtering duplicated here, and
# it works the same for the auto and manual pipelines.
#
# Bitbucket allows only ONE `deployment:` step per pipeline, so all components deploy from this
# single step (sequential — the VM serializes /opt/vera + `docker compose` anyway). set -e means
# a failed backend deploy aborts before the frontend, so a broken backend never gets a fresh SPA.
#
# Assumes bb-gcp-auth.sh has already run. Required env: GCP_REGION, GCP_PROJECT_ID, AR_REPO,
# BITBUCKET_COMMIT (+ the vars bb-deploy-vm.sh needs: SECRET_PREFIX, VM_NAME, VM_ZONE).
set -euo pipefail

: "${GCP_REGION:?repository variable GCP_REGION is required}"
: "${GCP_PROJECT_ID:?repository variable GCP_PROJECT_ID is required}"
: "${AR_REPO:?repository variable AR_REPO is required}"
: "${BITBUCKET_COMMIT:?BITBUCKET_COMMIT not set}"

here="$(cd "$(dirname "$0")" && pwd)"
ar_base="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${AR_REPO}"

image_exists() {   # $1 = AR image name (control-plane | agent-worker | frontend)
  gcloud artifacts docker images describe "$ar_base/$1:$BITBUCKET_COMMIT" >/dev/null 2>&1
}

deployed=0
# args: <compose-service> <AR-image> <tag_var> <run_migrations> <run_seed>
deploy_if_built() {
  if image_exists "$2"; then
    echo "==> deploying $1 ($2:$BITBUCKET_COMMIT)"
    bash "$here/bb-deploy-vm.sh" "$1" "$3" "$4" "$5"
    deployed=1
  else
    echo "==> skip $1 — no image for $BITBUCKET_COMMIT (unchanged this commit)"
  fi
}

deploy_if_built control-plane control-plane CONTROL_PLANE_TAG true  true
deploy_if_built agent-worker  agent-worker  WORKER_TAG        false false
deploy_if_built frontend      frontend      FRONTEND_TAG      false false

[ "$deployed" = 1 ] || echo "Nothing to deploy for $BITBUCKET_COMMIT (no component images built)."
