#!/usr/bin/env bash
# Phase 1 (pre-VM, runs in CI). Assert every env var a backend component requires is actually
# SUPPLIED by the deploy pipeline — i.e. its name appears on the RHS of secrets.map (rendered
# from Secret Manager) or is one of render-env.sh's built-ins. A required var that nothing
# supplies (e.g. a newly-introduced one that was never wired up) fails here, before any image is
# built. No secret VALUES are read — this is a static config-completeness check. Needs `jq`
# (present on CI runners); the required list is the single source of truth in env-manifest.json.
#
#   verify-config.sh <control-plane|worker>
set -euo pipefail

component="${1:?usage: verify-config.sh <control-plane|worker>}"
here="$(dirname "$0")"
manifest="$here/env-manifest.json"
secrets_map="$here/secrets.map"

[ -f "$manifest" ] || { echo "verify-config: missing $manifest" >&2; exit 1; }
[ -f "$secrets_map" ] || { echo "verify-config: missing $secrets_map" >&2; exit 1; }

required="$(jq -r --arg c "$component" '.[$c].required // empty | .[]' "$manifest")"
[ -n "$required" ] || { echo "verify-config: no 'required' list for '$component' in $manifest" >&2; exit 1; }

# Names verify-config treats as supplied: the env-var names on the RHS of secrets.map (a basename
# may fan out to several, comma-separated) plus the settings render-env.sh writes directly.
supplied="$(
  sed 's/#.*//' "$secrets_map" \
    | awk -F= 'NF > 1 { gsub(/[[:space:]]/, "", $2); print $2 }' \
    | tr ',' '\n' \
    | grep -v '^$'
)"
supplied+=$'\nVERA_ENV\nVERA_LOG_LEVEL'

missing=()
while IFS= read -r var; do
  [ -z "$var" ] && continue
  # Each supplied name sits on its own line; match it whole-line and literally (-x, -F).
  grep -qxF "$var" <<< "$supplied" || missing+=("$var")
done <<< "$required"

if [ ${#missing[@]} -gt 0 ]; then
  echo "verify-config: FAILED for '$component' — required env not supplied by the pipeline:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  echo "  → map each in deploy/secrets.map and create the secret in Secret Manager." >&2
  exit 1
fi
echo "verify-config: OK — all '$component' required env is supplied by the pipeline."
