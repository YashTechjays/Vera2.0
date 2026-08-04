#!/usr/bin/env bash
# Keyless GCP auth for a Bitbucket Pipelines step (the Bitbucket analogue of GitHub's
# google-github-actions/auth). The step must declare `oidc: true`, which makes Bitbucket
# mint a short-lived OIDC token in $BITBUCKET_STEP_OIDC_TOKEN. We exchange it for the
# deployer service account's credentials via Workload Identity Federation — no JSON key
# ever touches the runner.
#
# Required env (Bitbucket repository variables):
#   GCP_WIF_PROVIDER  full provider resource, e.g.
#                     //iam.googleapis.com/projects/N/locations/global/workloadIdentityPools/POOL/providers/bitbucket
#   GCP_DEPLOYER_SA   deployer SA email to impersonate (same SA the GitHub pipeline used)
#   GCP_PROJECT_ID    project to set as active
set -euo pipefail

: "${BITBUCKET_STEP_OIDC_TOKEN:?not set — the step needs 'oidc: true'}"
: "${GCP_WIF_PROVIDER:?repository variable GCP_WIF_PROVIDER is required}"
: "${GCP_DEPLOYER_SA:?repository variable GCP_DEPLOYER_SA is required}"
: "${GCP_PROJECT_ID:?repository variable GCP_PROJECT_ID is required}"

# TEMP DEBUG (remove after diagnosing UAT WIF audience error): show the exact provider value
# and its length so a missing //iam.googleapis.com/ prefix or a stray space/newline is visible.
echo "DEBUG GCP_WIF_PROVIDER=[$GCP_WIF_PROVIDER] len=${#GCP_WIF_PROVIDER}"
echo "DEBUG GCP_PROJECT_ID=[$GCP_PROJECT_ID]"

token_file="$(mktemp)"
cred_file="$(mktemp)"
# The token is written to a file (not passed on the CLI) so it never lands in a process list.
printf '%s' "$BITBUCKET_STEP_OIDC_TOKEN" > "$token_file"

# Build a workload-identity credential config that points gcloud at the token file, then
# activate it. STS validates the token against the provider (repo+branch locked in Terraform)
# and returns short-lived credentials for GCP_DEPLOYER_SA — no key material on disk.
gcloud iam workload-identity-pools create-cred-config "$GCP_WIF_PROVIDER" \
  --service-account="$GCP_DEPLOYER_SA" \
  --credential-source-file="$token_file" \
  --output-file="$cred_file"

gcloud auth login --cred-file="$cred_file" --quiet
gcloud config set project "$GCP_PROJECT_ID" --quiet

echo "Authenticated to GCP as $GCP_DEPLOYER_SA (project $GCP_PROJECT_ID) via Bitbucket OIDC."
