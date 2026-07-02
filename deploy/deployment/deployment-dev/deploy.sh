#!/bin/bash
# deploy.sh — VERA deployment orchestrator, run from vera-jumpserver-vm.
#
# All application deployment is SSH-driven from this jumpserver.
# Terraform manages infrastructure only — VMs, networking, IAM, secrets.
#
# Usage:
#   ./deploy.sh                    # full deploy: build images + deploy all 5 components
#   ./deploy.sh --worker           # rebuild + redeploy agent worker only
#   ./deploy.sh --control-plane    # rebuild + redeploy control plane only
#   ./deploy.sh --livekit          # refresh livekit-server config + restart
#   ./deploy.sh --sip              # refresh livekit-sip config + restart
#   ./deploy.sh --provision-trunk  # register self-hosted outbound SIP trunk (on livekit-vm, idempotent)
#   TEST_NUMBER=+1555... ./deploy.sh --test-outbound   # place a test outbound call from livekit-vm (add CLEANUP=1 to auto-delete the room)
#   ./deploy.sh --frontend         # build frontend + push to GCS + nginx reload
#   ./deploy.sh --secrets          # re-fetch secrets on all VMs + restart (no image rebuild)
#   ./deploy.sh --migrate          # run DB migrations only (alembic upgrade head)
#   ./deploy.sh --seed             # seed DB — permissions, roles, tenant, admin user, form schemas
#   ./deploy.sh --setup            # first-time: install Docker on all VMs
#   ./deploy.sh --build-only       # Cloud Build only — build images, do not deploy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # two levels up → repo root (vera-backend, vera-frontend)
CLOUDBUILD_DIR="$SCRIPT_DIR/cloudbuild"
APP_DIR="$SCRIPT_DIR/techjays-app"
LIVEKIT_DIR="$SCRIPT_DIR/livekit-app"

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="us-central1"
ZONE="us-central1-a"
VERA_ENV="dev"

WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/vera-repo/agent-worker:latest"
CONTROL_PLANE_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/vera-repo/control-plane:latest"
FRONTEND_BUCKET="vera-frontend-${PROJECT_ID}"
TERRAFORM_SA="vera-non-prod-terraform-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# ── Parse flags ───────────────────────────────────────────────────────────────

DO_WORKER=false
DO_CONTROL_PLANE=false
DO_LIVEKIT=false
DO_SIP=false
DO_PROVISION_TRUNK=false
DO_TEST_OUTBOUND=false
DO_FRONTEND=false
DO_SECRETS=false
DO_MIGRATE=false
DO_SEED=false
DO_SETUP=false
DO_BUILD_ONLY=false
SKIP_BUILD=false
FULL_DEPLOY=true

for arg in "$@"; do
  case "$arg" in
    --worker)        DO_WORKER=true;        FULL_DEPLOY=false ;;
    --control-plane) DO_CONTROL_PLANE=true; FULL_DEPLOY=false ;;
    --livekit)       DO_LIVEKIT=true;       FULL_DEPLOY=false ;;
    --sip)           DO_SIP=true;           FULL_DEPLOY=false ;;
    --provision-trunk) DO_PROVISION_TRUNK=true; FULL_DEPLOY=false ;;
    --test-outbound)   DO_TEST_OUTBOUND=true;   FULL_DEPLOY=false ;;
    --frontend)      DO_FRONTEND=true;      FULL_DEPLOY=false ;;
    --secrets)       DO_SECRETS=true;       FULL_DEPLOY=false ;;
    --migrate)       DO_MIGRATE=true;       FULL_DEPLOY=false ;;
    --seed)          DO_SEED=true;          FULL_DEPLOY=false ;;
    --setup)         DO_SETUP=true;         FULL_DEPLOY=false ;;
    --build-only)    DO_BUILD_ONLY=true;    FULL_DEPLOY=false ;;
    --skip-build)    SKIP_BUILD=true ;;
    *) echo "Unknown flag: $arg"; exit 1 ;;
  esac
done

if [[ "$FULL_DEPLOY" == "true" ]]; then
  DO_WORKER=true
  DO_CONTROL_PLANE=true
  DO_LIVEKIT=true
  DO_SIP=true
  DO_FRONTEND=true
fi

# ── Helper: SSH into a VM and run a script ────────────────────────────────────
ssh_run() {
  local vm="$1"
  local env_exports="$2"
  local script="$3"
  echo "  → SSH into $vm..."
  gcloud compute ssh "$vm" \
    --tunnel-through-iap \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    -- "sudo bash -s" < <(printf '%s\n' "${env_exports}"; cat "${script}")
}

# ── Resolve LiveKit internal IP (only needed for self-hosted livekit/sip) ────
LIVEKIT_INTERNAL_IP=""
if [[ "$DO_SIP" == "true" || "$DO_LIVEKIT" == "true" || "$FULL_DEPLOY" == "true" ]]; then
  echo "Querying LiveKit VM internal IP..."
  LIVEKIT_INTERNAL_IP=$(gcloud compute instances describe vera-livekit-vm \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    --format='get(networkInterfaces[0].networkIP)')
  echo "  LiveKit internal IP: $LIVEKIT_INTERNAL_IP"
