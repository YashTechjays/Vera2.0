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

# TEMP DEBUG (remove after diagnosing UAT WIF audience error): decode ONLY the aud + iss claims
# from the OIDC token to compare against the provider's allowedAudiences/issuerUri. Never prints
# the token, the sub, or the signature.
python3 - <<'PY'
import os, base64, json
seg = os.environ["BITBUCKET_STEP_OIDC_TOKEN"].split('.')[1]
seg += '=' * (-len(seg) % 4)
claims = json.loads(base64.urlsafe_b64decode(seg))
print("DEBUG token aud =", claims.get("aud"))
print("DEBUG token iss =", claims.get("iss"))
PY

# TEMP DEBUG (remove): do the raw STS token-exchange with curl to surface the FULL error_description
# (gcloud truncates it). Prints only the STS *response* body — the token is in the request, never logged.
echo "DEBUG raw STS response:"
curl -s -X POST "https://sts.googleapis.com/v1/token" \
  --data-urlencode "audience=$GCP_WIF_PROVIDER" \
  --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  --data-urlencode "requested_token_type=urn:ietf:params:oauth:token-type:access_token" \
  --data-urlencode "scope=https://www.googleapis.com/auth/cloud-platform" \
  --data-urlencode "subject_token_type=urn:ietf:params:oauth:token-type:jwt" \
  --data-urlencode "subject_token=$BITBUCKET_STEP_OIDC_TOKEN" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); d.pop("access_token",None); print(json.dumps(d))'

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

# TEMP DEBUG (remove): the cred config holds no secret (token is in a separate file) — print it to
# compare the audience/subject_token_type gcloud sends against the raw STS call that just succeeded.
echo "DEBUG cred config:"; cat "$cred_file"

gcloud auth login --cred-file="$cred_file" --quiet
gcloud config set project "$GCP_PROJECT_ID" --quiet

echo "Authenticated to GCP as $GCP_DEPLOYER_SA (project $GCP_PROJECT_ID) via Bitbucket OIDC."
