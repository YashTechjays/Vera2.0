# vera_core — crypto, RLS, audit, observability (scoped)

Inherits the repo root `vera-backend/CLAUDE.md`. This is the security-critical core;
changes here carry the highest blast radius in the repo.

## No PHI tokenization (the voice pipeline)

The `vera_core.phi` tokenization wall (`PHIBoundary` over `phi_codec`; the `redact` /
`hydrate_for_speech` / `hydrate_raw` crossings and the session vault) was **removed on
2026-07-13**, along with the vendored `phi_codec` package. PHI now flows in **plaintext**
through the live pipeline — every hop (Deepgram, Vertex Gemini, Cartesia, Twilio, LiveKit)
is inside the BAA-covered trust boundary (repo-root `CLAUDE.md`). Do not reintroduce a
codec/boundary here without a compliance decision. De-identification survives only for what
LEAVES the boundary — logs, traces, spans (see Observability below).

**PHI at rest:** structured PHI in Postgres/Redis relies on Google **CMEK** at the storage
layer (Cloud SQL + Memorystore). Application-level column envelope encryption (`protect` /
`reveal`, `*_ct` columns) is **deferred to a later decision** — do not introduce it here
until that decision is made.

## Out-of-pipeline LLM calls go through `vera_core.llm.ResilientLLM` — always

Any LLM call outside the live voice cascade (summaries, analytics, extraction,
post-call processing) MUST be made through `vera_core.llm.ResilientLLM` with
`LLMSpec` provider/model selectors — never by instantiating a provider SDK or a
LiveKit plugin LLM client directly at a call site. ResilientLLM wraps
livekit-agents' FallbackAdapter (ordered provider chain, per-attempt timeout,
retries) and is the single place provider construction, secret resolution
(`OPENAI_API_KEY` via SecretProvider), and PHI-safe error logging live. Adding a
provider means one entry in `vera_core.llm.PROVIDERS`, nothing else. The live
cascade's LLM (the agent worker's AgentSession) is separate and stays in
`apps/agent_worker` — do not route it through ResilientLLM.

## Envelope encryption (`vera_core.config.kms`)

TOTP seeds are envelope-encrypted: AES-256-GCM under a per-user DEK, DEK wrapped by a
`KeyManagementService`. The DEK is ephemeral — never persisted. Use `seal(kms, plaintext)` /
`open_sealed(kms, ...)` from `vera_core.config.kms` for any new credential that needs the
same pattern. Never store the plaintext DEK or the plaintext seed anywhere outside the call stack.

In dev, `LocalDevKMS` wraps DEKs with AES-256-GCM under `LOCAL_KMS_MASTER_KEY` (env var).
In prod, `GCPCloudKMS` delegates to Cloud KMS (Workload Identity, see `adr/devops-todo.md` #2).
`build_kms(settings)` selects the implementation: set `VERA_KMS_KEY_NAME` → GCP; unset → local.

## RLS is authorization, not encryption

`vera_core.db.rls` pins every request transaction to `app.tenant_id` via `SET LOCAL`
(fail-closed: unset GUC → zero rows, `FORCE` RLS). RLS stops cross-tenant **row** access; it
does not protect a row's **contents**. Both are required; neither substitutes for the other.
Always do PHI work inside a `tenant_session(...)`.

## Audit — the HIPAA evidence trail (`vera_core.audit`)

Every PHI access and every authz allow/deny writes an `AuditRecord`:
`{actor, tenant, resource, fields, action, ts}`. Record field **names**, never field
**values**; carry tokens / counts / entity types only. The `audit_log` is append-only (the
migration makes it INSERT/SELECT-only, FORCE RLS) — never `UPDATE` or `DELETE` it, and never
add a code path that mutates a past record.

**Never construct `AuditRecord(...)` or `AuthAuditRecord(...)` directly at a call site —
go through the shared emit helper.** `AuthAuditRecord` → `emit_auth_event()` (`vera_core.audit`,
no `control_plane` dependency by design, so `auth/rbac.py` can call it without circularly
importing `api/v1/common`); a PHI-read `AuditRecord` → `emit_phi_read_audit()`
(`control_plane/api/v1/common.py`). Both exist because hand-rolled construction at each
call site silently drifted on shape — several endpoints forgot `request_id` or
`elevation_session_id` (the field linking a PHI read back to an active superadmin elevation
grant) before these helpers existed. A new event shape that doesn't fit either helper is a
signal to extend it or add a sibling — not license to hand-roll the record again.

**Timestamps come from the DB clock, never the app clock.** Every `created_at`/`updated_at`
and every audit/elevation time is minted by Postgres `now()` / `func.now()` (the `db/base.py`
mixins; the SECURITY DEFINER fns) — never Python `datetime.now()`. A single NTP-synced clock
of record is a HIPAA audit-integrity requirement; app-clock writes add cross-node skew. Even
future-bounds are DB-computed (`tenant_elevation.expires_at = now() + interval`).

## Observability (`vera_core.observability`, Langfuse)

Structured logs and Langfuse spans are **outside** the PHI-plaintext set — scrub before emit.
Never `logger.*(f"...{plaintext}...")`. Trace token IDs, reference IDs, counts, and shapes —
never raw values. Any trace or log of transcript content must be scrubbed to IDs / counts /
shapes first — there is no in-pipeline tokenizer to lean on now (`vera_core.phi` is gone), so
treat raw transcript text as unloggable. Langfuse may carry reference IDs and timings
(operational shape), never raw PHI.