fi

# ── Step: First-time VM setup ─────────────────────────────────────────────────
if [[ "$DO_SETUP" == "true" ]]; then
  echo ""
  echo "==> [setup] Installing Docker on all VMs..."
  SETUP_SCRIPT='
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io gettext-base curl
  systemctl enable docker
fi
systemctl start docker || true
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
echo "VM setup complete."
'
  for vm in vera-livekit-vm vera-sip-vm vera-worker-vm vera-control-plane-vm; do
    echo "  → $vm"
    gcloud compute ssh "$vm" \
      --tunnel-through-iap \
      --zone="$ZONE" \
      --project="$PROJECT_ID" \
      -- "sudo bash -s" <<< "$SETUP_SCRIPT"
  done
  echo "==> [setup] Done. Now run ./deploy.sh --migrate then ./deploy.sh"
  exit 0
fi

# ── Step: Build images ────────────────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "false" ]]; then
  if [[ "$DO_WORKER" == "true" || "$DO_BUILD_ONLY" == "true" ]]; then
    echo ""
    echo "==> Building agent-worker image..."
    gcloud builds submit "$REPO_ROOT" \
      --config="$CLOUDBUILD_DIR/cloudbuild-worker.yaml" \
      --project="$PROJECT_ID"
  fi

  if [[ "$DO_CONTROL_PLANE" == "true" || "$DO_BUILD_ONLY" == "true" ]]; then
    echo ""
    echo "==> Building control-plane image..."
    gcloud builds submit "$REPO_ROOT" \
      --config="$CLOUDBUILD_DIR/cloudbuild-control-plane.yaml" \
      --project="$PROJECT_ID"
  fi

  if [[ "$DO_BUILD_ONLY" == "true" ]]; then
    echo "==> [build-only] Images built. Skipping deploy."
    exit 0
  fi
fi

# ── Step: Deploy LiveKit server ───────────────────────────────────────────────
if [[ "$DO_LIVEKIT" == "true" ]]; then
  echo ""
  echo "==> Deploying livekit-server to vera-livekit-vm..."
  ssh_run "vera-livekit-vm" "" "$LIVEKIT_DIR/livekit-server/start.sh"
fi

# ── Step: Deploy SIP bridge ───────────────────────────────────────────────────
if [[ "$DO_SIP" == "true" ]]; then
  echo ""
  echo "==> Deploying livekit-sip to vera-sip-vm..."
  ssh_run "vera-sip-vm" \
    "export LIVEKIT_SERVER_IP='${LIVEKIT_INTERNAL_IP}';" \
    "$LIVEKIT_DIR/livekit-sip/start.sh"
fi

# ── Step: Provision self-hosted outbound SIP trunk ────────────────────────────
if [[ "$DO_PROVISION_TRUNK" == "true" ]]; then
  echo ""
  echo "==> Provisioning self-hosted outbound SIP trunk on vera-livekit-vm..."
  ssh_run "vera-livekit-vm" "" "$LIVEKIT_DIR/livekit-server/provision-trunk.sh"
fi

# ── Step: Test outbound call (runs on vera-livekit-vm) ────────────────────────
if [[ "$DO_TEST_OUTBOUND" == "true" ]]; then
  : "${TEST_NUMBER:?Set TEST_NUMBER=+1... e.g. TEST_NUMBER=+15551234567 ./deploy.sh --test-outbound}"
  echo ""
  echo "==> Placing test outbound call from vera-livekit-vm to ${TEST_NUMBER}..."
  # Trunk id is read here (your gcloud identity has access) and passed to the VM,
  # whose SA is read-only on the API creds but not on the trunk-id secret.
  TRUNK_ID=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-sip-trunk-id)
  ssh_run "vera-livekit-vm" \
    "export TEST_NUMBER='${TEST_NUMBER}'; export TRUNK_ID='${TRUNK_ID}'; export ROOM='${ROOM:-test-outbound}'; export CLEANUP='${CLEANUP:-0}';" \
    "$LIVEKIT_DIR/livekit-sip/test-outbound-vm.sh"
fi

# ── Step: Deploy agent worker ─────────────────────────────────────────────────
if [[ "$DO_WORKER" == "true" ]]; then
  echo ""
  echo "==> Deploying agent-worker to vera-worker-vm..."
  ssh_run "vera-worker-vm" \
    "export WORKER_IMAGE='${WORKER_IMAGE}'; export VERA_ENV='${VERA_ENV}';" \
    "$APP_DIR/start-worker.sh"
fi

# ── Step: Deploy control plane ────────────────────────────────────────────────
if [[ "$DO_CONTROL_PLANE" == "true" ]]; then
  echo ""
  echo "==> Deploying control-plane to vera-control-plane-vm..."
  ssh_run "vera-control-plane-vm" \
    "export CONTROL_PLANE_IMAGE='${CONTROL_PLANE_IMAGE}'; export VERA_ENV='${VERA_ENV}'; export VERA_GCP_PROJECT='${PROJECT_ID}';" \
    "$APP_DIR/start-control-plane.sh"
