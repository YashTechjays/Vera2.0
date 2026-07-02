#!/bin/bash
# Deploy script — run from jumpserver via SSH to start/restart livekit-server.
# Fetches secrets at runtime from GCP Secret Manager.
# Config template is embedded below — REDIS_HOST and REDIS_AUTH are substituted at runtime.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# ── Docker (idempotent) ───────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io gettext-base curl
  systemctl enable docker
fi
systemctl start docker || true

# ── Fetch secrets ─────────────────────────────────────────────────────────────
echo "Fetching secrets from GCP Secret Manager..."
API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-key)
API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-selfhost-api-secret)
export REDIS_AUTH=$(gcloud secrets versions access latest --secret=vera-livekit-redis-auth-string)
# REDIS_HOST is set as non-secret VM instance metadata by Terraform
export REDIS_HOST=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host" -H "Metadata-Flavor: Google")

# ── Write resolved config ─────────────────────────────────────────────────────
mkdir -p /etc/livekit
chmod 700 /etc/livekit

envsubst '${REDIS_HOST} ${REDIS_AUTH}' << 'YAML_TEMPLATE' > /etc/livekit/livekit.yaml
port: 7880

rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true

redis:
  address: ${REDIS_HOST}:6379
  password: ${REDIS_AUTH}

logging:
  level: info
YAML_TEMPLATE

chmod 600 /etc/livekit/livekit.yaml

# ── Stop existing container ───────────────────────────────────────────────────
docker stop livekit-server 2>/dev/null || true
docker rm   livekit-server 2>/dev/null || true

# ── Start container ───────────────────────────────────────────────────────────
echo "Starting livekit-server..."
docker run -d \
  --name livekit-server \
  --restart unless-stopped \
  --network host \
  -e "LIVEKIT_KEYS=${API_KEY}: ${API_SECRET}" \
  -v "/etc/livekit/livekit.yaml:/etc/livekit.yaml" \
  livekit/livekit-server:latest \
  --config /etc/livekit.yaml

echo "Done. livekit-server status:"
docker ps --filter name=livekit-server
