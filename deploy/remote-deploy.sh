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
    # Migrations seed NULL-tenant platform rows under FORCE RLS that the app role cannot write,
    # so run alembic on a privileged (BYPASSRLS/superuser) connection. Fetched HERE — before the
    # stop, so a missing secret fails with no downtime — and used ONLY for this one-off, never
    # written into app.env / the running container (which keeps the least-privilege app role).
    migration_db_url="$(gcloud secrets versions access latest \
      --project="$PROJECT" --secret="${SECRET_PREFIX}-migration-database-url")"

    # Pending migration: stop the old control-plane FIRST so the previous code never
    # serves against the newly-migrated schema (no backward-compat overlap). Brief,
    # deliberate downtime — only when a real schema change lands.
    echo "Pending migration; stopping control-plane for migration (brief downtime)..." >&2
    docker compose stop control-plane
    docker compose run --rm -e VERA_DATABASE_URL="$migration_db_url" control-plane alembic upgrade head
  fi
fi

# Bring the service up and BLOCK until it is healthy. `--wait` exits non-zero if
# the container never reaches healthy (or exits) within the timeout, so a
# crash-looping image fails the deploy instead of reporting success. On failure,
# dump recent logs so CI shows why.
if ! docker compose up -d --wait --wait-timeout 120 "$SERVICE"; then
  echo "Deploy of $SERVICE did not become healthy; recent logs:" >&2
  docker compose logs --tail=100 "$SERVICE" >&2 || true
  exit 1
fi

# Reclaim unused images older than 7 days (not just dangling ones). Each deploy leaves the
# previous :<sha> image tagged, so a dangling-only prune never removes them; -a + an age
# filter does. The running image is "in use" and is never touched; anything removed is still
# in Artifact Registry, so a manual rollback just re-pulls.
docker image prune -af --filter "until=168h"
