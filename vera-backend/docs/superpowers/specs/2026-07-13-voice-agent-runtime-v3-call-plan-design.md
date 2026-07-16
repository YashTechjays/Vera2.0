# Voice Agent Runtime v3 — Call Plan compiler, PlanTaskAgents, Observer

> **Amendment (2026-07-13, post-implementation): PHI wall removed.** PHI
> tokenization was dropped by decision — the worker has no PhiWallAgent, no
> seams.py, and no boundary open/close. `VeraAgent` is the main conversational
> agent class (end_call + greeting); the plan runtime's `WrapUpAgent` subclasses
> it and `PlanTaskAgent` is a plain dialogue-only Agent. References to the
> stt/tts redact/hydrate seam below are historical.

## Context

The Agent Prompt feature already compiles the Form-Schema DSL into per-task prompts
(`render_task_prompts`, `vera_core/forms/prompting.py:161`), but the live `agent_worker`
still runs a single hardcoded system prompt (`agent_worker/prompt.py:16`) — the DSL→prompt
pipeline terminates at the control-plane preview surface. `call.prompt_version_id` is never
set, and no call-time answer extraction exists (`post_call.py:18-20` documents it as a
future seam). This work closes that gap with a two-phase runtime:

- **Phase 1 — Compiler + Conversational path**: compile a **Call Plan** at dispatch
  (schema structure + question prompts fused with prompt_version session/task wording),
  transport it to the worker via Redis, and run the call as **one PlanTaskAgent per schema
  task** with sequential LiveKit tool-returns-Agent handoffs ending in wrap_up.
- **Phase 2 — Observation path**: an in-worker async **Observer** LLM extracts answers
  from the live (redacted) transcript into shared **PlanRunState.answers** and the
  `field_answer` table (source=`ai_call`, via the worker-events Redis→DB bridge), while a
  worker-side **rule engine** evaluates flow_rules/contradictions in real time and
  redirects the live conversation (terminate → wrap_up / skip ahead / re-ask).

**Confirmed decisions**: Call Plan over Redis (worker stays DB-free) · Observer in-worker
with Redis-bridge persistence · IVR navigator hands off to the first PlanTaskAgent.

**Key verified facts**: livekit-agents **1.5.17** has `session.update_agent`
(agent_session.py:1302), `session.interrupt` (:1221), `session.generate_reply(instructions=…)`
(:1155); tool-returns-Agent handoff already used at `ivr_agent.py:186-192`. Worker is
DB-free (Redis only). `conditions.evaluate` works on partial `{path: raw}` dicts.
`field_answer` has `source='ai_call'` in its CHECK + `evidence_seq` column already — the
writer just doesn't exist. `worker_events._dispatch` gathers a batch concurrently
(worker_events.py:176-178) — needs per-room ordering once answer events ride the stream.

---

## Phase 1 — Compiler + Conversational path

### 1.1 CallPlan model + pure compiler
**NEW** `packages/vera_core/src/vera_core/forms/call_plan.py` (pure, DB-free, beside `prompting.py`)

- `PlanSession` {persona, goal, base_instructions} — literal, applies to every agent.
- `PlanFieldDescriptor` {path, title, type, role, values, special_values, validation,
  required, gates, inapplicable_value} — the Observer's per-leaf answer schema; derived
  from `dsl.Leaf` + `conditions.leaf_gates`.
- `PlanTask` {task_key, title, intro, outro, prompt (compiled instruction text),
  applicable_when, fields: list[PlanFieldDescriptor]} — ordered ask/confirm leaves only.
- `CallPlan` {plan_version: "1", insurance_type, schema_name, dsl_version,
  schema_version_id, prompt_version_id | None, session, tasks, flow_rules,
  contradictions, shared_conditions, stt_key_terms} — **reuses `dsl.py` models verbatim**
  for FlowRule/Contradiction/Condition so `conditions.evaluate` works unchanged.
- `compile_call_plan(doc, prompt_doc, *, schema_version_id, prompt_version_id) -> CallPlan`
  — calls **existing `render_task_prompts`** for session + per-task text; one
  `leaf_gates(doc)` walk bucketed by the same section→task map.

**Distilled projection, not embedded FormSchemaDoc**: worker never re-compiles; UI-only /
promoted-fields / intake metadata stay out; `plan_version` gates shape drift.

