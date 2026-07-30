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

`apps/agent_worker/tests/evals/` replays a **whole call** without placing one: the real entrypoint
(`build_agent`) drives the IVR navigator → a real compiled CallPlan → the gap pass → wrap-up, with a
second Gemini playing the payer rep. An evaluator LLM then grades the transcript across 11
dimensions (flow rules, contradictions, task handoffs, tool calls, IVR navigation, question
coverage, scope discipline, answer handling, gap conduct, closing, overall) and prints a scorecard.

These call live Gemini, so they are **excluded from `just check`** by the `evals` pytest marker and
never run by accident.

**Prerequisites:** Vertex ADC (`gcloud auth application-default login`) **and** a seeded local
Postgres (`just up && just migrate && just seed`) — the plan is read from the published
`schema_version` row, so an unseeded database skips the whole suite.

```bash
# full run: 3 scenarios, the cooperative rep over all 7 tasks / 182 fields (~12 min)
VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs

# fast loop: focused plans, a few turns each (~3 min)
VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs

# one scenario
VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs -k inactive

# make a verified evaluator failure fail the run (default: it only reports)
VERA_EVALS_JUDGE_STRICT=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
```

Three of those flags are load-bearing and easy to omit:

| flag | why you need it |
| --- | --- |
| `-m evals` | **required.** `addopts` carries `-m 'not evals'`, so omitting it silently runs only the 20 LLM-free tests and **no simulations at all** — which looks like a clean pass |
| `-s` | otherwise pytest swallows the transcript and the scorecard |
| `-rs` | shows skip *reasons* — without it an unseeded DB looks identical to "nothing to run" |
| `VERA_EVALS_ENABLED=1` | master switch; every simulation skips without it |
| `VERA_EVALS_FULL=1` | walks the whole plan instead of a narrowed subset |

A real run prints a `===== <scenario>: N tasks, M fields, … =====` banner per scenario. If you see no
banners, no simulation ran.

**Read two things in the output, not just the exit code.** The scenario banner reports the plan's
real mode (`focused` vs `full walk`), and each scenario ends with `N answers extracted` — a run that
extracted **0** means the Observer contributed nothing, so no rule could fire and the gap pass had no
state, yet most dimensions will still read `pass`.

The harness cannot see STT damage (text mode has no audio), real DTMF (`press_keypad` is mocked), or
a rule that fired too late to matter — extraction settles between turns, so rules fire earlier and
more reliably here than on a real call. It shortens the feedback loop; it does not replace a live
call.

The LLM-free parts (`test_judge_parsing.py`, 20 tests covering the evaluator's parsing, citation
checks and brief rendering) are deliberately **not** `evals`-marked and run in `just check`.

Defects it has already found are catalogued in
`docs/superpowers/plans/2026-07-30-call-flow-eval-findings-remediation.md`.

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
