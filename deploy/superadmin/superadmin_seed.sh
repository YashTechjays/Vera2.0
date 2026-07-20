#!/usr/bin/env bash
# Runs ON the VM (invoked by an operator over an IAP SSH tunnel, once).
# Seeds platform operator #1 — the first SUPER_ADMIN — which nothing else creates
# (`seed.py` makes only a TENANT_ADMIN). Idempotent: a no-op once any platform operator
# exists, so a second run is harmless but also cannot rotate creds or reset MFA.
#
#   superadmin_seed.sh SECRET_PREFIX PROJECT
#
# NOT wired into the deploy pipeline on purpose: on first creation the bootstrap prints a
# one-time otpauth:// MFA URI that must be scanned to log in. A human runs this and captures
# that URI live from the terminal — an unattended run would bury it in CI logs (→ lockout).
# You normally invoke this via superadmin_runme.sh (the laptop launcher), not directly.
set -euo pipefail

SECRET_PREFIX="$1"  # e.g. vera-test
PROJECT="$2"        # GCP project id

cd /opt/vera
export COMPOSE_FILE=/opt/vera/docker-compose.dev.yml

# Fetch a "${SECRET_PREFIX}-<name>" secret from Secret Manager via the VM's attached SA —
# the same mechanism the migrate/seed one-offs use in remote-deploy.sh.
fetch_secret() {
  gcloud secrets versions access latest --project="$PROJECT" --secret="${SECRET_PREFIX}-$1"
}

# Bootstrap writes NULL-tenant platform rows (and the envelope-encrypted MFA seed onto a
# NULL-tenant identity), which the least-privilege app role cannot — so it runs as a one-off
# on the privileged (BYPASSRLS) migration connection, exactly like seed.py. The creds never
# touch app.env / the running container. LOCAL_KMS_MASTER_KEY comes from app.env (env_file).
docker compose run --rm \
  -e VERA_DATABASE_URL="$(fetch_secret migration-database-url)" \
  -e BOOTSTRAP_ADMIN_EMAIL="$(fetch_secret superadmin-email)" \
  -e BOOTSTRAP_ADMIN_PASSWORD="$(fetch_secret superadmin-password)" \
  control-plane python scripts/bootstrap_platform_admin.py
