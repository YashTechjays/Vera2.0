# Vera 2.0 — Deployment Guide

## Repository Structure

```
Vera2.0/
├── vera-backend/          — FastAPI control plane + agent worker
├── vera-frontend/         — React SPA
├── infra/
│   └── terraform/         — GCP infrastructure (Cloud SQL, Memorystore, VMs, secrets)
└── deployment/
    ├── deploy.sh          — Deployment orchestrator (run from your Mac or jumpserver)
    ├── techjays-app/      — control-plane + worker + frontend start scripts
    └── livekit-app/       — livekit-server + livekit-sip start scripts
```

---

## Infrastructure (Terraform)

All infra changes are made from `infra/terraform/`.

```bash
cd infra/terraform

# First time only — initialise with remote state
terraform init -backend-config=backend.hcl

# Preview changes
terraform plan

# Apply
terraform apply
```

**⚠️ Changing Memorystore (`database.tf`) forces recreation** — all Redis data (sessions,
caches) is wiped. Run after a maintenance window, then redeploy all services.

### Current Redis configuration

Redis TLS is **disabled** (`transit_encryption_mode = "DISABLED"`, port `6379`).
The connection URL is `redis://:<auth>@<host>:6379/0` — stored automatically in
Secret Manager as `vera-redis-url` after every `terraform apply`.

---

## Application Deployment

All deployments are driven by `deploy.sh` from your Mac (it SSHes into VMs via IAP).

```bash
cd deployment

./deploy.sh                          # full deploy: build images + deploy all services
./deploy.sh --control-plane          # rebuild + redeploy control plane only
./deploy.sh --control-plane --skip-build  # redeploy without rebuilding image
./deploy.sh --livekit                # restart livekit-server (config refresh)
./deploy.sh --sip                    # restart livekit-sip (config refresh)
./deploy.sh --worker                 # rebuild + redeploy agent worker
./deploy.sh --frontend               # build frontend + push to GCS + nginx reload
./deploy.sh --secrets                # re-fetch secrets on ALL VMs + restart all services
./deploy.sh --migrate                # run DB migrations (alembic upgrade head)
./deploy.sh --seed                   # seed DB (permissions, roles, tenant, admin user)
```

### After `terraform apply`

Always redeploy all three Redis-using services so they pick up the new host/auth:

```bash
./deploy.sh --secrets
```

---

## Self-hosted LiveKit outbound SIP trunk

`livekit-server` + `livekit-sip` run on their own self-host credentials
(`vera-livekit-selfhost-api-key`/`secret`) and their own `vera-livekit-redis`,
independent of control-plane/worker (which are on **LiveKit Cloud**:
`vera-livekit-url = wss://...livekit.cloud`).

The SIP **trunk** is a LiveKit-server state object (stored in `vera-livekit-redis`),
registered once via the `lk` CLI — the bridge container only reads it. Because the
self-hosted server runs on its own Redis, the Cloud trunk `ST_BmdFy84WqTpG`
(in `vera-livekit-sip-trunk-id`) does **not** exist here. Create a new outbound
trunk and store its id in **`vera-livekit-selfhost-sip-trunk-id`** — leave
`vera-livekit-sip-trunk-id` (Cloud) untouched until control-plane/worker are cut
over to self-hosted.

Trunk parameters are stored as **individual key/value secrets** — nothing is
committed. At provision time the JSON is rendered from them on the VM:
- `vera-livekit-selfhost-sip-trunk-address` — Twilio termination SIP URI
- `vera-livekit-selfhost-sip-trunk-number` — caller-ID number (E.164)
- `vera-livekit-selfhost-sip-trunk-auth-username` / `…-auth-password` — Twilio SIP
  **termination** credentials. This trunk uses **credential auth** (Twilio answered
  `407` to IP-only). IP-authorized setups can leave these two empty.

Media note: `livekit-sip` runs with `use_external_ip: true` so it advertises the
SIP VM's public IP (`35.225.113.164`) in SDP — required or audio dies with
`media-timeout`. (Already set in `start.sh`.)

### 1. Store the trunk values in their secrets
Recover `address` + `number` from the existing Cloud trunk if you don't have them:
```bash
export LIVEKIT_URL=wss://vra-cmqzwokb.livekit.cloud
export LIVEKIT_API_KEY=$(gcloud secrets versions access latest --secret=vera-livekit-api-key)
export LIVEKIT_API_SECRET=$(gcloud secrets versions access latest --secret=vera-livekit-api-secret)
lk sip outbound list      # find ST_BmdFy84WqTpG → copy address + number
```
Store each value (`echo -n` avoids a trailing newline):
```bash
echo -n '<trunk>.pstn.twilio.com' | gcloud secrets versions add vera-livekit-selfhost-sip-trunk-address       --data-file=-
echo -n '+1XXXXXXXXXX'            | gcloud secrets versions add vera-livekit-selfhost-sip-trunk-number        --data-file=-
echo -n '<twilio-sip-username>'   | gcloud secrets versions add vera-livekit-selfhost-sip-trunk-auth-username --data-file=-
echo -n '<twilio-sip-password>'   | gcloud secrets versions add vera-livekit-selfhost-sip-trunk-auth-password --data-file=-
```

