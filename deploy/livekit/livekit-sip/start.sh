#!/bin/bash
# Runs ON vera-test-sip-vm (as root, via deploy-livekit.sh over IAP).
# Starts the LiveKit SIP bridge (Twilio ↔ LiveKit, outbound). It reaches the SFU
# over the LiveKit VM's private IP (ws://…:7880, in-VPC) and coordinates over the
# dedicated LiveKit Redis. Twilio termination uses the trunk registered by
# provision-trunk.sh. Secrets are fetched at runtime from Secret Manager.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
SECRET_PREFIX="${SECRET_PREFIX:-vera-test}"

if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io gettext-base curl
  systemctl enable docker
fi
systemctl start docker || true

echo "Fetching secrets from Secret Manager (${SECRET_PREFIX}-*)..."
export LIVEKIT_API_KEY LIVEKIT_API_SECRET REDIS_AUTH REDIS_HOST LIVEKIT_SERVER_IP
LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-key")
LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-secret")
REDIS_AUTH=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-redis-auth-string")
REDIS_HOST=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host" -H "Metadata-Flavor: Google")
LIVEKIT_SERVER_IP=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/livekit-server-ip" -H "Metadata-Flavor: Google")

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
  --log-driver gcplogs \
  --network host \
  -v "/etc/livekit/sip.yaml:/sip/config.yaml" \
  livekit/sip:latest

echo "Done. livekit-sip status:"
docker ps --filter name=livekit-sip
