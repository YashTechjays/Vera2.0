#!/usr/bin/env bash
# Phase 2 (on the VM). Runs inside remote-deploy.sh AFTER render-env.sh has written app.env and
# BEFORE the container is (re)started. Asserts every required env var for the deployed service is
# present AND non-empty in app.env, so an empty secret or an unmapped var fails the deploy before
# anything goes live (pairs with the health gate). Never prints values.
#
# The required list is passed IN as args — CI already resolved it from env-manifest.json with jq,
# so the VM needs no JSON parser. An empty list (e.g. frontend, whose env is baked at build) is a
# valid no-op.
#
#   verify-app-env.sh <service> <app_env_file> [REQUIRED_VAR...]
set -euo pipefail

service="${1:?usage: verify-app-env.sh <service> <app_env_file> [VAR...]}"
env_file="${2:?usage: verify-app-env.sh <service> <app_env_file> [VAR...]}"
shift 2

[ $# -eq 0 ] && { echo "verify-app-env: OK — no required env for '$service'."; exit 0; }
[ -f "$env_file" ] || { echo "verify-app-env: missing env file $env_file" >&2; exit 1; }

missing=()
for var in "$@"; do
  # Present + non-empty is `VAR=<at least one char>`; `VAR=` (empty) or absent both fail.
  grep -qE "^${var}=.+" "$env_file" || missing+=("$var")
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "verify-app-env: FAILED for '$service' — required env missing/empty in the rendered env:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  echo "  → set each secret in Secret Manager and map it in deploy/secrets.map, then re-deploy." >&2
  exit 1
fi
echo "verify-app-env: OK — all '$service' required env present."