**PHI**: `{{system_field}}` placeholders are NOT hydrated (raw intake values in Redis/LLM
prompt = bright-line violation). Phase 1 rewrites each token to the leaf's spoken title
("the patient's date of birth") + count-only warn. Real hydration = future PHI-vault seam.

### 1.2 Redis stores
**NEW** `packages/vera_core/src/vera_core/plan_store.py` (follow `transcript.py`/`call_stream.py`
conventions: key helper, Protocol, InMemory + Redis impls, Service wrapper)

- `vera:call-plan:{room_name}` — plain string key (SET JSON + EXPIRE); immutable blob,
  written once read once. `CallPlanService.put/get/clear` (get is tolerant: parse fail →
  None + type-only log).
- `vera:plan-run:{room_name}` — Redis **hash**: `active_task_id` (agent-owned) +
  `answer:{field_path}` = `{"value", "confidence", "evidence_seq", "ts"}` (Observer-owned).
  Disjoint writers, per-field atomic. Rolling EXPIRE per write.
- `PlanRunStateService.set_active_task / get_active_task / record_answer / get_answers /
  clear`. New setting `call_plan_ttl_seconds = 14_400` (transcript's 3600 too tight).
- Docstring: `answer:*` values carry what the transcript carries (tokenized by contract,
  raw today under passthrough) — same posture as `vera:transcript:*`; never logged.

### 1.3 Dispatch-time compile + lineage
**MOD** `packages/vera_core/src/vera_core/services/queue_dispatcher.py` (`try_dispatch:75`,
metadata build `:271-304`) + plumbing (`control_plane/dispatch.py`, lifespan, worker_events
consumer's `run_dispatch_pass` call).

Inside the per-form savepoint after the Call row flush:
1. Load pinned `SchemaVersion.schema_json`; `not is_v2(...)` → skip (legacy path, no flag).
2. Resolve PUBLISHED `PromptVersion` where `schema_version_id == form.schema_version_id`
   (newest if multiple families). Found → set `call.prompt_version_id`, parse
   `PromptDocument`. None → `prompt_doc=None` (FACTORY_SESSION fallback), lineage NULL.
3. `compile_call_plan(...)` → `plan_service.put(room_name, plan)` →
   `metadata["use_call_plan"] = True` (explicit flag, mirrors `enable_ivr_navigation`).
4. Any compile/store failure → type-only log, no flag, dispatch proceeds → legacy agent
   (fail-open to legacy keeps calls flowing). Voice Lab untouched.

### 1.4 Worker runtime
**MOD** `apps/agent_worker/src/agent_worker/agent.py` — extract VeraAgent's PHI-wall node
overrides (`agent.py:65-89`) into a `PhiWallAgent(Agent)` base (behavior-preserving;
existing `test_seams.py`/`test_agent.py` cover it). `build_agent` gains optional
`controller` param.

**NEW** `apps/agent_worker/src/agent_worker/plan_runtime.py`
- `PlanTaskAgent(PhiWallAgent)`: instructions = session block + `## Current task: {title}`
  + task prompt, assembled at construction. `on_enter`: fire-and-forget cursor write
  (`set_active_task`; failure never blocks speech) then `say(intro)` (first task:
  `greeting or intro`). **Single tool `task_complete()`** → say `outro` → `return
  controller.next_agent(after=i)`. No field paths, no write tools. Session-level cascade
  turn_handling (interruption.mode="vad" stays pinned via `cascade.py`).
- `WrapUpAgent(PhiWallAgent)`: closing directive; `end_call` tool → `session.shutdown(drain=True)`.
- `PlanRunController`: pre-builds **all** agents in `__init__` (validates whole plan before
  the call). `first_agent()`, `next_agent(after)` (skips non-applicable tasks via
  `conditions.evaluate` on `applicable_when` — vacuous in Phase 1, live in Phase 2).
  **Phase-2 seam designed now**: `apply_directive(Terminate|SkipToTask|ReAsk)` +
  asyncio.Lock + handoff-generation counter (no double-swap); Phase 1 body = logged no-op.

**MOD** `ivr_agent.py` — ctor gains `verification_agent_factory: Callable[[], Agent] | None`
(defaults to today's VeraAgent partial at `:117`); `transfer_to_verification` unchanged.

### 1.5 Entrypoint wiring
**MOD** `main.py` entrypoint: if `meta.get("use_call_plan")` → own Redis client →
`CallPlanService.get(room_name)`; missing/unparseable → warn + legacy fallback. Build
`PlanRunStateService` + `PlanRunController`; pass `plan.stt_key_terms` into
`build_session` (cascade gains optional `key_terms` → `deepgram.STTv2(keyterms=...)`).
Shutdown: best-effort `run_state.clear` + `plan_service.clear` + close client.

### 1.6 Phase 1 tests
- `tests/unit/forms/test_call_plan.py` — ordering matches document order; gates ==
  `leaf_gates`; prompt text byte-equal to `render_task_prompts`; FACTORY fallback;
  placeholder neutralization; JSON round-trip.
- Worker unit tests — controller next/skip, instructions fusion, cursor write,
  `task_complete` successor chain, final → WrapUpAgent, PHI nodes present.
- Dispatcher tests — prompt_version_id set; v1 schema → no flag; fail-open.
- Boot verification (`just up && just worker`) + a real console call.

---

## Phase 2 — Observation path

### 2.1 Answer event
**MOD** `packages/vera_core/src/vera_core/events/worker.py` — add
`CallAnswerRecordedEvent {type: "call.answer_recorded", room_name, field_path, value,
confidence, evidence_seq, ts}` to the WorkerEvent union. Same stream (answer events
stream-ordered before the producer's `call.ended`). Update module docstring: stream now
carries tokenized answer values (same posture as transcript keys).

### 2.2 Observer
**NEW** `apps/agent_worker/src/agent_worker/observer.py`
- **Subscription = in-process fan-out**: `ObserverTurnSink` implements `TurnPublisher`
  (transcript_publisher.py:59), appended to the sinks list — turns are already finalized,
  ordered, post-redaction. Non-blocking bounded `asyncio.Queue` (drop-oldest + count-only
  warn).
- **Seq stamping** replicates `transcript_finalizer._build_rows` numbering so
  `evidence_seq` == eventual `transcript.seq` (pin with a shared test; dtmf occupies slots).
- **Lifecycle**: started in entrypoint when plan active; `aclose()` (cancel + drain +
  final pass) in shutdown **before** `lifecycle.ended`. Task body fully wrapped: any
  exception → type-only log, observer dies, **call continues**.
- **Windowing**: descriptors of tasks (n-1, n, n+1); cursor read in-process from the
  controller (Redis stays the cross-process truth).
- **Debounce**: one extraction pass per rep turn, coalescing while a pass is in flight.
- **LLM**: `google.LLM(gemini-2.5-flash, vertexai, thinking_budget=0)` standalone
  `chat(chat_ctx=...)`; strict JSON out `[{field_path, value, confidence, evidence_seq}]`;
  field_path whitelist = window descriptors; bad JSON → skip pass.
- **record_answer**: skip unchanged → `run_state.record_answer` (sole answers writer) →
  `bus.emit(CallAnswerRecordedEvent)` → feed rule engine.

### 2.3 Rule engine + live redirect
**NEW** `apps/agent_worker/src/agent_worker/rule_engine.py`
- After every write: for each unfired rule, `conditions.evaluate(rule.when, answers,
  plan.shared_conditions)`. Contradictions re-arm when any of their `fields` change.
- On fire → `controller.apply_directive(...)` (Phase 1 seam), using verified 1.5.17 APIs:
  - terminate → `session.interrupt()` + `session.update_agent(wrap_up_agent)`
  - skip_to_task → `session.interrupt()` + `session.update_agent(agents[target])`
  - contradiction → `session.generate_reply(instructions=f"CONSISTENCY CHECK: {reason}…{clarify}")`
    (one-shot re-ask, no instruction rewrite)
- Lock + generation counter serialize directives against in-flight `task_complete` handoffs.

### 2.4 Consumer-side persistence
**NEW** `packages/vera_core/src/vera_core/services/field_answers.py` — extract the answer
writer inlined at `patient_forms.py:184-203` (intake) / `:592-618` (human):
`record_answer(session, *, tenant_id, form_id, call_id, field_path, raw_value, source,
confidence, evidence_seq) -> bool` — supersede current row (`_supersede` pattern),
**idempotent** under at-least-once (equal source/call_id/value → no-op), `fa_current_uq`
as backstop, form row FOR UPDATE per batch. Also extract
`recompute_form_projection(...)` from `patient_forms.py:655-686` (promoted columns +
completion_pct), reused by resolve_disputes and finalize.

**MOD** `apps/control_plane/src/control_plane/worker_events.py`
- Handler for `call.answer_recorded`: `parse_room_name` → `_retry_young_or_drop` →
  `tenant_session` → Call → form → `record_answer(source=AI_CALL)` → AuditRecord (field
  name + call id, never the value).
- **Ordering fix**: `_dispatch` (`:176-178`) changes from flat `asyncio.gather` to
  group-by-room, sequential within room, concurrent across rooms.
- `post_call.resolve_ai_processing`: call `recompute_form_projection` before the low_fill
  decision (completion can now rise between calls; flipping `auto_retry_enabled` stays a
  separate ops decision).

**Migrations: none expected** (`prompt_version_id`, `evidence_seq`, `ai_call` CHECK member
all exist). Verify AuditEvent has a suitable member before claiming zero.

### 2.5 Phase 2 tests
- Worker unit: rule engine (fire-once/re-arm/partial answers), windowing edges, observer
  debounce/coalesce/overflow/crash-isolation (raising LLM stub), directive vs concurrent
  task_complete race.
- `tests/unit/services/test_field_answers.py`: supersede, idempotent redelivery,
  intake→ai_call→human transitions, recompute parity with old inline code.
- Integration (real Postgres): consumer handler — fa_current_uq under redelivery +
  concurrent human resolve; young-event retry; finalize recompute.
- **CLAUDE.md long-lived-loop rule**: Observer task + modified consumer verified by
  BOOTING (`just up`, `just worker`, `just api`) and idling several loop windows.

---

## Ordered work items

**Phase 1**: (1) `forms/call_plan.py` +tests → (2) `plan_store.py` + setting +tests →
(3) dispatcher lineage/plan/flag + plumbing +tests → (4) PhiWallAgent extraction →
(5) `plan_runtime.py` +tests → (6) ivr factory param → (7) `main.py` wiring + boot verify.

**Phase 2**: (8) answer event → (9) observer → (10) rule engine + directives →
(11) `field_answers.py` extraction (+ patient_forms refactor) → (12) consumer handler +
per-room ordering → (13) post_call recompute → (14) integration tests + boot verify.

Each phase ends with: `/simplify` on the change, then `just check` (repo rule).

## Reused existing code

`render_task_prompts` + `FACTORY_SESSION` (prompting.py) · `conditions.evaluate/leaf_gates/
is_v2` · dsl `FlowRule/Contradiction/Condition/Leaf/Validation` models · tool-returns-Agent
handoff (ivr_agent.py:186) · `TurnPublisher`/fan-out (transcript_publisher.py) · transcript/
call_stream Redis store patterns · `WorkerEventBus`/`WorkerEventConsumer` · `unwrap_value`/
`promote_columns`/`completion_pct_v2` · `_supersede` pattern (patient_forms.py).

## Verification (end-to-end)

1. `just check` after each work item; freshness/round-trip suites stay green.
2. Boot: `just up && just migrate && just seed`, `just api`, `just worker`; place a Voice
   Lab / console call against a seeded v2 form: observe task-by-task handoffs (Task 1 →
   … → wrap_up), intros/outros spoken, `vera:plan-run:*` cursor moving in Redis.
3. Phase 2: during the call, watch `field_answer` rows appear with source=ai_call +
   evidence_seq; trigger a flow rule (answer that fires terminate/skip) and observe the
   live redirect; confirm form completion_pct rises at finalize.
4. Legacy paths still work: v1-schema form, voice-lab room, missing plan key → monolithic
   VeraAgent fallback.

## Risks / open questions

1. Placeholder hydration deferred (spoken titles in Phase 1) — needs explicit acknowledgment.
2. `say(outro)` drain semantics before tool-returned handoff — small spike; fallback:
   fold predecessor outro into successor `on_enter`.
3. `update_agent` from observer task alongside tool handoff — least-trodden LiveKit path;
   dedicated race test + live verification.
4. evidence_seq ↔ transcript_finalizer numbering parity (dtmf slots) — pin with shared test.
5. Worker-events stream now carries tokenized answer values — docstring + compliance note;
   fallback: dedicated answer stream (loses free ordering vs call.ended).
6. `try_dispatch` signature ripple across dispatcher tests — mechanical but wide.
7. Observer cost: ≤1 flash call per rep turn; debounce tunable via settings.
