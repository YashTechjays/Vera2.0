#!/usr/bin/env bash
# Phase 1 (pre-VM, runs in CI). Assert each required frontend BUILD var is present & non-empty in
# the environment before the image is built — Vite bakes these into the bundle, so an empty one
# ships a broken app. Values come from GitHub repository variables injected into this step's env.
# Needs `jq` (present on CI runners); the required list is the single source of truth in
# env-manifest.json (frontend.buildRequired).
#
#   verify-frontend-build-env.sh
set -euo pipefail

here="$(dirname "$0")"
manifest="$here/env-manifest.json"
[ -f "$manifest" ] || { echo "verify-frontend-build-env: missing $manifest" >&2; exit 1; }

required="$(jq -r '.frontend.buildRequired // empty | .[]' "$manifest")"
[ -n "$required" ] || { echo "verify-frontend-build-env: no frontend.buildRequired in $manifest" >&2; exit 1; }

missing=()
while IFS= read -r var; do
  [ -z "$var" ] && continue
  # Indirect expansion: is the env var *named by* $var set to a non-empty value?
  [ -n "${!var:-}" ] || missing+=("$var")
done <<< "$required"

if [ ${#missing[@]} -gt 0 ]; then
  echo "verify-frontend-build-env: FAILED — required build var(s) missing/empty:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  echo "  → set them as GitHub repository variables before building." >&2
  exit 1
fi
echo "verify-frontend-build-env: OK — all frontend build env present."
