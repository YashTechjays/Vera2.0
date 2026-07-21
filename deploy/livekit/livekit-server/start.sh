#!/bin/bash
# Runs ON vera-test-livekit-vm (as root, via deploy-livekit.sh over IAP).
# Starts the self-hosted LiveKit server with a public wss:// endpoint:
#   * Caddy terminates TLS on :443 and reverse-proxies WSS signaling to livekit :7880,
#     auto-provisioning a Let's Encrypt cert for the livekit-domain VM metadata.
#   * use_external_ip: true — the server advertises the VM's external IP for WebRTC ICE
#     so browsers can connect. In-VPC participants (agent-worker, SIP, Egress) still
#     reach it on the private IP at ws://…:7880.
# Secrets are fetched at runtime from Secret Manager via the VM's service account.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
SECRET_PREFIX="${SECRET_PREFIX:-vera-test}"

# ── Docker (idempotent; the startup-script normally already installed it) ──────
if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io gettext-base curl
  systemctl enable docker
fi
systemctl start docker || true

# ── Fetch secrets + non-secret metadata ───────────────────────────────────────
echo "Fetching secrets from Secret Manager (${SECRET_PREFIX}-*)..."
API_KEY=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-key")
API_SECRET=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-api-secret")
export REDIS_AUTH
REDIS_AUTH=$(gcloud secrets versions access latest --secret="${SECRET_PREFIX}-livekit-redis-auth-string")
export REDIS_HOST
REDIS_HOST=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/redis-host" -H "Metadata-Flavor: Google")
# Domain Caddy serves wss:// on — set as VM instance metadata by Terraform (var.livekit_domain).
DOMAIN=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/livekit-domain" -H "Metadata-Flavor: Google")
: "${DOMAIN:?livekit-domain metadata not set on the VM (set livekit_domain in terraform-test and apply)}"

# ── Write resolved config ─────────────────────────────────────────────────────
mkdir -p /etc/livekit
chmod 700 /etc/livekit

envsubst '${REDIS_HOST} ${REDIS_AUTH}' << 'YAML_TEMPLATE' > /etc/livekit/livekit.yaml
port: 7880

rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  # Advertise the VM's external IP for WebRTC ICE so browsers can connect.
  use_external_ip: true

redis:
  address: ${REDIS_HOST}:6379
  password: ${REDIS_AUTH}

logging:
  level: info
YAML_TEMPLATE

chmod 600 /etc/livekit/livekit.yaml

# ── Write Caddy config ────────────────────────────────────────────────────────
# Caddy terminates TLS on :443 and reverse-proxies WSS signaling to livekit :7880.
# It auto-provisions and renews a Let's Encrypt cert for $DOMAIN (needs :80 open).
# LiveKit OSS does not terminate TLS itself — this proxy is what makes wss:// work.
cat > /etc/livekit/Caddyfile <<CADDYFILE
${DOMAIN} {
    reverse_proxy localhost:7880
}
CADDYFILE
chmod 600 /etc/livekit/Caddyfile

# ── (Re)start the server + Caddy ──────────────────────────────────────────────
docker stop livekit-server caddy 2>/dev/null || true
docker rm   livekit-server caddy 2>/dev/null || true

echo "Starting livekit-server (wss://${DOMAIN} via Caddy; internal ws://<private-ip>:7880)..."
docker run -d \
  --name livekit-server \
  --restart unless-stopped \
  --network host \
  -e "LIVEKIT_KEYS=${API_KEY}: ${API_SECRET}" \
  -v "/etc/livekit/livekit.yaml:/etc/livekit.yaml" \
  livekit/livekit-server:latest \
  --config /etc/livekit.yaml

# caddy_data volume persists the issued cert across redeploys → avoids the
# Let's Encrypt rate limit (~5 certs/week/domain) on repeat deploys.
echo "Starting caddy (TLS termination for wss://${DOMAIN})..."
docker run -d \
  --name caddy \
  --restart unless-stopped \
  --network host \
  -v "/etc/livekit/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "caddy_data:/data" \
  -v "caddy_config:/config" \
  caddy:2 caddy run --config /etc/caddy/Caddyfile

echo "Done. Container status:"
docker ps --filter name=livekit-server --filter name=caddy
