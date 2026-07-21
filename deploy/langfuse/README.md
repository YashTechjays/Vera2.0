# Langfuse — LLM Observability (TEST)

Self-hosted Langfuse v3 running in-VPC on `vera-test-langfuse-vm`. Collects OTel traces
from the control-plane and agent-worker (co-located on `vera-test-backend-vm`).
Observability only — no PHI (spans are tokenized, per ADR-0003).

Deploy: `./deploy-langfuse.sh --deploy` (from your laptop, over IAP).
Infra is Terraform (`infra/terraform-test/langfuse.tf`).

## Architecture

```
vera-test-backend-vm ── OTLP POST ──► langfuse-web:3000 ──► MinIO (raw events)
 (control-plane + worker)                   │                     │
                                            └─ queue ─► Redis      ▼
                                                        └─► langfuse-worker
                                                              ├─► ClickHouse (queryable traces)
                                                              └─► Cloud SQL (config/keys)
```

All containers run on `vera-test-langfuse-vm`; Postgres is a dedicated Cloud SQL
instance (`vera-test-langfuse-postgres`), never the PHI-bearing app DB.

## Components

| Component | Runs as | Stores | Storage |
|-----------|---------|--------|---------|
| langfuse-web | container `:3000` | — (receives OTLP, serves UI) | — |
| langfuse-worker | container | — (MinIO → ClickHouse) | — |
| MinIO | container | raw events (source of truth) | data disk `/mnt/data/minio` |
| ClickHouse | container | queryable traces (browsed in UI) | data disk `/mnt/data/clickhouse` |
| Redis | container | queue pointers (transient) | in-memory |
| Postgres | Cloud SQL | users, API keys, dashboards | managed |

## Deploy

```bash
cd Vera2.0/deploy/langfuse
./deploy-langfuse.sh --deploy
```

The script SSHes into the VM over IAP and runs `start-langfuse.sh` as root there; that
script fetches all `vera-test-langfuse-*` secrets from Secret Manager via the VM's own
service account, renders the compose file, and brings the stack up. Idempotent — re-run
to restart/upgrade. Override `PROJECT_ID` / `ZONE` / `LANGFUSE_VM` via env if needed.

## App connection (no agent to install)

Tracing is built into the app images. The backend VM's `render-env.sh` maps three
secrets into `app.env` (see `deploy/secrets.map`), so control-plane + worker export
traces automatically:

```
VERA_LANGFUSE_HOST=http://<langfuse-vm-ip>:3000
VERA_LANGFUSE_PUBLIC_KEY=pk-lf-...
VERA_LANGFUSE_SECRET_KEY=sk-lf-...
```

Unset host = tracing off (no-op). Redeploy the app after the first Langfuse deploy so
`app.env` picks up the new values.

## Blob store (MinIO, not GCS)

The blob store is an **in-VM MinIO container** (native S3), not GCS. GCS-via-HMAC is
blocked by the org policy `iam.disableServiceAccountKeyCreation`, and Langfuse only
reaches a blob store via the S3 API — so MinIO is the store. MinIO root creds = the
`vera-test-langfuse-s3-access-key-id` / `-secret-access-key` secrets. Only Langfuse
reaches MinIO (private docker network, not published to the host/VPC).

## Open the Web UI

VM has no public IP — reach it via IAP tunnel (also printed by the
`langfuse_ui_tunnel_command` Terraform output):

```bash
gcloud compute start-iap-tunnel vera-test-langfuse-vm 3000 \
  --local-host-port=localhost:3000 --zone=us-east1-b --project=<PROJECT_ID>
```

Then open http://localhost:3000. Login `admin@vera.test`; password:
`gcloud secrets versions access latest --secret=vera-test-langfuse-init-user-password`.
Browse **Sessions**.

## Storage & recovery

- ClickHouse + MinIO share one **PD-Balanced 50 GB data disk** (`/mnt/data`, separate
  from the boot disk) — survives a VM re-image. Grows online if it fills; keep <80% full.
- **Daily disk snapshots**, `max_retention_days = 5` (≈ last 5), incremental.
- **MinIO = raw events (source of truth); ClickHouse = the queryable copy** built from them.
- VM crash → data disk re-attaches, no loss. Disk loss → restore from snapshot.

## Notes

- Media/audio upload stays OFF (raw audio = PHI).
- Everything private/in-VPC; UI only via IAP tunnel.
- ClickHouse memory is capped (`max_server_memory_usage_to_ram_ratio ≈ 0.6`) so it can't
  OOM the e2-standard-2.
- The dedicated Cloud SQL has `deletion_protection = true` (mirrors dev). Flip it off in
  `langfuse.tf` before any `terraform destroy`; the instance name is locked ~1 week after
  a delete.