### 2. Create the trunk (runs on `vera-livekit-vm`)
`provision-trunk.sh` runs **on the LiveKit VM** via the VM's service account — it
fetches the trunk address/number/auth from Secret Manager, renders the JSON, and
registers the trunk against `ws://localhost:7880`. No credentials touch your laptop.
It **deletes any existing trunk of the same name and recreates it**, so re-running
applies changed secrets — meaning a **new `ST_…` each run**.
```bash
./deploy.sh --provision-trunk
```
Store the printed `ST_<id>` (this VM SA is read-only on secrets):
```bash
echo -n 'ST_<new>' | gcloud secrets versions add vera-livekit-selfhost-sip-trunk-id --data-file=-
```

### 3. Test an outbound call
One command — runs on the VM (no local `lk` needed); reads the trunk id from
Secret Manager, creates room `test-outbound`, and dials:
```bash
TEST_NUMBER=+1<test-number> ./deploy.sh --test-outbound
# add CLEANUP=1 to auto-delete the room once the call ends:
CLEANUP=1 TEST_NUMBER=+1<test-number> ./deploy.sh --test-outbound
```
A healthy call ends with `reason: "bye"` and non-zero `audio_rx`/`audio_tx` in the
SIP logs. **You'll only hear silence if you're alone in the room** — there's no
other audio source until a second participant (or the agent) joins.

### 4. Test two-way audio (two calls into one room)
Each call now gets a **unique** `sip-callee-…` identity, so multiple calls coexist
(same identity would evict each other with `DUPLICATE_IDENTITY`). Run twice with
two numbers — both land in `test-outbound` and hear each other:
```bash
TEST_NUMBER=+1AAAAAAAAAA ./deploy.sh --test-outbound
TEST_NUMBER=+1BBBBBBBBBB ./deploy.sh --test-outbound
```
Confirm both are present (run on the VM, creds exported):
```bash
lk room participants list --room test-outbound   # two distinct sip-callee-… identities
```

### Troubleshooting (SIP log `reason:`)
- `auth-failed` (`407`) → Twilio termination creds wrong/missing (step 1 auth secrets).
- `media-timeout` → SDP advertising a private IP; ensure `use_external_ip: true`.
- `duplicate-identity` → two calls sharing an identity (test scripts already make it unique).
- `404` / `488` → number format or trunk address; check `vera-livekit-selfhost-sip-trunk-*`.
Logs: `gcloud compute ssh vera-sip-vm --tunnel-through-iap --zone=us-central1-a --command='sudo docker logs --tail 40 livekit-sip'`

---

## Database Migrations

Migrations run as the `postgres` superuser to bypass Row-Level Security (`FORCE RLS`)
on platform-level seed data.

```bash
./deploy.sh --migrate
```

### One-time setup — grant BYPASSRLS to postgres

Cloud SQL's `postgres` user does not have `BYPASSRLS` by default. Grant it once via
**Cloud SQL Studio** (Cloud Console → SQL → vera-postgres → Cloud SQL Studio):

```sql
ALTER ROLE postgres BYPASSRLS;
```

This only needs to be done once per Cloud SQL instance. If the instance is recreated
by Terraform, repeat this step before running migrations.

---

## Common Operations

### Check control-plane logs
```bash
gcloud compute ssh vera-control-plane-vm --tunnel-through-iap \
  --zone=us-central1-a --project=innate-watch-497101-k4 -- \
  "sudo docker logs vera-control-plane --tail=50"
```

### Verify Redis URL in running container
```bash
gcloud compute ssh vera-control-plane-vm --tunnel-through-iap \
  --zone=us-central1-a --project=innate-watch-497101-k4 -- \
  "sudo docker inspect vera-control-plane | grep VERA_REDIS_URL"
```

### Check current secret values
```bash
gcloud secrets versions access latest --secret=vera-redis-url --project=innate-watch-497101-k4
gcloud secrets versions access latest --secret=vera-database-url --project=innate-watch-497101-k4
```

---

## Application Guides

| Application | Guide |
|---|---|
| `vera-frontend` | [frontend.md](./frontend.md) |
| `control_plane` | [control-plane.md](./control-plane.md) |
| `agent_worker` | [agent-worker.md](./agent-worker.md) |

---

## Known Issues & Gotchas

### Systemd service conflict
The control-plane VM has a `vera-control-plane.service` that was set up manually.
`start-control-plane.sh` disables it on every deploy so docker's `--restart unless-stopped`
takes over. If the container keeps reverting to old config, check:
```bash
gcloud compute ssh vera-control-plane-vm --tunnel-through-iap \
  --zone=us-central1-a --project=innate-watch-497101-k4 -- \
  "sudo systemctl status vera-control-plane.service"
```

### deploy.sh script variable expansion
`deploy.sh` uses process substitution (`< <(...)`) to send start scripts to VMs without
local variable expansion. Do not change `ssh_run` back to a heredoc (`<<EOF`) — it causes
script-internal variables like `$REDIS_URL` to expand on the Mac (to empty or stale values)
instead of on the remote VM.

### Migrations blocked by RLS
If `--migrate` fails with `InsufficientPrivilegeError` on `platform_login_provider`, the
`postgres` role is missing `BYPASSRLS`. Fix via Cloud SQL Studio (see above).
