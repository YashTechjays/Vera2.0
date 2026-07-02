#!/bin/bash
# Deploy script — run from jumpserver via SSH to start/restart the VERA control plane.
# Expects the following env vars to be set by deploy.sh (jumpserver):
#   CONTROL_PLANE_IMAGE — full Artifact Registry image URL
#   VERA_ENV            — deployment environment (dev / staging / production)
#   VERA_GCP_PROJECT    — GCP project ID
set -euo pipefail

: "${CONTROL_PLANE_IMAGE:?CONTROL_PLANE_IMAGE must be set by deploy.sh}"
: "${VERA_ENV:=dev}"
: "${VERA_GCP_PROJECT:?VERA_GCP_PROJECT must be set by deploy.sh}"

export DEBIAN_FRONTEND=noninteractive

if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io
  systemctl enable docker
fi
systemctl start docker || true

gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

if ! command -v nginx &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends nginx
fi

mkdir -p /var/www/vera-frontend

cat > /etc/nginx/sites-available/vera << 'NGINXCONF'
server {
    listen 80 default_server;
    root /var/www/vera-frontend;
    index index.html;

    location /health {
        access_log off;
        return 200 "ok";
        add_header Content-Type text/plain;
    }

    location /healthz {
        access_log off;
        return 200 "ok";
        add_header Content-Type text/plain;
    }

    location /api/ {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
NGINXCONF

ln -sf /etc/nginx/sites-available/vera /etc/nginx/sites-enabled/vera
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl enable nginx && systemctl restart nginx

echo "Fetching secrets from Secret Manager..."
LK_URL=$(gcloud secrets versions access latest --secret=vera-livekit-url)
LK_API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-api-key)
LK_API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-api-secret)
LK_SIP_TRUNK_ID=$(gcloud secrets versions access latest --secret=vera-livekit-sip-trunk-id 2>/dev/null || echo "")
DATABASE_URL=$(gcloud secrets versions access latest --secret=vera-database-url)
REDIS_URL=$(gcloud secrets versions access latest --secret=vera-redis-url)
KMS_KEY=$(gcloud secrets versions access latest --secret=vera-local-kms-master-key)
FRONTEND_BASE_URL=$(gcloud secrets versions access latest --secret=vera-frontend-base-url)

echo "Pulling image: $CONTROL_PLANE_IMAGE"
docker pull "$CONTROL_PLANE_IMAGE"

# Disable systemd service if present — docker --restart handles auto-restart
systemctl stop vera-control-plane 2>/dev/null || true
systemctl disable vera-control-plane 2>/dev/null || true

docker stop vera-control-plane 2>/dev/null || true
docker rm   vera-control-plane 2>/dev/null || true

echo "Starting vera-control-plane..."
docker run -d \
  --name vera-control-plane \
  --restart unless-stopped \
  -p 8000:8000 \
  -e VERA_ENV="$VERA_ENV" \
  -e VERA_GCP_PROJECT="$VERA_GCP_PROJECT" \
  -e VERA_LIVEKIT_URL="$LK_URL" \
  -e LIVEKIT_API_KEY="$LK_API_KEY" \
  -e LIVEKIT_API_SECRET="$LK_API_SECRET" \
  ${LK_SIP_TRUNK_ID:+-e VERA_LIVEKIT_SIP_TRUNK_ID="$LK_SIP_TRUNK_ID"} \
  -e VERA_DATABASE_URL="$DATABASE_URL" \
  -e VERA_REDIS_URL="$REDIS_URL" \
  -e LOCAL_KMS_MASTER_KEY="$KMS_KEY" \
  -e VERA_FRONTEND_BASE_URL="$FRONTEND_BASE_URL" \
  "$CONTROL_PLANE_IMAGE"

echo "Done. vera-control-plane status:"
docker ps --filter name=vera-control-plane
