#!/bin/bash
# Runs ON vera-test-egress-vm (as root, via deploy-livekit.sh over IAP).
# Starts LiveKit Egress — records rooms as mixed audio and uploads to GCS via ADC
# (the attached egress VM service account; no HMAC key / SA key file). It has no
# public API: it registers with the server over the shared LiveKit Redis and runs
# jobs the control-plane starts via the server Egress API.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
SECRET_PREFIX="${SECRET_PREFIX:-vera-test}"

# Pin the image for a PHI-adjacent service (check newer tags at
# https://github.com/livekit/egress/releases before bumping).
EGRESS_IMAGE="livekit/egress:v1.13.0"

if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io gettext-base curl
  systemctl enable docker
fi
systemctl start docker || true

echo "Fetching secrets from Secret Manager (${SECRET_PREFIX}-*)..."
export LIVEKIT_API_KEY LIVEKIT_API_SECRET REDIS_AUTH REDIS_HOST RECORDINGS_BUCKET LIVEKIT_WS_URL
LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-key")
LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-secret")
REDIS_AUTH=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-redis-auth-string")

# Non-secret config from instance metadata (set by Terraform).
REDIS_HOST=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host" -H "Metadata-Flavor: Google")
LIVEKIT_INTERNAL_IP=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/livekit-internal-ip" -H "Metadata-Flavor: Google")
RECORDINGS_BUCKET=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/recordings-bucket" -H "Metadata-Flavor: Google")

# Egress talks to the SFU server-to-server over the LiveKit VM's private IP (in-VPC, no TLS).
LIVEKIT_WS_URL="ws://${LIVEKIT_INTERNAL_IP}:7880"

# Pass the config via EGRESS_CONFIG_BODY (env) rather than a mounted file: the egress
# image runs as a NON-root user (it sandboxes headless Chrome) and cannot read a
# root-owned 0600 config file, so a file mount crash-loops with "permission denied".
# The env-body approach also keeps the Redis password off the VM disk.
export EGRESS_CONFIG_BODY
EGRESS_CONFIG_BODY="$(envsubst '${REDIS_HOST} ${REDIS_AUTH} ${RECORDINGS_BUCKET}' << 'YAML_TEMPLATE'
redis:
  address: ${REDIS_HOST}:6379
  password: ${REDIS_AUTH}

storage:
  gcp:
    bucket: ${RECORDINGS_BUCKET}

logging:
  level: info
YAML_TEMPLATE
)"

docker stop livekit-egress 2>/dev/null || true
docker rm   livekit-egress 2>/dev/null || true

# --shm-size=1g: headless Chrome (bundled in the image) needs more than the 64 MB
# default /dev/shm or it crashes on Room Composite renders.
# API key/secret, ws_url, and the config body are all passed as env — nothing on disk.
echo "Starting livekit-egress..."
docker run -d \
  --name livekit-egress \
  --restart unless-stopped \
  --network host \
  --shm-size=1g \
  -e EGRESS_CONFIG_BODY \
  -e LIVEKIT_API_KEY="${LIVEKIT_API_KEY}" \
  -e LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET}" \
  -e LIVEKIT_WS_URL="${LIVEKIT_WS_URL}" \
  "${EGRESS_IMAGE}"

echo "Done. Container status:"
docker ps --filter name=livekit-egress
