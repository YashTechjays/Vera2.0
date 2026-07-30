# Filled Form Eval — post-call re-read pipeline (design)

**Date:** 2026-07-09
**Status:** Draft for review
**Phase:** 1 of 4 (Post-Call & Output). Foundation for Retry Call and Dispute Resolution.
**Owner:** yash@techjays.com

---

## 1. Context & problem

Vera is a HIPAA voice platform: an AI agent calls insurance payers and fills an IBV
(insurance benefit verification) form. Today the **data model** for post-call output is
fully built (`field_answer`, `field_evaluation`, `call_form_snapshot`, the reserved
`AI_PROCESSING` form status), but **nothing produces `ai_call` field values**:

- `agent_worker` does not persist any extraction.
- No `field_answer(source=ai_call)` rows are ever written.
- `field_evaluation` is unused.
- The call-end callback transitions `IN_CALL → COMPLETED` directly, with no eval step.

Downstream work is blocked on this: **Dispute Resolution** has nothing to dispute (disputes
are derived from `ai_call` values diverging from the intake/human baseline), and **Retry
Call** cannot compute "which fields are still missing/low-confidence." This phase builds the
missing core: a **post-call LLM re-read** that reads the finished call's transcript, extracts
every collected field with per-field confidence and an evidence span, scores each extraction
with a second judge pass, persists the results, and advances the form to a terminal state.

### Approved decisions (from brainstorming)

1. **Sequencing:** Foundation first — build Filled Form Eval before Retry, Disputes, Export.
2. **Eval role:** Extractor **and** judge. Pass 1 extracts into `field_answer(ai_call)`;
   pass 2 scores each into `field_evaluation(supported, confidence)`.
3. **Trigger/placement:** A Redis-stream consumer in the control plane (mirrors
   `worker_events.py`); the callback enqueues and returns fast.
4. **Extraction scope:** Extract **all** fields; reconcile identifier-token values against the
   intake baseline (match → confirm, mismatch → flag).
5. **Model:** Gemini **Flash** for both passes to start.
6. **Worker seam:** Included — the worker stashes a per-call `field_path → token` map at call
   start so reconciliation can match identifier fields without re-identification.

---

## 2. PHI posture (read first)

- The pipeline **consumes the transcript exactly as stored**. Transcripts are already
  de-identified during the call (`agent_worker` `redact()` at the STT→LLM boundary;
  `transcript_publisher.py` publishes finalized, tokenized turns). The pipeline therefore
  adds **no tokenizer, no vault, no re-identification** of its own — honoring "ignore the phi
  tokenizer for now" — and **raw PHI never reaches Gemini**, because it is not in the stored
  transcript to begin with.
- `evidence` stored on `field_answer`/`field_evaluation` is the **verbatim tokenized turn
  text** — safe at rest under CMEK.
- Reconciliation of identifier fields is **token-identity only** (compare the token seen in
  the transcript to the token minted for the seeded intake value). No raw value, no live
  vault. `open_session(known=…)` already seeds intake PHI so identical raw values yield
  identical tokens within a session (`phi/boundary.py:53`).
- Every PHI read/write in the pipeline emits an `AuditRecord` carrying field **names/counts
  only**, never values. All timestamps come from the DB clock (`func.now()`).
- **This design does not send raw PHI to the LLM.** See Open Decision D1 for the deferred,
  compliance-gated question of whether raw values may ever be sent to (BAA-covered) Vertex.

---

## 3. Architecture

```
worker  ── POST /calls/{call_id}/status {status: completed} ──▶  control_plane callback
                                                                    │
  update_call_status():                                             │
    • call.current_status = completed; CallEvent(STATUS)           │
    • write call_form_snapshot.before_state (current answers)      │
    • form: IN_CALL → AI_PROCESSING                                │
    • XADD vera:post-call  {tenant_id, form_id, call_id}           │
    • return 200  ◀── never blocks on the LLM ─────────────────────┘

post_call consumer  (group "post-call", block-read, ack)
    1. XREADGROUP job  (TimeoutError = idle tick, per CLAUDE.md)
    2. load tenant, form, schema_version, transcript (ordered by seq),
       intake baseline, field_path→token map (Redis, per call)
    3. IDEMPOTENCY GUARD: if ai_call answers already exist for call_id → skip to step 8
    4. PASS 1 extract  → Gemini structured output:
          { field_path: {value, confidence_0_100, evidence_seq} }
    5. reconcile + persist:
          • non-token value        → store as-is
          • token identifier value → compare to seeded intake token
                match    → store intake value (confirmed, source=ai_call)
                mismatch → store token-flagged, mark field for review
          write field_answer(source=ai_call, confidence, evidence_seq, evidence,
                             is_current=True) via the merge invariant (demote prior current)
    6. PASS 2 judge  → Gemini per answer:
          { supported: bool, confidence_0_100, evidence }
          write field_evaluation(answer_id, supported, confidence, evidence)
    7. write call_form_snapshot.after_state; recompute form.completion_pct
    8. transition form:
          clean            → AI_PROCESSING → COMPLETED
          unresolved / any judge.supported=false / below threshold
                           → AI_PROCESSING → EXCEPTION_REVIEW
    9. try_dispatch()   ← frees the VA concurrency slot only now
   10. XACK
```

