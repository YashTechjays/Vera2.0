#!/usr/bin/env bash
# Runs ON the VM (invoked by the deploy pipeline over an IAP SSH tunnel).
# Pins one service to a new image tag, refreshes the env from Secret Manager,
# pulls, optionally migrates, restarts.
#
#   remote-deploy.sh SERVICE TAG_VAR SHA AR_IMAGE_BASE REGISTRY_HOST \
#                    SECRET_PREFIX PROJECT [RUN_MIGRATIONS] [REQUIRED_VARS]
#
# Two config files live beside the compose file in /opt/vera:
#   .env      — image coordinates for compose ${...} interpolation (managed here).
#   app.env   — the container runtime config/secrets, rendered from Secret Manager
#               by render-env.sh (injected via the compose env_file).
set -euo pipefail

SERVICE="$1"        # compose service: frontend | control-plane | agent-worker
TAG_VAR="$2"        # .env var to rewrite: FRONTEND_TAG | CONTROL_PLANE_TAG | WORKER_TAG
SHA="$3"            # image tag to deploy (git commit SHA)
AR_IMAGE_BASE="$4"  # <region>-docker.pkg.dev/<project>/<repo>
REGISTRY_HOST="$5"  # <region>-docker.pkg.dev
SECRET_PREFIX="$6"  # e.g. vera-test
PROJECT="$7"        # GCP project id
RUN_MIGRATIONS="${8:-false}"
REQUIRED_VARS="${9:-}"  # space-separated env names for this service; CI resolved them from env-manifest.json
RUN_SEED="${10:-false}" # control-plane only: run the idempotent DB seed after migrations

cd /opt/vera
export COMPOSE_FILE=/opt/vera/docker-compose.dev.yml
touch .env

# Idempotently set KEY=VALUE in .env (compose reads it for ${...} interpolation).
upsert() {
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
upsert AR_IMAGE_BASE "$AR_IMAGE_BASE"
upsert "$TAG_VAR" "$SHA"

# Fetch the latest version of a "${SECRET_PREFIX}-<name>" secret from Secret Manager
# (via the VM's attached SA). Used for the privileged one-off migrate/seed steps below.
fetch_secret() {
  gcloud secrets versions access latest --project="$PROJECT" --secret="${SECRET_PREFIX}-$1"
}

# Render the container env from Secret Manager (VM's attached SA), so a rotated
# secret is picked up on the next deploy.
bash /opt/vera/render-env.sh "$SECRET_PREFIX" "$PROJECT" /opt/vera/app.env

# Fail the deploy BEFORE touching the running service if the rendered env is incomplete
# (a required var missing or empty). Fail-closed: nothing broken goes live. The required list
# is word-split from CI (resolved from env-manifest.json there); empty is a valid no-op.
# shellcheck disable=SC2086
bash /opt/vera/verify-app-env.sh "$SERVICE" /opt/vera/app.env $REQUIRED_VARS

# Authenticate docker to Artifact Registry using the VM's attached service
# account — no registry password is ever stored on the box.
gcloud auth configure-docker "$REGISTRY_HOST" --quiet

docker compose pull "$SERVICE"

# Both migrating and seeding write rows the least-privilege app role cannot (NULL-tenant
# platform rows / global + cross-tenant catalog under FORCE RLS), so both run on a privileged
# (BYPASSRLS/superuser) connection. Fetch it ONCE, up front — before any service stop, so a
# missing secret fails with no downtime — and use it ONLY for these one-offs, never writing it
# into app.env / the running container (which keeps the least-privilege app role).
migration_db_url=""
if [ "$RUN_MIGRATIONS" = "true" ] || [ "$RUN_SEED" = "true" ]; then
  migration_db_url="$(fetch_secret migration-database-url)"
fi

# Migrate only when the DB is behind head, and only for control-plane deploys.
# The check runs against the OLD control-plane, still serving: `alembic current`
# prints "(head)" when the DB is up to date; anything else (older rev, or empty on
# a fresh DB) means a migration is pending. Capturing to a var (no pipe) lets a hard
# failure abort BEFORE we stop anything, so a flaky check never costs downtime.
if [ "$RUN_MIGRATIONS" = "true" ]; then
  current=$(docker compose run --rm control-plane alembic current 2>/dev/null)
  if printf '%s' "$current" | grep -q '(head)'; then
    echo "DB already at head; skipping migration, no downtime." >&2
  else
    # Pending migration: stop the old control-plane FIRST so the previous code never
    # serves against the newly-migrated schema (no backward-compat overlap). Brief,
    # deliberate downtime — only when a real schema change lands.
    echo "Pending migration; stopping control-plane for migration (brief downtime)..." >&2
    docker compose stop control-plane
    docker compose run --rm -e VERA_DATABASE_URL="$migration_db_url" control-plane alembic upgrade head
  fi
fi

# Seed the DB (idempotent) when requested — control-plane deploys only. Runs as a one-off on
# the same privileged connection; seed.py existence-checks every row, so re-runs are no-ops.
# The sample-tenant admin creds come from Secret Manager (never baked into app.env); redact the
# password seed.py echoes on its final line so it never lands in the deploy log.
if [ "$RUN_SEED" = "true" ]; then
  seed_email="$(fetch_secret seed-admin-email)"
  seed_pw="$(fetch_secret seed-admin-password)"
  echo "Seeding database (idempotent)..." >&2
  docker compose run --rm \
    -e VERA_DATABASE_URL="$migration_db_url" \
    -e SEED_TENANT_SLUG="vera-health-example" \
    -e SEED_ADMIN_EMAIL="$seed_email" \
    -e SEED_ADMIN_PASSWORD="$seed_pw" \
    control-plane python scripts/seed.py \
    | sed 's/"password": "[^"]*"/"password": "***"/'
fi

# Bring the service up and BLOCK until it is healthy. `--wait` exits non-zero if
# the container never reaches healthy (or exits) within the timeout, so a
# crash-looping image fails the deploy instead of reporting success. On failure,
# dump recent logs so CI shows why.
if ! docker compose up -d --wait --wait-timeout 120 --force-recreate "$SERVICE"; then
  echo "Deploy of $SERVICE did not become healthy; recent logs:" >&2
  docker compose logs --tail=100 "$SERVICE" >&2 || true
  exit 1
fi

# Cap local image growth: keep only the 2 newest images for the service just deployed (the
# running one + one rollback target), and drop older :<sha> tags regardless of age. An
# age-based prune (the old `until=168h`) never bounded the COUNT, so frequent deploys filled
# the VM disk within ~3 days, well inside the window. `docker images` lists newest-first;
# awk dedupes IDs (a tag may point at an already-seen image); `tail -n +3` is everything past
# the newest 2. The running image is never lost — Docker refuses to remove an image in use by
# a running container, so even a rollback to an older image survives (the failed rmi is
# swallowed). Removed images still live in Artifact Registry, so a deeper rollback re-pulls.
# Best-effort (`|| true`): cleanup must never fail an already-healthy deploy.
repo="${AR_IMAGE_BASE}/${SERVICE}"
docker images "$repo" --format '{{.ID}}' | awk '!seen[$0]++' | tail -n +3 \
  | xargs -r docker rmi -f >/dev/null 2>&1 || true

# The per-service pass above only bounds *tagged* growth for this repo; also reclaim
# dangling (untagged) layers left by the occasional corrupt pull or stale cache. This is
# safe — a dangling prune never touches a tagged or in-use image — and keeps those from
# creeping up over weeks now that the old blanket `prune -af` is gone.
docker image prune -f >/dev/null 2>&1 || true
