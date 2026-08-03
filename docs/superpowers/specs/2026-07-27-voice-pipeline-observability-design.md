# Voice Pipeline Observability Instrumentation — Design

**Date:** 2026-07-27
**Branch:** TBD (not yet started)
**Status:** Approved design, pending implementation plan

## 1. Overview

Vera's agent worker builds one `PlanTaskAgent` per schema task at call start
(`PlanRunController.__init__`, `plan_runtime.py:195-196`) and hands control between them at
runtime via LiveKit's tool-returns-Agent / `session.update_agent()` mechanisms. None of this —
which task is active, which agent handed off to which, or why — is currently visible in
Langfuse. Separately, the LLM calls made by three different background subsystems all surface
under the same generic SDK span name, making them indistinguishable in the trace view.

This design closes both gaps by adding a small, targeted set of Vera-owned span attributes and
spans, without introducing a new instrumentation framework or changing runtime behavior.

### Goals

- Make the currently active task/agent identity visible in traces, not just in the Redis cursor.
- Make every handoff (task-complete, rule-engine-forced, IVR→verification) visible, with a
  `from`/`to`/`reason` triple, in Langfuse.
- Distinguish "task finished normally" from "a flow rule forced a jump" as an explicit, queryable
  attribute — including which rule fired.
- Distinguish which subsystem issued a given LLM call (observer extraction / health observer /
  conversation turn) so Langfuse spans stop colliding under one generic name.
- Extend the same visibility to control-plane dispatch-time decisions (schema compile, prefill
  fuse, IVR toggle), correlated into the same Langfuse trace as the call that follows.

### Non-goals (this design)

- Redacting the pre-existing LiveKit SDK spans (`llm_node`, `llm_request`, `agent_turn`,
  `eou_detection`, `user_turn`) that already attach raw transcript/chat-context text to Langfuse.
  This is a real gap (see `otel-spans-unredacted-pre-prod` — tracked separately) but is
  explicitly deferred: the app is pre-production and the team will add a PHI-redacting
  `SpanProcessor` as its own follow-on piece of work. That gap is accepted as-is for now — but
  this design must not widen it. See §6 for the hard guardrail on everything this design adds.
