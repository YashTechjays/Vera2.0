#!/bin/bash
# Runs ON vera-test-langfuse-vm (as root, via deploy-langfuse.sh over IAP) to
# start/restart the self-hosted Langfuse v3 stack.
#
# Everything is fetched from Secret Manager at runtime via the VM's own SA — no env in.
# The stack: langfuse-redis + langfuse-clickhouse + langfuse-minio + langfuse-web +
# langfuse-worker, all on a private docker network. Only web (:3000) is host-published.
# ClickHouse + MinIO persist on the dedicated data disk mounted at /mnt/data.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
SECRET_PREFIX="${SECRET_PREFIX:-vera-test}"

# ── Docker + Compose v2 (idempotent) ──────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  apt-get update -y
  apt-get install -y --no-install-recommends docker.io docker-compose-v2 curl
  systemctl enable docker
fi
systemctl start docker || true
# Ensure the compose v2 plugin is present even if docker was pre-installed.
docker compose version &>/dev/null || apt-get install -y --no-install-recommends docker-compose-v2

# ── Mount the dedicated data disk (idempotent; format only on first boot) ──────
DATA_DEV=/dev/disk/by-id/google-clickhouse-data
if ! blkid "$DATA_DEV" &>/dev/null; then
  echo "Formatting data disk $DATA_DEV (first boot)..."
  mkfs.ext4 -F "$DATA_DEV"
fi
mkdir -p /mnt/data
grep -q " /mnt/data " /etc/fstab || echo "$DATA_DEV /mnt/data ext4 defaults,nofail 0 2" >> /etc/fstab
mountpoint -q /mnt/data || mount /mnt/data
mkdir -p /mnt/data/clickhouse /mnt/data/clickhouse-logs /mnt/data/minio
# ClickHouse and MinIO containers run as uid 101 / 1000 respectively; make dirs writable.
chown -R 101:101 /mnt/data/clickhouse /mnt/data/clickhouse-logs

# ── Fetch secrets (VM SA has secretAccessor on all <prefix>-langfuse-* secrets) ─
echo "Fetching secrets from Secret Manager (${SECRET_PREFIX}-langfuse-*)..."
sec() { gcloud secrets versions access latest --secret="${SECRET_PREFIX}-langfuse-$1"; }
export LANGFUSE_DATABASE_URL="$(sec database-url)"
export LANGFUSE_CLICKHOUSE_PASSWORD="$(sec clickhouse-password)"
export LANGFUSE_REDIS_PASSWORD="$(sec redis-password)"
export LANGFUSE_NEXTAUTH_SECRET="$(sec nextauth-secret)"
export LANGFUSE_ENCRYPTION_KEY="$(sec encryption-key)"
export LANGFUSE_SALT="$(sec salt)"
export LANGFUSE_S3_ACCESS_KEY_ID="$(sec s3-access-key-id)"
export LANGFUSE_S3_SECRET_ACCESS_KEY="$(sec s3-secret-access-key)"
export LANGFUSE_PUBLIC_KEY="$(sec public-key)"
export LANGFUSE_SECRET_KEY="$(sec secret-key)"
export LANGFUSE_INIT_USER_PASSWORD="$(sec init-user-password)"

WORKDIR=/opt/vera-langfuse
mkdir -p "$WORKDIR/clickhouse-config.d"

# ClickHouse memory cap — keep it from OOM-killing the e2-standard-2 (8 GB) box.
cat > "$WORKDIR/clickhouse-config.d/memory.xml" <<'XML'
<clickhouse>
    <max_server_memory_usage_to_ram_ratio>0.6</max_server_memory_usage_to_ram_ratio>
</clickhouse>
XML

