# Voice Agent Runtime v3 — Phase 2: Per-task Observer, Rule engine, Answer persistence

> Companion to `2026-07-13-voice-agent-runtime-v3-call-plan-design.md`. Phase 1
> (compiler + conversational path) has landed on branch `feat/observer-agent`. This doc
> covers **only Phase 2** and **supersedes the Phase-2 sketch** in the 2026-07-13 doc where
> they differ (per-task Observer instead of one windowed Observer; rule signal via the
> pre-LLM hook instead of a background `update_agent`).

## Context

Phase 1 shipped the conversational runtime: the control plane compiles a `CallPlan` at
dispatch and the worker runs **one `PlanTaskAgent` per schema task**, chained by
`task_complete` tool-returns-Agent handoffs into a `WrapUpAgent`. The agents are
**dialogue-only** — their sole shared-state write is the `active_task_id` cursor
(`plan_runtime.py:203`). Everything for observation was scaffolded but left inert:

- `PlanRunStateService.record_answer(..., evidence_seq=...)` exists (`plan_store.py:140`) but
  **no code calls it** — nothing writes `PlanRunState.answers`.
- `PlanRunController.apply_directive` raises `NotImplementedError` (`plan_runtime.py:217`);
  the `lock` + `generation` counter and `update_answers` seam (`:213`) are in place.
- No `observer.py`, no `rule_engine.py`, no directive classes, no `field_answer` writer for
  `source='ai_call'`, no `call.answer_recorded` event or consumer handler.

Phase 2 closes that gap: while the rep talks, extract answers from the live transcript into
the DB, and let flow-rules/contradictions redirect the live call in real time — **without a
mid-turn agent-swap race** and **without losing answers across task handoffs**.

## Confirmed decisions

1. **One Observer per task** (not one windowed Observer for the whole call). Each Observer is
   bound to exactly one `PlanTask`, owns only that task's field whitelist, and is torn down at
   the task boundary — so an answer can never be written to the wrong task's field, lost, or
   delayed across a handoff. This is the direct answer to "how do we keep consistency during
   the handoff between agents."
2. **Observer active only on the conversation path.** Observers are keyed to
   `controller.active_task_index`; during IVR and wrap-up (index is `None`) no Observer runs,
   so there is no extraction/form-filling during the IVR phase.
3. **Answer value rides the event.** `CallAnswerRecordedEvent` carries the value; the
   control plane consumes it and writes the `field_answer` row. The worker→CP stream stops
   being PHI-free-by-construction (docstring relaxed + compliance note; values follow the same
   passthrough posture as `vera:transcript:*`).
4. **Rule signal via the pre-LLM `on_user_turn_completed` hook.** The async Observer never
   touches the session directly — it **stages** a directive on the controller; the active
   `PlanTaskAgent`'s `on_user_turn_completed` hook consumes and applies it before the LLM
   replies (inject a re-ask instruction, or swap the agent). This removes the "update_agent
   from a background task" race entirely, at a one-turn latency.
5. **Continuous, debounced extraction** — the active task's Observer extracts after each
   finalized rep turn (coalescing while a pass is in flight), so redirects can fire mid-task.

## Key verified facts

`PlanTask` carries `fields: list[PlanFieldDescriptor]`, `applicable_when`, intro/outro/prompt
(`call_plan.py:86`). `FlowRule{when, action:"terminate_call", skip_to_task, note}` and
`Contradiction{when, fields, reason, clarify}` are dsl models (`dsl.py:387,395`).
`conditions.evaluate(cond, values, shared)` takes a `path→raw` dict — exactly
`PlanRunStateService.get_answers()`'s shape. The transcript fan-out sink protocol is
`TurnPublisher.publish_turn(room, role, text, *, ts, source)` (`transcript_publisher.py:58`);
sinks are assembled in `main.py:434`. `field_answer` already has `source='ai_call'` in its
`AnswerSource` CHECK, an `evidence_seq` column, and the `fa_current_uq` partial-unique index
(`models/field_answer.py:50,67,83`). The CP consumer's `_dispatch` is a flat `asyncio.gather`
(`worker_events.py:179`) — needs per-room ordering once answers ride the stream.
**livekit-agents 1.5.17** exposes `Agent.on_user_turn_completed` (voice/agent.py:253),
`session.update_agent`, `session.interrupt`, `session.generate_reply(instructions=…)`.