- Fixing `transcript_publisher.py`'s silent drop of LiveKit's native `AgentHandoff` conversation
  item. Once every handoff is instrumented at its actual application call site (richer,
  business-meaningful attributes than the SDK's generic `Agent.id`), that event stops being
  useful for observability. Left alone unless wanted for transcript/UI reasons unrelated to
  tracing.
- Tagging the main conversation-turn LLM call with a `vera.llm.purpose` attribute via an
  `Agent.llm_node()` override. Mechanically confirmed possible (`agent.py:309-341`), but it
  needs a shared mixin across `PlanTaskAgent`/`WrapUpAgent`/`IvrNavigatorAgent` (no common base
  below `Agent`). Not required to fix the reported span-collision (both examples given were
  background subsystems); once the three background sites below are tagged, an untagged
  `llm_fallback_adapter` span is identifiable as the conversation turn by elimination. Candidate
  fast-follow.

## 2. Current-state gaps (confirmed by code read, 2026-07-27)

- `PlanTaskAgent` never passes `id=` to `Agent.__init__` (`plan_runtime.py:84-94`), so every task
  instance collapses to the identical LiveKit-generated label `"plan_task_agent"`
  (`agent.py:42,63`) — the SDK's own per-agent span attribute cannot distinguish task 1 from task 3.
- Active task identity reaches only a fire-and-forget Redis cursor write
  (`note_task_entered`/`note_wrap_up_entered`, `plan_runtime.py:217-223`, `_write_cursor`,
  `plan_runtime.py:295-308`) — never a log or span.
- Neither in-app handoff site logs or traces anything: task-complete
  (`PlanTaskAgent._task_complete`, `plan_runtime.py:111-123`) and the rule-engine-forced swap
  (`PlanRunController.apply_directive_now`, `plan_runtime.py:238-265`) are both silent on the
  success path. Only the unrelated IVR→verification crossover logs a plain string
  (`ivr_agent.py:219`).
- Nothing distinguishes "task completed normally" from "a flow rule forced a jump" — the firing
  `Directive`'s identity (`rule_key`, `task_key`, `plan_runtime.py:257`) is available in hand at
  the call site and never attached to anything.
- `apply_directive_now` runs from the Observer's long-lived background tail task
  (`observer.py:389-423`), **not** inside any LiveKit-owned span — confirmed by tracing the call
  chain (`_record` → `apply_directive_now`). Tagging "current span" there would silently land on
  a no-op span.
- `vera_core/observability/otel.py` has zero span-opening code of its own anywhere in the repo —
  the only two OTel touch-points (`main.py:342,426-428`) mutate whatever span happens to be
  "current" (LiveKit's ambient `job_entrypoint` span), never open one.
- Two distinct background LLM call sites — `ResilientAnswerExtractor.extract()`
  (`observer.py:102-105`) and `CallHealthObserver._analyze_once()` (`health_observer.py:133`) —
  both call `vera_core.llm.ResilientLLM.complete()`, which wraps `FallbackAdapter` internally
  (`llm.py:163`); `FallbackAdapter`'s own span name is the literal string `"llm_fallback_adapter"`
  (`fallback_adapter.py:120`), so both call sites funnel through the identical generic SDK span
  name with no distinguishing attribute — in Langfuse they render identically regardless of which
  subsystem issued them (confirmed via screenshot during brainstorming). `coaching.py`'s
  `CoachingListener` was initially assumed to be a third such site but is not: it only injects a
  system-role chat message (`coaching.py:107-117`) for the *next* conversation turn to pick up —
  it makes no LLM call of its own, so there is nothing to tag there.
- Control plane has no span/attribute usage beyond installing the OTel exporter
  (`control_plane/main.py:327`); dispatch-time decisions that shape the runtime agent graph
  (schema compile, prefill fuse, IVR toggle) are invisible in Langfuse today, and control plane
  never calls `call_trace_attributes(room_name)` — only the agent worker does.

## 3. Decisions (settled during brainstorming)

| Decision | Choice |
|---|---|
| Scope | Agent worker runtime **and** control-plane dispatch-time spans |
| Approach | Hybrid: tag the current span + log where an SDK span is already correctly scoped; open a Vera-owned span only where none exists |
| PHI-redaction of pre-existing SDK spans | Deferred — separate future work, not blocking, not expanded here |
| `transcript_publisher.py` `AgentHandoff` drop | Left as-is — superseded by direct instrumentation at the application call sites |
| Main-turn `llm_node` purpose tagging | Deferred — candidate fast-follow, not required to fix the reported collision |
| Attribute namespace | Extends the existing `vera.*` convention (`vera.room`, `vera.tenant_id`, `vera.llm.model`) rather than inventing a new one |

## 4. Naming convention

| Attribute | Values | Set where |
|---|---|---|
| `vera.task.key`, `vera.task.index` | schema `task_key` / int | task entry |
| `vera.handoff.from_task`, `vera.handoff.to_task` | task_key or `"@wrap_up"` / `"@ivr_navigator"` sentinel | all handoff sites |
| `vera.handoff.reason` | `task_complete` \| `flow_rule` \| `ivr_live_human` | all handoff sites |
| `vera.handoff.directive_type` | `Terminate` \| `SkipToTask` \| `ReAsk` | rule-engine evaluation (see §5.5 — `ReAsk` carries no `from_task`/`to_task`, it isn't a handoff) |
| `vera.handoff.rule_key` | schema rule key (e.g. `contradiction_dob_mismatch`) | rule-engine evaluation, whenever a directive fires |
| `vera.rule_engine.fired` | bool | every rule-engine evaluation, fired or not |
| `vera.llm.purpose` | `observer_extraction` \| `health_observer` | the 2 background LLM call sites |
| `vera.dispatch.schema_version` | schema_version id | `vera.dispatch.compile_plan` |
| `vera.dispatch.form_id` | form id | `vera.dispatch.fuse_plan` |
| `vera.dispatch.task_count`, `vera.dispatch.ivr_enabled` | int / bool | `vera.dispatch.stage_call` (alongside `call_trace_attributes(room_name)`) |

`WrapUpAgent` gets `id=WRAP_UP_TASK_KEY` (reusing the `"@wrap_up"` sentinel already defined at
`plan_runtime.py:45`, so the value is identical whether read from the Redis cursor or from OTel
attributes) and `IvrNavigatorAgent` gets a new `id="@ivr_navigator"` sentinel, for the same
reason.

## 5. Components

### 5.1 `PlanTaskAgent` id fix (`plan_runtime.py:88`)

`super().__init__(instructions=..., id=self._task.task_key)`. Foundational — every downstream
handoff attribute reads `.id` off the successor/target agent object, so this must land first (or
in the same change) or `successor.id`/`target.id` stay generic.

### 5.2 Task-entry attribution (`note_task_entered`/`note_wrap_up_entered`, `plan_runtime.py:217-223`)

Called from `Agent.on_enter()` (`plan_runtime.py:96-100,143-148`), which LiveKit already wraps in
its own `on_enter` span (`agent_activity.py:587-591`) — the ambient span is already correctly
scoped to this specific agent's entry. Approach: tag the current span with
`vera.task.key`/`vera.task.index` (or the wrap-up sentinel) alongside the existing cursor write;
no new span.

### 5.3 Task-complete handoff (`_task_complete`, `plan_runtime.py:111-123`)

Runs inside LiveKit's own `function_tool` span (`@tracer.start_as_current_span("function_tool")`,
`generation.py:643-654`) — already correctly scoped per call. After `successor` resolves, tag the
current span (`vera.handoff.from_task=self._task.task_key`, `to_task=successor.id`,
`reason="task_complete"`) and add a `logger.info("handoff: %s -> %s (reason=task_complete)", ...)`
matching the existing IVR log's shape.

### 5.4 IVR→verification handoff (`ivr_agent.py:214-224`)

Same shape as 5.3, replacing the current ad-hoc string log with the structured
`vera.handoff.*` attributes (`from_task="@ivr_navigator"`, `to_task=<resolved verification
agent>.id` — whatever `verification_agent_factory` actually returned, `reason="ivr_live_human"`)
so this handoff is queryable the same way as the other two.

### 5.5 Rule-engine evaluation + forced handoff (`observer.py:419-423` → `plan_runtime.py:238-265`)

Runs from the Observer's background tail task with no ambient LiveKit span — Vera opens its own
span here: `vera.rule_engine.evaluate`, wrapping the `self._rule_engine.evaluate(...)` call and
(when it fires) the `apply_directive_now` call. Always sets `vera.rule_engine.fired` and, when a
directive fires, `directive_type` (`Terminate`/`SkipToTask`/`ReAsk`) and `rule_key` — both are
schema-authored structural identifiers (`^[a-z][a-z0-9_]*$`, same pattern as `task_key`), not
per-patient values. This is also what makes a rule that *evaluates but doesn't fire* visible for
the first time.

For `Terminate`/`SkipToTask` only, also set `vera.handoff.from_task`/`to_task` (read off
`target.id` after `_directive_target` resolves) and `reason="flow_rule"` — these are handoffs.
`ReAsk` does **not** swap the agent (`plan_runtime.py:253-256`), so it gets no `from_task`/
`to_task`/`reason`, only `directive_type="ReAsk"` + `rule_key`.

**Explicitly excluded:** `Directive.reason`/`Directive.clarify` (`directives.py:26-30`) are
schema-authored free-text (the contradiction's authored explanation, `dsl.py:426-433`) — not
per-patient data, but prose rather than the IDs/enums/counts this design otherwise sticks to.
Never attach them to a span or log line; see §6.

### 5.6 LLM-call purpose tagging (observer extraction / health_observer)

Each is Vera's own explicit call to `ResilientLLM.complete()` (not SDK-internal). Wrap each in
its own span — `vera.observer.extraction_llm_call` around `observer.py:105`,
`vera.health_observer.llm_call` around `health_observer.py:133` — with `vera.llm.purpose` set
accordingly. LiveKit's generic `llm_fallback_adapter`/`llm_request` spans continue to nest
underneath as children (unchanged); the outer Vera span is what Langfuse groups/filters on.

### 5.7 Control-plane dispatch spans (`queue_dispatcher.py`)

Corrected during plan prep: `_resolve_call_plan` (compile at `queue_dispatcher.py:632` inside
`_resolve_plan_template`, fuse at `queue_dispatcher.py:575`) runs at line 333, in the dispatch
loop, **before** the `Call` row exists — `room_name = room_name_for_call(...)` isn't computed
until line 428, later in the *same* loop iteration. `call_trace_attributes(room_name)` cannot be
attached at the compile/fuse call sites; `room_name` doesn't exist yet there. Two separate spans,
not one:

- `vera.dispatch.compile_plan` around `compile_call_plan` (`queue_dispatcher.py:632`) —
  schema-scoped and memoized per pass (one compile may serve several forms/calls), so it's tagged
  with `vera.dispatch.schema_version` (`str(schema_version.id)`) only, never a room/call
  attribute — attaching one would misattribute a shared, cross-call compile to whichever call
  happened to trigger it first.
- `vera.dispatch.fuse_plan` around `fuser.fuse(...)` (`queue_dispatcher.py:575`) — per-form, tagged
  with `vera.dispatch.form_id` (`str(form.id)`) — also pre-room-name, so also not call-correlated.
- **New: `vera.dispatch.stage_call`**, wrapping `queue_dispatcher.py:428-450` (from
  `room_name = room_name_for_call(...)` through the `create_call_room` call) — this is where
  `room_name`, `tenant_id`, and the plan are all in scope together, so this is the span that
  calls `call_trace_attributes(room_name)` and carries `vera.dispatch.ivr_enabled`
  (`bool(metadata.get("enable_ivr_navigation"))`) and `vera.dispatch.task_count`
  (`len(plan.tasks)`). Because both processes derive the same `langfuse.session.id` from the
  identical room name, *this* span — not the compile/fuse ones — is what lands in the same
  Langfuse trace as the call that follows.

## 6. PHI guardrail for new instrumentation (hard requirement)

The repo's existing rule is unconditional: never log, print, trace, or attach to a span
plaintext PHI (`vera-backend/CLAUDE.md`, enforced by a PreToolUse hook). The pre-existing
LiveKit SDK spans already violate it today (§ Non-goals) — that's accepted, tracked, and
deferred, **not** a license to add more. Every attribute or span this design adds is one of:

- a schema-authored structural identifier matching `^[a-z][a-z0-9_]*$` (`task_key`, `rule_key`)
  — fixed at schema-authoring time, never a per-call/per-patient value
- a closed enum (`vera.handoff.reason`, `vera.handoff.directive_type`, `vera.llm.purpose`)
- a boolean or count (`vera.rule_engine.fired`, `vera.dispatch.task_count`)
- an `Agent.id` (itself just a `task_key` or a fixed sentinel, per §4)

**Never**, anywhere in this design's implementation: transcript text, extracted answer values
(`ExtractedAnswer.value`, `self._answers`), DTMF digits (`press_keypad`'s `digits`), or
`Directive.reason`/`clarify` free text (§5.5). The Vera-owned wrapping spans in §5.6 carry only
`vera.llm.purpose` + task identity — never the chat context, prompt, or model output; that
content only ever appears (already, unchanged by this design) on the SDK's own nested child
spans.

Any implementation-plan step that would attach a value not on the allow-list above needs to
come back to this design for a decision, not be added ad hoc.

## 7. Error handling

Every attribute/span-setting call is wrapped so a tracing failure can never affect the call or
the dispatch path — the same principle already used for the cursor write
(`plan_runtime.py:296-297`, "a Redis blip must never delay speech"):
`try/except Exception: logger.warning(..., type(exc).__name__)`. Never a bare `except` that would
swallow `asyncio.CancelledError`; never placed before a `session.say`/`update_agent`/dispatch call
in a way that could delay it.

## 8. Testing

Assert against OTel's `InMemorySpanExporter` (a test-only `TracerProvider`) for span name +
attribute values at each of the 7 points in §5 — a real regression test for "handoff reason is
present and correct," not eyeballing Langfuse. These assertions slot into the existing tests that
already exercise `plan_runtime.py`'s handoff paths (e.g. the takeover interlock test) rather than
new test files. Include a negative assertion per §6: the recorded attribute set for each new
span, diffed against a denylist of live per-call values (current `self._answers` values,
`ExtractedAnswer.value`, transcript text, DTMF digits), must be disjoint — so a future edit that
accidentally attaches one of them fails a test, not just a review.

## 9. Open follow-ons (not part of this design's implementation plan)

- PHI-redacting `SpanProcessor` for the pre-existing SDK spans (tracked in memory as
  `otel-spans-unredacted-pre-prod`) — schedule before any real production cutover.
- `Agent.llm_node()` override for main-conversation-turn LLM purpose tagging, if full positive
  coverage is wanted later.
