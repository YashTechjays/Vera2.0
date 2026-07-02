#!/bin/bash
# Deploy script — run from jumpserver via SSH to start/restart livekit-sip.
# Expects LIVEKIT_SERVER_IP to be set by the caller (deploy.sh on jumpserver).
# Fetches all other secrets at runtime from GCP Secret Manager.
set -euo pipefail

: "${LIVEKIT_SERVER_IP:?LIVEKIT_SERVER_IP must be set by the caller (deploy.sh)}"

export DEBIAN_FRONTEND=noninteractive

if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io gettext-base curl
  systemctl enable docker
fi
systemctl start docker || true

echo "Fetching secrets from GCP Secret Manager..."
export LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-key)
export LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-secret)
export REDIS_AUTH=$(gcloud secrets versions access latest --secret=vera-livekit-redis-auth-string)
export REDIS_HOST=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host" -H "Metadata-Flavor: Google")

mkdir -p /etc/livekit
chmod 700 /etc/livekit

envsubst '${LIVEKIT_SERVER_IP} ${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET} ${REDIS_HOST} ${REDIS_AUTH}' << 'YAML_TEMPLATE' > /etc/livekit/sip.yaml
sip_port: 5060
rtp_port: 10000
rtp_port_end: 20000

api_key: ${LIVEKIT_API_KEY}
api_secret: ${LIVEKIT_API_SECRET}
ws_url: ws://${LIVEKIT_SERVER_IP}:7880
use_external_ip: true

redis:
  address: ${REDIS_HOST}:6379
  password: ${REDIS_AUTH}

logging:
  level: info
YAML_TEMPLATE

chmod 600 /etc/livekit/sip.yaml

docker stop livekit-sip 2>/dev/null || true
docker rm   livekit-sip 2>/dev/null || true

echo "Starting livekit-sip..."
docker run -d \
  --name livekit-sip \
  --restart unless-stopped \
  --network host \
  -v "/etc/livekit/sip.yaml:/sip/config.yaml" \
  livekit/sip:latest

echo "Done. livekit-sip status:"
docker ps --filter name=livekit-sip
