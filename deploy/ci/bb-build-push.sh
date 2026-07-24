#!/usr/bin/env bash
# Build one component image and push it to Artifact Registry — the Bitbucket port of the
# GitHub _build-image.yml reusable. Tags the image with the immutable commit SHA (what the
# deploy pins) plus a moving `dev` tag, then runs a Trivy scan.
#
#   bb-build-push.sh <component> <context> <dockerfile> [build_args]
#     component   AR image name / GitHub "component": control-plane | agent-worker | frontend
#     context     docker build context dir (vera-backend | vera-frontend)
#     dockerfile  path to the Dockerfile
#     build_args  optional, space-separated "K=V K=V" (frontend passes VITE_API_BASE_URL)
#
# Assumes bb-gcp-auth.sh has already run in this step. Required env (repository variables):
#   GCP_REGION, GCP_PROJECT_ID, AR_REPO. Provided by Bitbucket: BITBUCKET_COMMIT.
# Optional: TRIVY_EXIT_CODE (default 0 = warn; set 1 to block on CRITICAL/HIGH fixable CVEs).
set -euo pipefail

component="${1:?usage: bb-build-push.sh <component> <context> <dockerfile> [build_args]}"
context="${2:?missing build context}"
dockerfile="${3:?missing dockerfile path}"
build_args="${4:-}"

: "${GCP_REGION:?repository variable GCP_REGION is required}"
: "${GCP_PROJECT_ID:?repository variable GCP_PROJECT_ID is required}"
: "${AR_REPO:?repository variable AR_REPO is required}"
: "${BITBUCKET_COMMIT:?BITBUCKET_COMMIT not set}"
TRIVY_EXIT_CODE="${TRIVY_EXIT_CODE:-0}"   # warn by default; flip to 1 once the CVE backlog is triaged

# google/cloud-sdk:slim ships neither jq nor docker. verify-config.sh needs jq; install it up
# front (no-op if present). Ports the GitHub verify-* pre-build jobs, run here as a fail-closed
# gate BEFORE the build so a missing/unmapped env var stops us before we build anything.
command -v jq >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq jq >/dev/null; }
case "$component" in
  control-plane) bash deploy/verify-config.sh control-plane ;;
  agent-worker)  bash deploy/verify-config.sh worker ;;
  frontend)      bash deploy/verify-frontend-build-env.sh ;;
  *) echo "unknown component '$component'" >&2; exit 1 ;;
esac

registry_host="${GCP_REGION}-docker.pkg.dev"
image="${registry_host}/${GCP_PROJECT_ID}/${AR_REPO}/${component}"

# google/cloud-sdk:slim ships gcloud but not the docker CLI; install it once (the daemon comes
# from the step's `docker` service, so we only need the client). No-op if already present.
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq docker.io >/dev/null
fi

# The frontend imports the backend schema JSON from the sibling vera-backend/ tree; stage a
# copy inside the build context so the import resolves (same as the GitHub build).
if [ "$component" = "frontend" ]; then
  ( cd vera-frontend && ./scripts/stage-backend-schema-for-docker.sh )
fi

gcloud auth configure-docker "$registry_host" --quiet

# Registry-tag cache: Bitbucket has no GHA layer cache, so warm from the last pushed :dev image.
docker pull "$image:dev" 2>/dev/null || echo "no :dev cache yet — cold build"

# shellcheck disable=SC2086  # build_args is intentionally word-split into repeated --build-arg
ba_flags=""
for kv in $build_args; do ba_flags="$ba_flags --build-arg $kv"; done

# shellcheck disable=SC2086
docker build \
  --cache-from "$image:dev" \
  -f "$dockerfile" \
  -t "$image:$BITBUCKET_COMMIT" \
  -t "$image:dev" \
  $ba_flags \
  "$context"

docker push "$image:$BITBUCKET_COMMIT"
docker push "$image:dev"
echo "Pushed $image:$BITBUCKET_COMMIT and $image:dev"

# Scan the image we just built. Trivy reads the local docker daemon (DOCKER_HOST from the
# service). Warn mode by default to match the repo's staged scanner rollout.
if ! command -v trivy >/dev/null 2>&1; then
  command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl >/dev/null; }
  curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
    | sh -s -- -b /usr/local/bin >/dev/null
fi
trivy image \
  --severity CRITICAL,HIGH \
  --ignore-unfixed \
  --exit-code "$TRIVY_EXIT_CODE" \
  "$image:$BITBUCKET_COMMIT"
