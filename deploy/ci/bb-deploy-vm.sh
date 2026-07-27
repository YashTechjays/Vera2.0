#!/usr/bin/env bash
# Deploy one component to the test VM over an IAP SSH tunnel — the Bitbucket port of the
# GitHub _deploy-vm.yml reusable. Ships the compose file + deploy scripts to /opt/vera and
# runs remote-deploy.sh there (which renders env from Secret Manager, migrates, and restarts
# the service health-gated). Keyless throughout: bb-gcp-auth.sh must have run first.
#
#   bb-deploy-vm.sh <service> <tag_var> <run_migrations> <run_seed>
#     service          compose service: control-plane | agent-worker | frontend
#     tag_var          .env var to pin: CONTROL_PLANE_TAG | WORKER_TAG | FRONTEND_TAG
#     run_migrations   true|false (control-plane only)
#     run_seed         true|false (control-plane only)
#
# Required env (repository variables): GCP_REGION, GCP_PROJECT_ID, AR_REPO, SECRET_PREFIX,
#   VM_NAME, VM_ZONE. Provided by Bitbucket: BITBUCKET_COMMIT.
set -euo pipefail

service="${1:?usage: bb-deploy-vm.sh <service> <tag_var> <run_migrations> <run_seed>}"
tag_var="${2:?missing tag_var}"
run_migrations="${3:-false}"
run_seed="${4:-false}"

: "${GCP_REGION:?repository variable GCP_REGION is required}"
: "${GCP_PROJECT_ID:?repository variable GCP_PROJECT_ID is required}"
: "${AR_REPO:?repository variable AR_REPO is required}"
: "${SECRET_PREFIX:?repository variable SECRET_PREFIX is required}"
: "${VM_NAME:?repository variable VM_NAME is required}"
: "${VM_ZONE:?repository variable VM_ZONE is required}"
: "${BITBUCKET_COMMIT:?BITBUCKET_COMMIT not set}"

# jq parses env-manifest.json below; google/cloud-sdk:slim doesn't ship it. No-op if present.
if ! command -v jq >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq jq >/dev/null
fi

here="$(cd "$(dirname "$0")" && pwd)"
deploy_dir="$(dirname "$here")"           # .../deploy
registry_host="${GCP_REGION}-docker.pkg.dev"
ar_image_base="${registry_host}/${GCP_PROJECT_ID}/${AR_REPO}"

# Resolve this service's required runtime env names from env-manifest.json (the deploy scripts'
# single source of truth). The manifest keys the two backend apps as control-plane / worker;
# the agent-worker compose service maps to the "worker" key. Frontend has no runtime `required`
# list (its env is baked at build), so this is empty for it.
case "$service" in
  control-plane) key=control-plane ;;
  agent-worker)  key=worker ;;
  frontend)      key=frontend ;;
  *) echo "unknown service '$service'" >&2; exit 1 ;;
esac
required="$(jq -r --arg k "$key" '.[$k].required // [] | join(" ")' "$deploy_dir/env-manifest.json")"

# Ship the deploy assets (repo stays the source of truth — same five files GitHub scp'd).
gcloud compute scp --tunnel-through-iap --zone "$VM_ZONE" --project "$GCP_PROJECT_ID" \
  "$deploy_dir/docker-compose.dev.yml" \
  "$deploy_dir/remote-deploy.sh" \
  "$deploy_dir/render-env.sh" \
  "$deploy_dir/secrets.map" \
  "$deploy_dir/verify-app-env.sh" \
  "$VM_NAME:/opt/vera/"

# Run the deploy on the VM. Arg order matches remote-deploy.sh exactly:
#   SERVICE TAG_VAR SHA AR_IMAGE_BASE REGISTRY_HOST SECRET_PREFIX PROJECT RUN_MIGRATIONS REQUIRED RUN_SEED
gcloud compute ssh "$VM_NAME" --tunnel-through-iap --zone "$VM_ZONE" --project "$GCP_PROJECT_ID" \
  --command "bash /opt/vera/remote-deploy.sh '$service' '$tag_var' '$BITBUCKET_COMMIT' '$ar_image_base' '$registry_host' '$SECRET_PREFIX' '$GCP_PROJECT_ID' '$run_migrations' '$required' '$run_seed'"
