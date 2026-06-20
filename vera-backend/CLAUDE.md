# Vera 2.0 backend — PHI / HIPAA guardrails (repo root)

Vera is a HIPAA-regulated, multi-tenant AI **voice** platform. The rules below are
non-negotiable boundary discipline for **every** change in this repo. They are
deepened by nested `CLAUDE.md` files that load only when you touch the relevant code:

- `packages/vera_core/src/vera_core/CLAUDE.md` — PHI codec, crypto, RLS, audit, observability.
- `apps/control_plane/src/control_plane/CLAUDE.md` — PHI-returning HTTP endpoints.

## Build, test & layout (`just` runs everything; see README.md for detail)

- `just check` — the full CI gate: `lint` (ruff) + `typecheck` (mypy --strict) + `test` (pytest).
  Run it before claiming work is done.
- **After every implementation, run the `/simplify` skill** on the change (reuse /
  simplification / efficiency / altitude cleanup — quality only, not bug-hunting), then
  re-run `just check`, before claiming done or committing. Skip only for truly trivial edits
  (typo, one-line rename). Use `/code-review` for correctness bugs.
- `just up` then `just migrate` — local Postgres+Redis via docker compose, then Alembic.
  Integration tests skip without this; **RLS policies live in `migrations/`, not in models.**
  Migration `0001` materializes table DDL from `Base.metadata` at runtime — a table name may not
  appear literally in any migration; removing a model needs an explicit drop migration.
  A `SECURITY DEFINER` fn whose **param type** changes needs `DROP FUNCTION` + recreate +
  re-`ALTER FUNCTION … OWNER TO vera_definer_owner` — `CREATE OR REPLACE` leaves the old
  overload behind and the recreated fn loses its definer ownership (so BYPASSRLS stops applying).
- `LOCAL_KMS_MASTER_KEY` — required for local dev when `VERA_KMS_KEY_NAME` is unset.
  Generate once: `python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"`.
  In production, set `VERA_KMS_KEY_NAME` to the Cloud KMS key resource path instead (see `adr/devops-todo.md`).
- `just api` / `just worker` — run the control plane / agent worker.
- Code style: PEP 695 type params (`class Foo[T]`, `def f[T]`) — ruff rejects `Generic[T]`/`TypeVar`.
- Async runtime: **`asyncio` is the single async runtime** — the stack is asyncio-locked (livekit-agents,
  SQLAlchemy async, `redis.asyncio`, `pytest-asyncio`). `anyio` stays **transitive-only** (pulled by
  starlette/httpx/SDKs); never add it to a `pyproject.toml` `dependencies`, never `import anyio`. For
  structured concurrency use stdlib `asyncio.TaskGroup` / `asyncio.timeout`, not anyio equivalents.
- uv workspace: `vera_core` (shared core) + `phi_codec` (vendored) → consumed by `control_plane`
  (FastAPI, owns Postgres, no audio) and `agent_worker` (LiveKit). Python pinned 3.12 (`<3.13`).
- **Vendored `packages/phi_codec` is excluded from ruff & mypy** — don't lint/retype it; integrate
  at the `vera_core.phi` boundary only.

## Prime directive

PHI never leaves the trust boundary in plaintext. It never lands in a log, trace,
span, URL, path, query string, or cache. It never reaches the LLM as raw values, and
never reaches the browser as ciphertext or with a key. **A smaller boundary is a safer
boundary. When you cannot tell whether something is PHI, treat it as PHI.**

## Trust boundary

**INSIDE** (BAA-covered — PHI may flow): FastAPI control plane, agent worker, Cloud SQL
Postgres, Memorystore Redis, Deepgram (STT), Cartesia (TTS), Twilio (SIP), LiveKit
(self-hosted OSS — never LiveKit Cloud), Vertex AI Gemini (LLM), self-hosted Langfuse on GKE.

**De-identification point:** between STT output and LLM input. Raw identifiers in the
transcript are swapped for `[[TYPE_N]]` tokens before any LLM sees them and re-identified
only at the TTS / payer-tool seams (`vera_core.phi`). Structured PHI at rest is protected by
Google **CMEK** at the storage layer (Cloud SQL + Memorystore); application-level envelope
encryption of PHI columns is **deferred to a later decision** — do not introduce it.

**OUTSIDE** (PHI must be de-identified or access-controlled first): the browser (an
authorized, authenticated session — plaintext-over-TLS only), any non-BAA third party,
any analytics / observability / error-tracking SaaS.

## Bright lines — NEVER  (⛔ = also blocked by a PreToolUse hook)

- ⛔ NEVER log, print, trace, or attach to a Langfuse span: plaintext PHI.
- ⛔ NEVER put PHI in a URL, path, query string, route template, or Referer.
- NEVER persist PHI in browser storage (localStorage / sessionStorage / IndexedDB / cookies).
- NEVER put raw PHI in an LLM prompt — tokenize at the STT→LLM boundary (`vera_core.phi.redact`).
- NEVER store **plaintext** PHI in Redis. The session vault holds raw values **encrypted at
  rest**, keyed per session and wiped at call end; everything else caches tokens / opaque
  reference IDs, never values.
- NEVER use `livekit.agents.inference.*` (Cloud-only — we self-host LiveKit OSS): it streams
  call audio off-box to `agent-gateway.livekit.cloud`. Keep audio/turn models local/plugin —
  e.g. pin `interruption.mode="vad"`, never the auto-selected adaptive detector (`cascade.py`).

⛔ lines are also enforced by `../.claude/hooks/phi_guardrails.py`. The hook is a
conservative backstop, not the rule — these lines hold everywhere, including where no hook
watches. Opt-out is documented in the hook header.

## When in doubt, stop and ask

Infra obligations code can't enforce (clock NTP sync, CMEK, DB roles) go in
`adr/devops-todo.md` — add a row when a change *depends on* an infra property.

A blocked task is recoverable; a PHI disclosure is not. These files enforce boundary
*discipline* — they **cannot make a compliance determination**. Defer every field-level
"is this PHI?" and "who may see this?" call to the BAA and compliance review, and default
to over-protection until told otherwise. Never invent a compliance ruling to unblock yourself.
