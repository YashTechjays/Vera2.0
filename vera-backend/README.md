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

### Eval harness (call-flow simulation) — opt-in

`apps/agent_worker/tests/evals/` replays a whole call without placing one — real entrypoint, real
compiled CallPlan, a second Gemini as the payer rep — then an evaluator LLM grades the transcript and
prints a scorecard. Live Gemini calls, so the `evals` marker keeps them out of `just check`.

Needs Vertex ADC (`gcloud auth application-default login`) and a seeded Postgres
(`just up && just migrate && just seed`) — the plan comes from the published `schema_version` row.

```bash
# full: 3 scenarios over all 9 tasks / 182 fields (~12 min). Drop VERA_EVALS_FULL for a ~3-min
# focused loop; add -k inactive for one scenario; VERA_EVALS_JUDGE_STRICT=1 to gate on the verdict.
VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
```

- **`-m evals` is required.** Without it you get the 20 LLM-free tests and **no simulations** — which
  looks like a clean pass. A real run prints a `===== <scenario>: … =====` banner per scenario.
- `-s` or the transcript and scorecard are swallowed; `-rs` or skip reasons are hidden (an unseeded DB
  otherwise looks like "nothing to run").
- Check `N answers extracted` per scenario: **0 means the Observer contributed nothing**, so no rule
  could fire, yet most dimensions still read `pass`.

No STT, no real DTMF (`press_keypad` is mocked), and extraction settles between turns so rules fire
more reliably than on a real call. It shortens the loop; it does not replace a live call — for that
without telephony, see browser callee below. Defects found so far:
`docs/superpowers/plans/2026-07-30-call-flow-eval-findings-remediation.md`.

### Browser callee (a live call without telephony) — opt-in

A real call in every respect except the transport: the queue dispatcher places no SIP call, and you
join the LiveKit room from Live Monitoring **as the payer rep**. Real STT, real LLM, real TTS, real
Observer — so unlike the eval harness it *does* count as a live call for voice-path changes.

Off by default and refused when off (`?callee=true` → 409), so it can never reach production. Both
flags must be set — the backend is the authority; the frontend one only decides whether the button
renders.

```bash
just up && just migrate && just seed
VERA_BROWSER_CALLEE_TRANSPORT=true just api       # terminal 1
VERA_BROWSER_CALLEE_TRANSPORT=true just worker    # terminal 2
cd ../vera-frontend && VITE_BROWSER_CALLEE_TRANSPORT=true npm run dev
```

Send a patient form to the queue, open it in Live Monitoring (it appears as **Initiated**), click
**Join as payer rep**, allow the mic — the agent greets you and you answer as the payer would.
Closing the tab hangs up, exactly like a phone hangup.

- **You have ~60s** from enqueue to joining (`_SPEAKER_TIMEOUT_S`). Miss it and the worker gives up
  with `NO_ANSWER`; with `auto_retry_enabled` the form re-dispatches and burns retry budget.
- **One tab per call.** The identity is `caller-{user_id}` with no session suffix, so the same user
  in a second tab evicts the first and tears the room down.
- The form still needs a valid E.164 payer number — only the SIP trunk requirement is waived.
- Traces carry `vera.transport` (`sip` | `browser`), so filter it out of call-quality analysis.

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