# ── Render the compose file ───────────────────────────────────────────────────
# Quoted heredoc: ${VARS} stay literal in the file; `docker compose` interpolates
# them from the exported env at `up` time (all secret values are special-char-free).
# Image tags match the locally-validated docker-compose.yml. For prod, pin digests.
cat > "$WORKDIR/docker-compose.langfuse.yml" <<'YAML'
services:
  langfuse-redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --requirepass "${LANGFUSE_REDIS_PASSWORD}"
    volumes:
      - langfuse_redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${LANGFUSE_REDIS_PASSWORD}", "ping"]
      interval: 5s
      timeout: 5s
      retries: 20

  langfuse-clickhouse:
    image: clickhouse/clickhouse-server:latest
    restart: unless-stopped
    user: "101:101"
    environment:
      CLICKHOUSE_DB: default
      CLICKHOUSE_USER: clickhouse
      CLICKHOUSE_PASSWORD: "${LANGFUSE_CLICKHOUSE_PASSWORD}"
    volumes:
      - /mnt/data/clickhouse:/var/lib/clickhouse
      - /mnt/data/clickhouse-logs:/var/log/clickhouse-server
      # Mount the single memory-cap file, NOT the whole config.d dir — a directory
      # mount shadows the image's docker_related_config.xml (which sets listen_host
      # to 0.0.0.0), leaving ClickHouse listening on localhost only → other containers
      # get "connection refused" on :9000.
      - /opt/vera-langfuse/clickhouse-config.d/memory.xml:/etc/clickhouse-server/config.d/zz-vera-memory.xml:ro
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8123/ping"]
      interval: 5s
      timeout: 5s
      retries: 30

  langfuse-minio:
    image: minio/minio:latest
    restart: unless-stopped
    entrypoint: sh
    command: -c 'mkdir -p /data/langfuse && minio server --address ":9000" --console-address ":9001" /data'
    environment:
      MINIO_ROOT_USER: "${LANGFUSE_S3_ACCESS_KEY_ID}"
      MINIO_ROOT_PASSWORD: "${LANGFUSE_S3_SECRET_ACCESS_KEY}"
    volumes:
      - /mnt/data/minio:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 20

  langfuse-web:
    image: langfuse/langfuse:3
    restart: unless-stopped
    depends_on:
      langfuse-clickhouse: { condition: service_healthy }
      langfuse-redis: { condition: service_healthy }
      langfuse-minio: { condition: service_healthy }
    ports:
      - "3000:3000"
    environment: &langfuse-env
      DATABASE_URL: "${LANGFUSE_DATABASE_URL}"
      CLICKHOUSE_MIGRATION_URL: "clickhouse://langfuse-clickhouse:9000"
      CLICKHOUSE_URL: "http://langfuse-clickhouse:8123"
      CLICKHOUSE_USER: clickhouse
      CLICKHOUSE_PASSWORD: "${LANGFUSE_CLICKHOUSE_PASSWORD}"
      CLICKHOUSE_CLUSTER_ENABLED: "false"
      REDIS_HOST: langfuse-redis
      REDIS_PORT: "6379"
      REDIS_AUTH: "${LANGFUSE_REDIS_PASSWORD}"
      LANGFUSE_BULLMQ_SKIP_REDIS_VERSION_CHECK: "true"
      # Blob store = in-VM MinIO (S3). Event upload is REQUIRED in v3. Media upload
      # is intentionally left UNSET/OFF — raw audio is PHI and must never land here.
      LANGFUSE_S3_EVENT_UPLOAD_ENABLED: "true"
      LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: "http://langfuse-minio:9000"
      LANGFUSE_S3_EVENT_UPLOAD_REGION: auto
      LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: "${LANGFUSE_S3_ACCESS_KEY_ID}"
      LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: "${LANGFUSE_S3_SECRET_ACCESS_KEY}"
      LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
      TELEMETRY_ENABLED: "false"
      # Deterministic seed — the same keypair the app authenticates with.
      LANGFUSE_INIT_ORG_ID: "org-vera-test"
      LANGFUSE_INIT_ORG_NAME: "Vera Test"
      LANGFUSE_INIT_PROJECT_ID: "proj-vera-test"
      LANGFUSE_INIT_PROJECT_NAME: "vera-test"
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: "${LANGFUSE_PUBLIC_KEY}"
      LANGFUSE_INIT_PROJECT_SECRET_KEY: "${LANGFUSE_SECRET_KEY}"
      LANGFUSE_INIT_USER_EMAIL: "admin@vera.test"
      LANGFUSE_INIT_USER_NAME: "Admin"
      LANGFUSE_INIT_USER_PASSWORD: "${LANGFUSE_INIT_USER_PASSWORD}"
      # Browser reaches the UI over the IAP tunnel at localhost:3000.
      NEXTAUTH_URL: "http://localhost:3000"
      NEXTAUTH_SECRET: "${LANGFUSE_NEXTAUTH_SECRET}"
      ENCRYPTION_KEY: "${LANGFUSE_ENCRYPTION_KEY}"
      SALT: "${LANGFUSE_SALT}"
    healthcheck:
      test: ["CMD-SHELL", "wget --spider -q http://localhost:3000/api/public/health || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 30s

  langfuse-worker:
    image: langfuse/langfuse-worker:3
    restart: unless-stopped
    depends_on:
      langfuse-clickhouse: { condition: service_healthy }
      langfuse-redis: { condition: service_healthy }
      langfuse-minio: { condition: service_healthy }
    environment:
      <<: *langfuse-env

volumes:
  langfuse_redis_data:
YAML

# ── Bring the stack up ────────────────────────────────────────────────────────
cd "$WORKDIR"
docker compose -f docker-compose.langfuse.yml up -d

echo "Waiting for langfuse-web health..."
for _ in $(seq 1 60); do
  if curl -sf http://localhost:3000/api/public/health >/dev/null 2>&1; then
    echo "Langfuse is healthy."
    break
  fi
  sleep 5
done

echo "Done. Stack status:"
docker compose -f docker-compose.langfuse.yml ps
