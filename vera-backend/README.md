# vera-backend

Vera 2.0 — HIPAA-compliant, multi-tenant voice-AI platform that places outbound calls
to health-insurance payers for eligibility verification and prior-auth checks.

## Architecture

Three independently deployable processes sharing one core package:

| Process | Path | Role |
| --- | --- | --- |
| control_plane | `apps/control_plane` | FastAPI. Owns Postgres + business logic. Never touches audio. |
| agent_worker | `apps/agent_worker` | LiveKit Agents worker. Registers with an `agent_name`, dispatched to rooms. Authenticates as a GCP service principal. |
| frontend | separate repo | Out of scope here. |

Shared code lives in `packages/vera_core` (config, db, models, PHI boundary, schemas,
audit, observability) and `packages/phi_codec` (vendored PHI de-/re-identification codec).

## Getting started

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and [`just`](https://github.com/casey/just)
(`brew install just` / `cargo install just` / `apt install just`).

```bash
uv sync --all-packages   # or: just bootstrap
just up                  # core infra: Postgres (pgvector) + Redis + sendria + livekit
just migrate && just seed
just api                 # control plane on :8000
just check               # ruff + mypy + pytest — the CI gate
```

Python is pinned to 3.12 (`.python-version`); the phi-codec dependency caps at `<3.13`.

### Observability (Langfuse) — opt-in

`just up` starts only the core dev infra. The self-hosted **Langfuse v3** tracing stack
(ClickHouse + MinIO + its own Postgres/Redis + web + worker) is heavy, so it's gated behind
the `langfuse` docker-compose profile and never starts with `just up` (or a plain
`docker compose up`). Bring it up only when you need tracing:

```bash
just langfuse-up         # docker compose --profile langfuse up -d; web UI on :4000
just langfuse-down       # stop it and free the RAM
```

Leaving it running balloons the Docker VM until the kernel OOM-kills ClickHouse. The worker
degrades gracefully (drops spans) when it's down.

### Seeding a dev login

`just seed` (run after `just migrate`) is idempotent. It provisions the global
permission catalog and system roles (SUPER_ADMIN / TENANT_ADMIN / SUPERVISOR), a sample
tenant (*Vera Health (Example)*), the local password provider (MFA off), and an admin user
wired to TENANT_ADMIN. By default the admin is `admin@veratechsolutions.example` /
`dev-password-change-me`.

To test with **your own** credentials, seed a personal admin into the same sample tenant:

```bash
just seed-user you@example.com your-password
# or, equivalently:
SEED_ADMIN_EMAIL=you@example.com SEED_ADMIN_PASSWORD=your-password just seed
```

The seed is keyed on email, so this **adds** your user rather than replacing the sample
one, and re-running is safe. On success the script prints the exact login call —
`POST /api/v1/tenants/{tenant_id}/auth/login` with your `{email, password}`.

> Local-dev credentials only. Seeding runs as the docker-compose superuser (bypasses RLS);
> request-path code never does this.

## Repo layout

```
packages/vera_core/    shared core (imported by both apps)
packages/phi_codec/    vendored PHI codec (upstream: phi-codec-vault repo)
apps/control_plane/    FastAPI app
apps/agent_worker/     LiveKit worker
migrations/            Alembic; RLS policies live IN migrations
infra/                 Terraform skeleton
docker/                one Dockerfile per app
adr/                   Architecture Decision Records
```