---

## Worker side

### W1. Directive types + controller seam
**NEW** `apps/agent_worker/src/agent_worker/directives.py` — small frozen dataclasses:
`Terminate()`, `SkipToTask(task_key)`, `ReAsk(reason, clarify, fields)`. No behavior.

**MOD** `plan_runtime.py` — flesh out the existing seam (do **not** call `session.*` from a
background task):
- `update_answers` stays the Observer's push point for the applicability snapshot
  (`_next_applicable` already reads `self._answers`).
- Add `stage_directive(d)` / `take_pending_directive() -> directive | None` guarded by
  `self.lock`. The Observer/rule-engine calls `stage_directive`; the agent hook drains it.
  Keep at most one pending directive (last-writer-wins; terminate outranks skip outranks re-ask).
- Repurpose `apply_directive` as the **hook-side** applier invoked *from within the turn
  pipeline* (see W2), using `self.generation` to no-op a directive whose target task was
  already passed by an in-flight `task_complete`. Remove the `NotImplementedError`.

### W2. Pre-LLM hook on PlanTaskAgent
**MOD** `plan_runtime.py::PlanTaskAgent` — add
`async def on_user_turn_completed(self, turn_ctx, new_message)`:
1. `d = await controller.take_pending_directive()`; if none, return (normal turn).
2. `ReAsk` → inject the contradiction re-ask as turn context
   (`turn_ctx.add_message(role="system", content=f"CONSISTENCY CHECK: {reason} … {clarify}")`)
   so the **same** agent re-asks in this turn. No agent swap.
3. `SkipToTask` / `Terminate` → resolve target via the controller under `lock`+`generation`;
   `session.update_agent(target_or_wrap_up)` then `raise StopResponse()` to suppress the current
   task's reply (the new agent's `on_enter` drives the next utterance). Wrap in try/except so a
   swap failure degrades to a normal turn, never a dropped call.