**Why `AI_PROCESSING` gates the concurrency slot:** the dispatcher already counts
`IN_CALL | AI_PROCESSING` forms as occupying a VA
(`services/queue_dispatcher.py::try_dispatch`), so a form under eval correctly holds its slot
until the pipeline finishes and calls `try_dispatch()`.

---

## 4. Components (new & changed)

### New

| File | Responsibility |
|---|---|
| `packages/vera_core/src/vera_core/services/post_call_eval.py` | Pure orchestration: `evaluate_call(session, deps, job) -> EvalOutcome`. No I/O beyond the injected session + LLM client; unit-testable with a fake LLM. Owns extract → reconcile → persist → judge → snapshot → completion% → status decision. |
| `packages/vera_core/src/vera_core/integrations/llm.py` | Thin **Vertex AI Gemini** client behind a `Protocol` (`LLMClient`): `extract(schema, transcript) -> dict` and `judge(schema, answers, transcript) -> dict`. Structured output. Flash model id in config. |
| `packages/vera_core/src/vera_core/events/post_call.py` | `PostCallJob` payload + stream name/group constants (`vera:post-call`, group `post-call`). |
| `apps/control_plane/src/control_plane/post_call.py` | The Redis-stream consumer loop. Copies the canonical `TimeoutError`-as-idle handling from `worker_events.py::_read_once`. Booted in `main.py` lifespan as an `asyncio.create_task`. |

### Changed

| File | Change |
|---|---|
| `apps/control_plane/src/control_plane/api/v1/calls.py::update_call_status` | On `COMPLETED`: write `before_state` snapshot, transition `IN_CALL → AI_PROCESSING`, `XADD` the job. (Failure statuses keep today's `CALL_FAILED`/auto-retry path unchanged.) |
| `packages/vera_core/src/vera_core/services/form_state_machine.py` | Allow `AI_PROCESSING → EXCEPTION_REVIEW` (COMPLETED / CALL_FAILED already allowed from AI_PROCESSING). |
| `apps/control_plane/src/control_plane/main.py` | Start/stop the `post_call` consumer in lifespan. |
| `apps/agent_worker/.../main.py` (or the seam module) | After `open_session(known=…)`, read back and stash the per-call `field_path → token` map in Redis under the call key (opaque tokens, non-PHI) for reconciliation. Degrades gracefully if absent. |
| `apps/control_plane/pyproject.toml` | Add the Vertex/Gemini SDK dependency (`google-genai`). |
| Config (`vera_core/config`) | Gemini model id (Flash), Vertex project/location, review threshold. |
| `migrations/` | Only if a column/constraint is needed (none anticipated — tables exist). No raw `op.add_column`; use the idempotent guards per `CLAUDE.md` if any add is required. |

---

## 5. Data model (existing tables, how we use them)

- **`field_answer`** — one **current** `ai_call` row per collected field. The partial unique
  index `fa_current_uq (form_id, field_path) WHERE is_current` enforces exactly one current
  value; retries demote the prior current and insert a new one. Columns used: `value` (JSONB,
  PHI), `source='ai_call'`, `confidence` (0–100), `evidence_seq` (→ `transcript.seq`),
  `evidence` (tokenized text), `call_id`, `is_current`.
- **`field_evaluation`** — one judge verdict per `ai_call` answer: `answer_id`, `supported`,
  `confidence`, `evidence` (tokenized).
- **`call_form_snapshot`** — `before_state` (written in the callback) and `after_state`
  (written by the pipeline); 1:1 with the call, immutable audit artifact.

No new tables or columns are expected. All new rows are additive; the audit log stays
append-only.

---

## 6. LLM contract

**Pass 1 — extract.** Input: the form schema's collectable paths (from the DSL
`collection_paths()`), and the transcript as numbered turns `(seq, role, text)`. Output
(structured / JSON schema): for each `field_path`, `{ value, confidence: 0-100,
evidence_seq }` where `evidence_seq` is the transcript turn that supports the value. Model is
instructed to omit fields it cannot find (no hallucinated values) and to cite the turn.

**Pass 2 — judge.** Input: each extracted `{field_path, value, evidence_seq}` plus the cited
turn(s). Output: `{ supported: bool, confidence: 0-100, evidence }`. A field is "needs
attention" when `supported=false` **or** `confidence < REVIEW_THRESHOLD` (config default TBD,
proposed 60).

Both passes run on **Gemini Flash** with structured output. The `LLMClient` `Protocol` lets
tests inject a deterministic fake.

---

## 7. Status decision (COMPLETED vs EXCEPTION_REVIEW)

