# control_plane

FastAPI HTTP API — `vera-backend/apps/control_plane`

## Requirements

- Docker (for containerised run)
- Or: Python 3.12, uv (for local run)
- PostgreSQL 17 with pgvector extension
- Redis 7

## Build Docker Image

Build context must be the `vera-backend/` directory, not the app subdirectory:

```bash
cd vera-backend
docker build -f docker/control_plane.Dockerfile -t control-plane:latest .
```

## Step 1 — Create Database Tables

Run this once before starting the container for the first time. It creates all the tables. On subsequent deploys it applies only schema changes since the last run.

```bash
docker run --rm \
  -e VERA_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/vera \
  -e LOCAL_KMS_MASTER_KEY=<base64-32-byte-key> \
  control-plane:latest \
  alembic upgrade head
```

Wait for this to complete before proceeding.

## Step 2 — Run

```bash
docker run -p 8000:8000 \
  -e VERA_ENV=dev \
  -e VERA_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/vera \
  -e VERA_REDIS_URL=redis://:<auth_string>@<host>:6379/0 \
  -e LOCAL_KMS_MASTER_KEY=<base64-32-byte-key> \
  -e VERA_LIVEKIT_URL=ws://host:7880 \
  -e LIVEKIT_API_KEY=your-livekit-key \
  -e LIVEKIT_API_SECRET=your-livekit-secret \
  control-plane:latest
```

The API is available at `http://localhost:8000`.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `VERA_ENV` | Yes | `local` | Environment name: `local`, `dev`, `staging`, `prod` |
| `VERA_DATABASE_URL` | Yes | — | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `VERA_REDIS_URL` | Yes | — | Redis connection string (`redis://...`) |
| `LOCAL_KMS_MASTER_KEY` | Yes (non-prod) | — | Base64-encoded 32-byte key for MFA TOTP encryption. Use this in dev/local. In prod, use `VERA_KMS_KEY_NAME` instead. |
| `VERA_KMS_KEY_NAME` | Yes (prod only) | — | Full GCP Cloud KMS key resource path. Replaces `LOCAL_KMS_MASTER_KEY` in production. |
| `VERA_LIVEKIT_URL` | No | — | LiveKit server URL (`ws://` locally, `wss://` in prod). If unset, call endpoints are disabled. |
| `LIVEKIT_API_KEY` | No | — | LiveKit API key. Required if `VERA_LIVEKIT_URL` is set. |
| `LIVEKIT_API_SECRET` | No | — | LiveKit API secret. Required if `VERA_LIVEKIT_URL` is set. |
| `VERA_LANGFUSE_HOST` | No | — | Self-hosted Langfuse URL for tracing. Tracing is disabled if unset. |
| `VERA_LANGFUSE_PUBLIC_KEY` | No | — | Langfuse project public key. |
| `VERA_LANGFUSE_SECRET_KEY` | No | — | Langfuse project secret key. |
| `VERA_SMTP_HOST` | No | `localhost` | SMTP server host for sending invite emails. |
| `VERA_SMTP_PORT` | No | `1025` | SMTP server port. |
| `VERA_EMAIL_FROM` | No | `no-reply@vera.local` | From address for outbound emails. |
| `VERA_FRONTEND_BASE_URL` | No | `http://localhost:5173` | Public frontend origin used to build invite-link URLs in emails. Must be the React SPA host, not the API. Set via `vera-frontend-base-url` secret in production. |
| `VERA_GCP_PROJECT` | No | — | GCP project ID. Required in production for KMS. |
| `VERA_LOG_LEVEL` | No | `INFO` | Log level. |

## Health Check

```
GET /healthz → {"status": "ok"}
```

## Ports

| Port | Protocol | Description |
|---|---|---|
| `8000` | HTTP | API server |
