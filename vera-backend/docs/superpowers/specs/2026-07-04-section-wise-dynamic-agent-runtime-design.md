# Section-wise Dynamic Voice Agent Runtime — Design & Implementation Plan

**Date:** 2026-07-04
**Status:** M1 implemented; M2–M4 planned
**Depends on:** voice-pipeline-core-cascade-design (2026-06-19), persona-tweak-runtime-knob-design (2026-06-23)

> **AMENDMENT (2026-07-06) — PHI tokenization removed from M1 (dev simplification).**
> The M1 prefill no longer tokenizes or KMS-seals values. Prefilled DB-known
> values now flow into the plan as **raw values** (`PlanField.confirm_value`);
> there is no `[[TYPE_N]]` token, no `vera:phiseed:` key, no sealed seed, and no
> `PrefillEntry`. Wherever the M1 sections below describe token minting, sealing,
> `open_session(known=…)`, or "tokens only — no raw PHI", read them as superseded:
> the call plan (and its `vera:callplan:{room}` Redis key) now hold **plaintext
> prefilled PHI** and are synthetic-data-only until protection is reintroduced
> (adr/devops-todo.md #8). The M2–M4 sections still describe the original
> token-based design as the future intent.

## Context

Today the agent worker runs **one static `VeraAgent`** with a hardcoded prompt
(`apps/agent_worker/src/agent_worker/prompt.py`). The versioned authoring catalog
(`SchemaVersion.schema_json` seeded from `data/form_schemas/ibv_form_standard.json`,
21 sections + 49 policy fragments + per-phase `phase_order` composition recipes) is
**never consumed at runtime**. The target flow:

1. **Schema → runtime field list**: compile the canonical schema into a flattened
   `field_path` list with validation rules, **pre-filled from the DB** (known values
   become confirm-type interactions via the schema's `confirm_only`/`confirm_value`).
2. **One agent per section, fully dynamic**: N sections → N agents, instantiated at
   runtime from the compiled plan; LiveKit handles handoff between section agents.
3. **Observer Agent**: parallel extraction listener whose scope re-targets to the
   active section's `field_path` schema on every handoff.
4. **State**: extracted values → DB as source of truth, plus a shared per-call state
   for cross-section contradiction detection/reconciliation.

**Locked decisions**: per-section agents (not per-phase); extraction persists via
Redis events → control plane (worker keeps zero Postgres); compiled call plan handed
to the worker via Redis keyed by room name; delivery in phased slices.

## Verified ground truth

Read from the repo and the installed SDK, not assumed:

- **livekit-agents 1.5.17** (`.venv/.../livekit/agents/`): handoff = a
  `@function_tool` returning an `Agent` (or `(Agent, str)`) →
  `session.update_agent(...)`; passing `chat_ctx=self.chat_ctx` to the next agent
  preserves history (the framework inserts an `AgentHandoff` chat item).
  `AgentSession` is `Generic[Userdata_T]` with a `userdata=` kwarg — the shared-state
  home. STT/LLM/TTS/`turn_handling` live on the **session** and survive every handoff,
  so `cascade.py` stays byte-identical.
- **Schema facts**: `phase_order` refs resolve against `global_policies` ∪ the four
  anchor sections' `section_policies` (`insurance_information`→phase_2,
  `diagnostic_labs_xray_ultrasound`→phase_3, `infertility_limits`→phase_4,
  `enrollment`→phase_5; male-partner fragments are global). Six sections are
  context-only ("use as context, do not ask"). Fields can be nested objects
  (recurse into `field_path`, e.g. `diagnostic_labs_xray_ultrasound.diagnostic_testing.*`).
  Three `confirm_only` fields use PHI placeholders (`{patient_name}`,
  `{date_of_birth}`, `{member_id}`).
- **PHI**: `PHIBoundaryProtocol.open_session(known=...)` and `phi_codec.seed_session`
  already accept pre-minted `[[TYPE_N]]` tokens — control-plane token minting is
  forward-compatible with the real codec.
- **`Call.prompt_version_id` exists** (`models/call.py`) but `POST /calls` never sets
  it — lineage gap this plan closes.
- **Worker has no Postgres dependency** (Redis + livekit only) — deliberate; kept.

## Architecture

```
POST /calls (control plane)                       agent worker (no DB)
──────────────────────────                        ─────────────────────────────
load PatientForm + current FieldAnswers           entrypoint:
load published SchemaVersion (+PromptVersion)       plan  = GET  vera:callplan:{room}
compile_call_plan(schema, prefill, tweak)           seed  = GETDEL vera:phiseed:{room} → open_sealed(kms)
  → CallPlan (tokens only — no raw PHI)             boundary.open_session(sid, known=seed)
mint PHI tokens for prefill values                  userdata = CallRuntime(plan)
SET vera:callplan:{room}  (TTL)                     session.start(SectionAgent(first_pending))
SET vera:phiseed:{room} = seal(kms, values)       complete_section tool → returns next SectionAgent
call.prompt_version_id = published version          (livekit handoff, chat_ctx carried)
create room + explicit dispatch (unchanged)       Observer task: taps redacted transcript events,
start ExtractionConsumer(room)                      Gemini structured extraction scoped to the
                                                    ACTIVE section, XADD vera:extract:{room}
ExtractionConsumer (control-plane lifespan)
  XREAD vera:extract:{room} → tenant_session →
  FieldAnswer(source=ai, event_key unique,
  is_current flip) + audit(field NAMES)
```

PHI at every seam: Redis carries **tokens only**; raw prefill values cross only
KMS-sealed (`vera_core.config.kms.seal/open_sealed`); LLM prompts contain `[[TYPE_N]]`
tokens hydrated exclusively at `tts_node` (existing `seams.hydrate_stream`); the
observer consumes the redacted side (same seam as `transcript_publisher.py`).

---

## M1 — Compiler + Redis stash + worker consumes compiled plan (single agent)

**New `packages/vera_core/src/vera_core/callplan/`** (pure, DB-free):

- `model.py` — pydantic contract (`extra="forbid"`). **`PlanField` carries the FULL
  field definition from the schema, not just the confirm pair** (verified key
  inventory from `ibv_form_standard.json`):

  ```
  PlanField {
    field_path, title, description, type,
    prompt_role: question|verifiable_question|prose,   # 106 fields — drives ask behavior
    required,                                          # from required_state
    enum, constraint_ref,                              # validation (constraint_library)
    verbatim_prompt,                                   # 26 fields — exact ask script
    ask_prompt, ask_category,                          # prompt.ask / prompt.category (17)
    metadata: {cpt_codes, icd10} | None,               # 8 fields — agent SPEAKS these codes
    rules: list[FieldRule],                            # conditional effects (below)
    policies: list[FieldPolicy],                       # field-level after_answer checkpoints
    group_integrity,                                   # all_or_nothing groups
    mode: ask|confirm|skip,
    confirm_token,                                     # a token, NEVER a raw value
  }
  FieldRule {effect: make_required|terminate_call_when|ask_question|auto_fill,
             match, conditions: [{field, comparison, value}], summary}
  FieldPolicy {title, verbatim, exact_text}            # e.g. after_answer MANDATORY CHECKPOINT
  ```

  `ui.widget` is the one field key deliberately dropped — form-UI-only, meaningless
  to the voice runtime.
  - `PlanSection {section_key, title, description, phase_key,
    mode: collect|confirm|context|skip, instructions, fields}`.
  - `CallPlan {version: 1, room_name, tenant_id, call_id, schema_version_id,
    prompt_version_id, greeting, flat_instructions, sections}`.
  - `PrefillEntry {field_path, value, entity_type, token}` — control-plane-side input
    only, never serialized into the plan.
- `compiler.py` — `compile_call_plan(schema_json, prefill, tweak, ...) -> CallPlan`,
  `CompileError` (fail-closed at POST /calls, never mid-call):
  1. Fragment index from `global_policies` ∪ all `section_policies`, keyed by the
     `source` suffix; `FORBIDDEN_PATTERNS[x]` matched case-insensitively; dangling
     `phase_order` ref → `CompileError`.
  2. Section→phase via the four anchors; context sections get `mode="context"`.
  3. Recursive field flattening (object types → nested `field_path`), **carrying
     every `PlanField` key above** — validation, verbatim prompts, CPT/ICD metadata,
     rules, field policies.
  4. Prefill overlay: prefilled or `confirm_only` → `mode="confirm"` + token
     (placeholders like `{patient_name}` resolve to the matching prefill token);
     fully-answered → `skip`; section mode rolls up (all-confirm → `confirm`,
     all-answered → `skip`).
  5. Per-section `instructions` = the section's phase recipe with `<SECTIONS>`
     replaced by only this section's field block, which renders per field:
     `verbatim_prompt`/`ask_prompt` verbatim, enum values, `metadata` CPT/ICD-10
     codes (spoken-digit form per the schema's phonetic fragments), field `policies`
     `exact_text` (the after-answer checkpoints), and `rules` in LLM-readable form
     ("required only when coverage_type is Family"); non-PHI placeholders
     (`{clinic_name}`, `{verified_by}`, `{phase_number}`, `{questions_list}`, …)
     substituted from compile inputs; PHI placeholders → tokens; `COMPLETE_PHASE_TOOL`
     fragment adapted to the `complete_section` tool; `tweak.extra_instructions` +
     `CARTESIA_MARKUP_GUIDE` (moved here from `prompt.py`) appended.
  5b. **Rules engine, compile half**: conditions referencing prefilled fields are
     resolved at compile time (e.g. `coverage_type=Family` already known → spouse
     fields flip to required); conditions on not-yet-answered fields stay in the plan
     as `FieldRule` for the runtime — M2 renders them into section instructions, and
     M4's `CallRuntime.record()` re-evaluates them as answers arrive (dynamic
     requiredness, `terminate_call_when` → instruct end_call, `auto_fill` →
     synthetic answer event).
  6. `flat_instructions` = M1 single-agent composite (persona + shared rules + every
     non-context section's field block + confirm blocks + end_call guidance) —
     validates the pipeline before per-section agents land in M2.
- `store.py` — `CallPlanStore` over `redis.asyncio` (mirrors `RedisTranscriptStore`):
  keys `vera:callplan:{room_name}` (plan JSON, `EX call_plan_ttl_seconds`) and
  `vera:phiseed:{room_name}` (KMS-sealed prefill blob, GETDEL read-once). Token
  minting: per-entity-type counters using `phi_codec` entity-type names so
  `seed_session` accepts them verbatim when the real codec lands.

**Control plane** — `api/v1/calls.py start_call`:
1. Fetch current `FieldAnswer` rows (`form_id, is_current`) + `intake_payload` leaves
   → `PrefillEntry` map (entity types for the three confirm fields from a small
   compiler-side map; `schemas/form_template.py`'s `entity_type` is the eventual
   authoring home — noted as TODO).
2. Load published `SchemaVersion` + `PromptVersion`; set `call.prompt_version_id`.
   `PromptVersion.composite_json` stays the archival v1 snapshot; the runtime compiles
   from `schema_json` at call time (in-process cache keyed by `schema_version_id`).
3. Compile + stash both Redis keys **before** `livekit.create_call_room` (the worker
   must never race an absent plan). Emit a PHI-access `AuditRecord` (field **names**).
4. Dispatch metadata unchanged (PersonaTweak only) — the plan rides Redis.

`main.py` lifespan: `app.state.call_plan_store`; annotated dep in `api/v1/common.py`.
New setting `call_plan_ttl_seconds: int = 3600`.

**Worker** — `main.py`: for a canonical call room, fetch plan + seed
(`open_sealed(build_kms(settings), ...)`) → `boundary.open_session(session_id,
known=...)`. Plan missing/invalid → **fall back to today's static agent** (mirrors the
`parse_persona_tweak` fail-safe posture; Voice Lab/console rooms never have a plan and
stay unchanged). `agent.py` receives `plan.flat_instructions` + `plan.greeting`.
Worker now needs KMS env (`LOCAL_KMS_MASTER_KEY` / `VERA_KMS_KEY_NAME`) — add an
`adr/devops-todo.md` row.

**M1 tests**
- `tests/unit/callplan/test_compiler.py` against the real `ibv_form_standard.json`:
  every `phase_order` ref resolves; sections partition into expected phases; nested
  `diagnostic_testing` flattens to 3 `field_path`s; **full key carry-through** (the 26
  `verbatim_prompt`s, 8 `metadata` CPT/ICD entries, all `rules`/`policies` survive
  into `PlanField` and render into section instructions); compile-time rule
  resolution (prefilled `coverage_type=Family` → spouse fields required); confirm
  fields get tokens; **no prefill value substring appears anywhere in the serialized
  plan** (the key PHI regression test); `CompileError` on a dangling ref.
- `tests/unit/callplan/test_store.py`: seed round-trip through
  `LocalDevKMS(master_key=b"a"*32)`; GETDEL read-once semantics.
- Worker: plan-missing fallback; `open_session` called with seeded `known`.
- Integration: POST /calls stashes both keys + sets `prompt_version_id`.

**Shippable**: `/calls` calls run on the compiled prompt; everything else identical.

## M2 — Per-section dynamic agents + handoffs + skip/confirm

- `agent_worker/runtime.py` — `CallRuntime` dataclass (the `session.userdata`):
  `plan`, `section_status: dict[str, Literal["pending","active","done","skipped"]]`,
  `answers` (filled by M3/M4), `next_pending(after) -> PlanSection | None`
  (skips `context`/`skip` sections).
- `agent_worker/section_agent.py` — refactor the PHI node overrides out of `VeraAgent`
  into a `PHIWallAgent(Agent)` base (stt/tts/transcription nodes + `end_call` tool;
  `seams.py` untouched), then:

  ```python
  class SectionAgent(PHIWallAgent):
      def __init__(self, boundary, session_id, runtime, section, *, chat_ctx=None):
          super().__init__(instructions=section.instructions, chat_ctx=chat_ctx, ...)

      async def on_enter(self):  # first section: say(plan.greeting); later: generate_reply()

      @llm.function_tool(name="complete_section")
      async def _complete_section(self):
          runtime = self.session.userdata
          runtime.section_status[self._section.section_key] = "done"
          nxt = runtime.next_pending(self._section.section_key)
          if nxt is None:
              return "All sections complete. Say your closing line, then call end_call."
          return (SectionAgent(..., nxt, chat_ctx=self.chat_ctx),
                  f"Section complete. Continue with {nxt.title}.")
  ```

- `cascade.py`: `build_session(vad, userdata) -> AgentSession[CallRuntime]` — only the
  generic parameter and `userdata=` kwarg change; the single `turn_handling` block and
  plugin config stay byte-identical (extend `test_cascade.py` to assert it).
- **`vera_core/phi/seeded.py` — `SeededPHIBoundary`** (interim): with today's
  `PassthroughPHIBoundary`, `[[NAME_1]]` in a confirm prompt would be *spoken
  literally*. `SeededPHIBoundary` implements `PHIBoundaryProtocol` with redact =
  passthrough and `hydrate_for_speech` resolving the seeded token map (unknown tokens
  neutralize like the real boundary). `build_phi_boundary` returns it when a seed is
  present. Hydrate-only by design; deleted when devops-todo #8 (real codec) lands.

**M2 tests**: direct tool invocation (existing `test_end_call_tool.py` pattern) for
the handoff chain, skip logic, and last-section closing string; `SeededPHIBoundary`
unit tests (hydrate seeded, neutralize unknown, wipe on close); `RunResult`-based
behavior tests marked/skipped like other LLM-touching tests.

**Shippable**: same call flow, now section-by-section with skip/confirm.

## M3 — Observer + extraction events + control-plane persistence

- `vera_core/extraction.py` — mirrors `transcript.py`: `ExtractionEvent`
  (`kind: answer|contradiction|section_status`, `event_key` =
  `<room_name>:<monotonic seq>`, `section_key`, `field_path`, tokenized
  `value`/`evidence`, `confidence`, `ts`), `RedisExtractionStore`
  (`vera:extract:{room_name}`, XADD + rolling TTL + ended sentinel + tailing read),
  `ExtractionService`.
- `agent_worker/observer.py` — `attach_observer(session, service, room_name, runtime,
  llm_factory)`: taps `user_input_transcribed` (final) + `conversation_item_added` —
  the same de-identified seam as `transcript_publisher.py`; debounced
  single-in-flight extraction (owned `asyncio.TaskGroup`, cancelled via
  `ctx.add_shutdown_callback`); reads `runtime`'s **active section at call time**, so
  re-scoping on handoff is automatic with zero coupling to handoff code; calls Gemini
  via a separate `google-genai` client (`vertexai=True`,
  `response_mime_type="application/json"` + response schema — session pipeline
  untouched, in-boundary Vertex, never `livekit.agents.inference.*`); publishes only
  deltas vs `runtime.answers`; best-effort (Redis failure logs, never kills the call);
  `end()` sentinel in the shutdown callback.
- **Migration** (`just makemigration`, random hex id): `field_answer.event_key:
  str | None` + partial unique index (`WHERE event_key IS NOT NULL`) — the durable
  exactly-once guard against stream redelivery and consumer restarts.
- `control_plane/extraction_consumer.py` — `ExtractionConsumerRunner` on `app.state`:
  `start(room_name)` from `start_call`; startup `SCAN vera:extract:*` reconciliation
  (resume from stream id 0 — idempotent via `event_key`). Per event:
  `parse_room_name` → `tenant_session(...)` → `INSERT … ON CONFLICT (event_key) DO
  NOTHING`; on real insert, flip the prior `is_current` row then insert the new
  `source=ai` row with call provenance (same order as the human-answer path —
  preserves `fa_current_uq`); DB-clock timestamps only; audit with field **names**;
  `section_status` events → `CallEvent` rows. Lifespan shutdown cancels cleanly.
- Settings: `extraction_stream_ttl_seconds` (3600), `observer_enabled: bool = True`.

**M3 tests**: store/service round-trip + sentinel; observer retarget/delta-only/
error-swallow (mirror `test_transcript_publisher.py`); consumer idempotency under
duplicate delivery; `fa_current_uq` under two answers for one field; RLS-scoped write
(integration, needs `just up`).

**Shippable**: forms fill in near-real-time during calls; the review UI dispute
derivation works off the new rows unchanged.

## M4 — Shared state + contradiction handling

- `CallRuntime` grows `AnswerState {value, confidence, evidence_ts, section_key,
  history}` and `record(field_path, value, ...) -> Contradiction | None`, comparing
  via `vera_core.forms.review.normalize_value` (single normalization rule repo-wide).
- `record()` also re-evaluates pending `FieldRule`s against the new answer (dynamic
  requiredness, `terminate_call_when` flags, `auto_fill` → synthetic answer event) —
  the runtime half of the rules engine compiled in M1.
- Observer calls `runtime.record(...)` before publishing; on contradiction, publishes
  `kind="contradiction"` (with the superseded tokenized value) and adds the field to
  `runtime.pending_contradictions`.
- Reconciliation (no mid-turn prompt injection — too racy): `complete_section` returns
  a hold string ("Conflicting answers were recorded for <field title>. Re-confirm it
  before completing this section.") instead of the next Agent while contradictions are
  pending; the fresh extraction clears the flag. The compiler adds one standing global
  fragment to every section prompt describing this behavior. DB-side, `FieldAnswer`
  history + the existing dispute machinery surface the conflict to humans — **no new
  schema**.

**M4 tests**: pure `CallRuntime` record/contradiction/normalization matrix;
hold-then-release `complete_section` sequence; consumer persists both rows with the
latest winning `is_current`.

---

## Critical files

Modify:
- `apps/agent_worker/src/agent_worker/{main,agent,cascade,prompt}.py`
- `apps/control_plane/src/control_plane/api/v1/calls.py`, `control_plane/main.py`,
  `api/v1/common.py`
- `packages/vera_core/src/vera_core/config/settings.py`

Create:
- `packages/vera_core/src/vera_core/callplan/{model,compiler,store}.py`
- `packages/vera_core/src/vera_core/extraction.py`
- `packages/vera_core/src/vera_core/phi/seeded.py`
- `apps/agent_worker/src/agent_worker/{runtime,section_agent,observer}.py`
- `apps/control_plane/src/control_plane/extraction_consumer.py`
- one migration (M3)

## Open risks

1. **Chat-context growth over ~15 handoffs** — `chat_ctx` carry-over is verified, but
   a 40-minute call may need truncation (last N turns + a compiler-generated "answers
   so far" summary from runtime). Defer until latency data says so.
2. **Tokens inside persisted extraction values** once the real codec lands (a rep
   echoing an identifier leaves `[[TYPE_N]]` in a `FieldAnswer.value`; the control
   plane cannot hydrate — the vault dies with the call). Options: worker-side strict
   `hydrate_raw` + KMS-sealed extraction events, or tokenized persistence + post-call
   rehydration. **Needs a compliance ruling — flagged, not solved here.**
3. **`SeededPHIBoundary` scope creep** — hydrate-only; must not grow detection
   features (that is the codec's job).
4. **Multi-replica consumer double-consume after rolling deploys** — harmless under
   `event_key` idempotency; document in the consumer docstring.
5. **livekit `RunResult` evals need a live LLM** — unit tests lean on direct tool
   invocation + pure compiler tests; `RunResult` tests marked like other
   network-touching tests.

## Verification (per milestone)

- `just check` (ruff + mypy --strict + pytest) after every milestone; `/simplify` per
  repo rule before claiming done.
- **M1**: `just up && just migrate`, seed, POST /calls → inspect
  `vera:callplan:{room}` in Redis (tokens only, no raw PHI); `just worker` + join →
  agent speaks the compiled greeting/prompt; delete the Redis key → static fallback.
- **M2**: live call walks section→section; `AgentHandoff` items visible in the
  transcript SSE; a fully-prefilled section is skipped; a confirm field is spoken with
  the real value (SeededPHIBoundary hydration at `tts_node`).
- **M3**: during a live call, `XRANGE vera:extract:{room}` shows events and
  `field_answer` rows appear with `source=ai` and `is_current` flipping; kill/restart
  the control plane mid-call → no duplicate rows (`event_key`).
- **M4**: answer a field, contradict it in a later section → hold string on
  `complete_section`, re-confirm clears it; history shows both rows and the dispute
  derivation flags it in the worklist.