After persistence, the form goes to:

- **`COMPLETED`** when every required field is filled, no field is token-mismatch-flagged, and
  no field is "needs attention" (judge).
- **`EXCEPTION_REVIEW`** otherwise (a human worklist item).

The precise "required field" set comes from the schema (system/required leaves). The
completion-% recompute reuses the existing helper used by intake/dispute flows.

> Note: turning "needs attention" into an actual **retry** (re-queue + partial prompt) is the
> **Retry Call** phase, not this one. This phase only records confidence/evidence and routes to
> review vs completed.

---

## 8. Error handling & idempotency

- **Redelivery-safe:** step 3 guards on existing `ai_call` answers for the `call_id` (or a
  written `after_state`), so a re-delivered job is a no-op ack. No double-write; the merge
  invariant is never violated.
- **LLM failure:** bounded retries with backoff; on exhaustion the form is routed to
  `EXCEPTION_REVIEW` (never left stuck in `AI_PROCESSING`), the failure is audited, and
  `try_dispatch()` still runs to free the slot.
- **Idle stream reads:** use `except TimeoutError as RedisTimeoutError: continue` exactly as
  `worker_events.py` and `transcript.py` do — a blocking `xreadgroup` that times out **raises**,
  it does not return empty.
- **Missing token map:** reconciliation degrades to "route token-valued identifier fields to
  review" rather than guessing.
- **Boot verification (repo rule):** because this adds a long-lived background loop, verify by
  actually booting — `just up`, run the consumer, watch it idle two windows — not by pytest
  alone.

---

## 9. Testing

**Unit (`post_call_eval` with a fake `LLMClient`, no telephony):**
- extraction maps fields correctly; omitted fields stay unfilled
- non-token value stored as-is
- token identifier: match → intake value + confirmed; mismatch → flagged for review
- judge `supported=false` / low confidence → `EXCEPTION_REVIEW`
- all-good → `COMPLETED`; completion-% recomputed
- redelivery guard: second run is a no-op
- LLM-failure path → `EXCEPTION_REVIEW`, slot freed

**Integration (docker Postgres + Redis, fake Gemini):**
- callback → `AI_PROCESSING` + snapshot.before_state + job enqueued
- consumer → answers + evaluations + snapshot.after_state written, status advanced,
  `try_dispatch()` fired
- RLS: rows are tenant-scoped; audit records emitted with field names only

**Gate:** `just check` (ruff + mypy --strict + pytest) green → run `/simplify` on the change →
re-run `just check` → then done/commit (per repo `CLAUDE.md`).

---

## 10. Open decisions (resolve at review)

- **D1 — raw PHI to Vertex (compliance-gated).** This design never sends raw PHI to Gemini; it
  reads the already-de-identified transcript. If the team wants raw values sent to BAA-covered
  Vertex (removing the de-identification-before-LLM control), that is a **separate compliance
  determination** — not self-approved here. Add an `adr/devops-todo.md` row and get BAA/
  compliance sign-off before any such change. Default: keep the safe posture.
- **D2 — worker seam scope vs "ignore tokenizer."** The worker seam (stash `field_path→token`)
  exists solely to enable identifier reconciliation. It is included per instruction. If we
  ultimately want **zero** token handling this phase, drop the seam and route all token-valued
  identifier fields to review instead of reconciling. Confirm which.
- **D3 — `REVIEW_THRESHOLD` default** (proposed 60) and whether it is a constant or
  per-tenant config. Proposed: a single config constant now (YAGNI), per-tenant later.
- **D4 — judge granularity/cost.** One judge call per answer vs one batched judge call per
  form. Proposed: batched per form to cut cost/latency; revisit if accuracy suffers.

---

## 11. Out of scope (later phases)

- **Retry Call** — missing-fields calculation as a *retry trigger*, partial (re-ask-only)
  prompt, `call_lineage` population.
- **Dispute Resolution** — FE surfacing of confidence/evidence, wiring inline accept/override/
  correct actions (backend endpoint already exists).
- **Export** — template mapping → XLSX/PDF + disclosure logging.

---

## Appendix A — roadmap context (the other three phases)

| Phase | Depends on this | Rough shape |
|---|---|---|
| 2. Retry Call | Yes (needs confidence/missing) | Compute missing/low-confidence set → trigger re-queue on completed-but-incomplete → partial prompt (ask only missing) → populate `call_lineage`. |
| 3. Dispute Resolution | Yes (needs `ai_call` values) | Mostly FE: wire `IbvFormModal`/`FieldRow`/`DisputeControls` to `POST /disputes:resolve`; surface confidence + evidence; verify audited accept/override/correct end-to-end. |
| 4. Export | No (independent) | Add XLSX (openpyxl) + PDF (reportlab/weasyprint) libs; template-mapping layer; endpoint writing `export_artifact` (gcs_uri, disclosed_at) with disclosure audit; FE download. |