fi

# ── Step: Deploy frontend ─────────────────────────────────────────────────────
if [[ "$DO_FRONTEND" == "true" ]]; then
  echo ""
  echo "==> Building and deploying frontend..."
  PROJECT_ID="$PROJECT_ID" ZONE="$ZONE" BUCKET="$FRONTEND_BUCKET" SKIP_BUILD="$SKIP_BUILD" \
    bash "$APP_DIR/start-frontend.sh"
fi

# ── Step: DB migrations ───────────────────────────────────────────────────────
if [[ "$DO_MIGRATE" == "true" ]]; then
  echo ""
  echo "==> Running DB migrations on vera-control-plane-vm..."
  gcloud compute ssh vera-control-plane-vm \
    --tunnel-through-iap \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    -- "sudo bash -s" << EOF
set -euo pipefail
LK_API_KEY=\$(gcloud secrets versions access latest --secret=vera-livekit-api-key)
LK_API_SECRET=\$(gcloud secrets versions access latest --secret=vera-livekit-api-secret)
DATABASE_URL=\$(gcloud secrets versions access latest --secret=vera-database-url)
REDIS_URL=\$(gcloud secrets versions access latest --secret=vera-redis-url)
KMS_KEY=\$(gcloud secrets versions access latest --secret=vera-local-kms-master-key)
POSTGRES_PASSWORD=\$(gcloud secrets versions access latest --secret=vera-postgres-password)

# Migrations run as postgres superuser to bypass RLS policies in data-seeding steps
DB_HOST=\$(echo "\$DATABASE_URL" | sed 's|.*@\(.*\):5432.*|\1|')
POSTGRES_URL="postgresql+asyncpg://postgres:\${POSTGRES_PASSWORD}@\${DB_HOST}:5432/vera_db"

echo "Running alembic upgrade head..."
docker run --rm \
  -e VERA_ENV="${VERA_ENV}" \
  -e VERA_GCP_PROJECT="${PROJECT_ID}" \
  -e VERA_LIVEKIT_URL="ws://${LIVEKIT_INTERNAL_IP}:7880" \
  -e LIVEKIT_API_KEY="\$LK_API_KEY" \
  -e LIVEKIT_API_SECRET="\$LK_API_SECRET" \
  -e VERA_DATABASE_URL="\$POSTGRES_URL" \
  -e VERA_REDIS_URL="\$REDIS_URL" \
  -e LOCAL_KMS_MASTER_KEY="\$KMS_KEY" \
  "${CONTROL_PLANE_IMAGE}" \
  alembic upgrade head
echo "Migrations complete."
EOF
fi

# ── Step: Seed database ───────────────────────────────────────────────────────
if [[ "$DO_SEED" == "true" ]]; then
  echo ""
  echo "==> Running seed script on vera-control-plane-vm..."
  gcloud compute ssh vera-control-plane-vm \
    --tunnel-through-iap \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    -- "sudo bash -s" << EOF
set -euo pipefail
DATABASE_URL=\$(gcloud secrets versions access latest --secret=vera-database-url)
REDIS_URL=\$(gcloud secrets versions access latest --secret=vera-redis-url)
KMS_KEY=\$(gcloud secrets versions access latest --secret=vera-local-kms-master-key)

echo "Running seed script..."
docker run --rm \
  -e VERA_ENV="${VERA_ENV}" \
  -e VERA_GCP_PROJECT="${PROJECT_ID}" \
  -e VERA_DATABASE_URL="\$DATABASE_URL" \
  -e VERA_REDIS_URL="\$REDIS_URL" \
  -e LOCAL_KMS_MASTER_KEY="\$KMS_KEY" \
  "${CONTROL_PLANE_IMAGE}" \
  python scripts/seed.py
echo "Seed complete."
EOF
fi

# ── Step: Secrets-only refresh ────────────────────────────────────────────────
if [[ "$DO_SECRETS" == "true" ]]; then
  echo ""
  echo "==> Refreshing secrets on all VMs..."
  ssh_run "vera-livekit-vm" "" "$LIVEKIT_DIR/livekit-server/start.sh"
  ssh_run "vera-sip-vm" \
    "export LIVEKIT_SERVER_IP='${LIVEKIT_INTERNAL_IP}';" \
    "$LIVEKIT_DIR/livekit-sip/start.sh"
  ssh_run "vera-worker-vm" \
    "export WORKER_IMAGE='${WORKER_IMAGE}'; export VERA_ENV='${VERA_ENV}';" \
    "$APP_DIR/start-worker.sh"
  ssh_run "vera-control-plane-vm" \
    "export CONTROL_PLANE_IMAGE='${CONTROL_PLANE_IMAGE}'; export VERA_ENV='${VERA_ENV}'; export VERA_GCP_PROJECT='${PROJECT_ID}';" \
    "$APP_DIR/start-control-plane.sh"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "==========================================================================="
echo " Deploy complete!"
echo "==========================================================================="