*(Least-trodden LiveKit path — spike `update_agent` + `StopResponse` from inside
`on_user_turn_completed` against 1.5.17 first; fallback: stage the swap and perform it in the
agent's `llm_node` guard.)*

### W3. Per-task Observer + manager
**NEW** `apps/agent_worker/src/agent_worker/observer.py`
- `TaskObserver` — bound to one `PlanTask`:
  - Field whitelist = that task's `PlanFieldDescriptor` list; extraction may **only** emit those
    paths (drop + count anything else). This is the isolation guarantee.
  - Bounded `asyncio.Queue` of finalized turns; a debounced worker loop (coalesce while a pass
    is in flight). Each pass: `google.LLM(gemini-2.5-flash, vertexai, thinking_budget=0)`
    standalone `chat()`, strict JSON `[{field_path, value, confidence, evidence_seq}]`.
  - Per accepted answer: `run_state.record_answer(...)` (sole answers writer) →
    `controller.update_answers(run_state snapshot)` → `bus.emit(CallAnswerRecordedEvent{…value…})`
    → `rule_engine.evaluate(...)` → `controller.stage_directive(...)` on fire.
  - `evidence_seq`: replicate `transcript_finalizer._build_rows` numbering so it lines up with
    the eventual `transcript.seq` (dtmf occupies slots) — pin with a shared test.
  - `aclose()` runs a **final drain pass** over buffered turns before returning (catches the
    task's trailing turns, e.g. an answer spoken during the outro).
- `ObserverManager(TurnPublisher)` — appended to the fan-out sink list. `publish_turn` routes
  each finalized turn to the Observer for `controller.active_task_index`. On task change:
  `await prev.aclose()` **then** activate/create `next`. When `active_task_index is None`
  (IVR, wrap-up) turns are dropped — no Observer, satisfying "conversation-path only."
  Lifecycle: created in the entrypoint when a plan is active; `aclose()` all observers in
  shutdown **before** `lifecycle.ended`. Whole task body wrapped: any exception → type-only log,
  observer dies, **call continues**.

### W4. Rule engine
**NEW** `apps/agent_worker/src/agent_worker/rule_engine.py`
- After every write: for each unfired `FlowRule`, `conditions.evaluate(rule.when, answers,
  plan.shared_conditions)`; `terminate_call` → `Terminate()`, `skip_to_task` → `SkipToTask(key)`.
- `Contradiction`: re-arm when any of its `fields` change; on fire → `ReAsk(reason, clarify,
  fields)`.
- Pure/synchronous over the answers dict + plan rules; returns a directive or `None`. The
  Observer stages it; the hook applies it (never touches the session here).

### W5. Entrypoint wiring
**MOD** `apps/agent_worker/src/agent_worker/main.py`
- When `use_call_plan` and controller built: construct `WorkerEventBus`, `ObserverManager`
  (with `run_state`, `controller`, `bus`, plan), append it to `sinks` (`main.py:434`) **before**
  `_fan_out_sink`. The manager must be added **unconditionally** for plan calls (not gated on
  `publish_transcript`/`publish_events`) — observation is independent of transcript persistence.
- Shutdown (`_on_shutdown`): `await observer_manager.aclose()` in the spec-pinned order, before
  `_end_plan_run()`.

---

## Control-plane side

### C1. Answer event (value on stream)
**MOD** `packages/vera_core/src/vera_core/events/worker.py`
- Add `CallAnswerRecordedEvent {type:"call.answer_recorded", room_name, field_path, value,
  confidence, evidence_seq, ts}`; add to the `WorkerEvent` union **and** the discriminated
  `_ADAPTER` (both lists, `:56` and `:57-62`).
- **Relax the PHI-free-by-construction docstring** (`:1-8`): the stream now carries answer
  values, which follow the **same posture as `vera:transcript:*`** (tokenized by contract, raw
  today under passthrough). Add a compliance note; this is the one intentional widening of the
  event contract and must be called out for review.

### C2. Answer-writer service (extract from patient_forms)
**NEW** `packages/vera_core/src/vera_core/services/field_answers.py`
- `record_answer(session, *, tenant_id, form_id, call_id, field_path, raw_value, source,
  confidence, evidence_seq) -> bool` — extract the inline writer/`_supersede` at
  `patient_forms.py:626-652`: demote current row → `flush()` → insert new (the mandatory flush
  between demote and insert for `fa_current_uq`). **Idempotent** under at-least-once
  (equal source/call_id/value → no-op); `fa_current_uq` as the concurrency backstop for the
  worker path (no form-level `FOR UPDATE`, per the `patient_forms.py:584-585` note).
- `recompute_form_projection(session, form, doc)` — extract the inlined recompute at
  `patient_forms.py:699-717` (promoted columns via `promote_columns`, `completion_pct_v2`;
  **preserve the `flush()`-before-`refresh()` ordering** — refresh discards pending writes).
  Reused by the new handler, `resolve_disputes`, and finalize.

### C3. Consumer handler + per-room ordering
**MOD** `apps/control_plane/src/control_plane/worker_events.py`
- Register `"call.answer_recorded": self._handle_call_answer_recorded` (`:116`). Handler:
  `isinstance`-narrow → `parse_room_name` → `_retry_young_or_drop` (Call row may not be
  committed yet) → `tenant_session` → load Call → form → `field_answers.record_answer(source=
  AI_CALL, …)` → `recompute_form_projection` → `AuditRecord(FORM_AI_ANSWER, SERVICE actor,
  field name + call id, **never the value**)`.
- **Ordering fix**: `_dispatch` (`:179`) from flat `asyncio.gather` → **group-by-room,
  sequential within a room, concurrent across rooms** (answers for one call must not race each
  other or `call.ended`).

### C4. Audit + post-call recompute
- **MOD** `models/audit_log.py` — add `AuditEvent.FORM_AI_ANSWER = "form.ai_answer"` (mirrors
  `FORM_INTAKE`; names/counts only). No migration (event_type is a string column).
- **MOD** `post_call.py::resolve_ai_processing` — call `recompute_form_projection` before the
  `low_fill` decision (`:90`) so completion reflects AI answers that arrived this call.
- **Migrations: none expected** (`ai_call` CHECK, `evidence_seq`, `prompt_version_id` all
  exist). Confirm before claiming zero.

---

## Reused existing code
`PlanRunStateService.record_answer/get_answers/get_active_task` (plan_store.py) ·
`PlanRunController.lock/generation/update_answers/advance_from` (plan_runtime.py) ·
`conditions.evaluate` + dsl `FlowRule/Contradiction/Condition` · `FanOutTurnPublisher`/
`TurnPublisher` (transcript_publisher.py) · `WorkerEventBus.emit` · `_supersede`/promoted-columns/
`completion_pct_v2` (patient_forms.py, review.py, intake.py) · `parse_room_name`/
`_retry_young_or_drop`/`tenant_session` (worker_events.py) · `carry_chat_ctx` handoff.

## Ordered work items
(W1) directives + controller seam → (W2) pre-LLM hook +tests → (W3) per-task Observer + manager
+tests → (W4) rule engine +tests → (W5) entrypoint wiring → (C1) answer event → (C2)
field_answers extraction +tests → (C3) consumer handler + per-room ordering +tests →
(C4) audit member → integration + boot verify.
Then `/simplify` on the change, then `just check` (repo rule).

> **Implementation note (2026-07-15):** the C2 `patient_forms.py` refactor and the C4
> `post_call.resolve_ai_processing` recompute were **not** taken. The endpoint's inline
> recompute raises 422 on a bad promoted value (a contract the worker path must not share),
> so `recompute_form_projection` is worker-safe (logs + skips) and left the endpoint
> untouched. The post_call recompute is redundant: under the consumer's **per-room
> sequential dispatch**, each `call.answer_recorded` (stream-ordered before `call.ended`)
> already recomputed the projection, so `resolve_ai_processing` reads a fresh
> `completion_pct` with no extra query. The `AuditEvent.FORM_AI_ANSWER` member (C4) shipped.

## Tests
- Worker unit: rule engine (fire-once / re-arm / partial answers); Observer debounce / coalesce /
  queue-overflow / crash-isolation (raising LLM stub); **per-task isolation** (task-N Observer
  never writes a task-M field); **handoff drain** (trailing turn caught by `aclose` final pass);
  hook applies staged directive (ReAsk injects, Skip/Terminate swaps) and no-ops a stale-generation
  directive; `evidence_seq`↔finalizer numbering shared test.
- `tests/unit/services/test_field_answers.py`: supersede, idempotent redelivery,
  intake→ai_call→human transitions, recompute parity with old inline code.
- Integration (real Postgres): consumer handler under `fa_current_uq` redelivery + concurrent
  human resolve; young-event retry; per-room ordering; finalize recompute.

## Verification (end-to-end)
1. `just check` after each item.
2. **Boot (CLAUDE.md long-lived-loop rule)**: `just up && just migrate && just seed`, `just api`,
   `just worker`; place a Voice-Lab/console call on a seeded v2 form. Observe: no extraction during
   IVR; per-task `field_answer` rows appear with `source=ai_call` + `evidence_seq` as each task
   runs; the active task's Observer swaps at each handoff (Redis `answer:*` keys populate per task);
   form `completion_pct` rises at finalize. Idle the consumer a couple of loop windows.
3. Trigger a `flow_rule` (answer that fires terminate/skip) and a `contradiction`; observe the
   live redirect/re-ask on the **next** rep turn (one-turn latency by design).
4. Legacy paths intact: v1 schema, missing plan key → monolithic fallback; IVR-only call → no
   Observer ever starts.

## Risks / open questions
1. `update_agent` + `StopResponse` from inside `on_user_turn_completed` (1.5.17) — least-trodden
   path; spike first, fallback = swap in `llm_node` guard.
2. Trailing-answer at handoff: an answer finalized **after** the old Observer's `aclose` falls
   outside both whitelists → dropped. Mitigation: drain-before-close; fallback = one-turn grace
   overlap of the two Observers.
3. One-turn directive latency (accepted): a rule that fires on turn N redirects at turn N+1.
4. Value-on-stream widens the event PHI contract — compliance note + review sign-off required.
5. Per-task Observer cost: ≤1 flash call per rep turn per active task; debounce tunable.
6. `evidence_seq`↔`transcript.seq` parity (dtmf slots) — pin with the shared test.
