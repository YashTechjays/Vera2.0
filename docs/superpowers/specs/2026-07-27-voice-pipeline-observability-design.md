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
  coaching / conversation turn) so Langfuse spans stop colliding under one generic name.
- Extend the same visibility to control-plane dispatch-time decisions (schema compile, prefill
  fuse, IVR toggle), correlated into the same Langfuse trace as the call that follows.

### Non-goals (this design)

- Redacting the pre-existing LiveKit SDK spans (`llm_node`, `llm_request`, `agent_turn`,
  `eou_detection`, `user_turn`) that already attach raw transcript/chat-context text to Langfuse.
  This is a real gap (see `otel-spans-unredacted-pre-prod` — tracked separately) but is
  explicitly deferred: the app is pre-production and the team will add a PHI-redacting
  `SpanProcessor` as its own follow-on piece of work. Nothing in this design widens that gap —
  every new attribute added here is metadata (keys, enums, booleans, counts), never content.
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
- Three distinct LLM call sites — observer extraction, health-observer analysis, and coaching —
  all funnel through LiveKit's generic `llm_fallback_adapter`/`llm_request`/`llm_request_run`
  span names with no distinguishing attribute, so in Langfuse they render identically regardless
  of which subsystem issued them (confirmed via screenshot during brainstorming).
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
| `vera.handoff.directive_type` | `Terminate` \| `SkipToTask` | rule-engine handoff only |
| `vera.handoff.rule_key` | schema rule key | rule-engine handoff, `SkipToTask` only |
| `vera.rule_engine.fired` | bool | every rule-engine evaluation, fired or not |
| `vera.llm.purpose` | `observer_extraction` \| `health_observer` \| `coaching` | the 3 background LLM call sites |
| `vera.dispatch.schema_version`, `vera.dispatch.task_count`, `vera.dispatch.ivr_enabled` | schema version / int / bool | control-plane dispatch spans |

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
(when it fires) the `apply_directive_now` call. Always sets `vera.rule_engine.fired`; when true,
also sets `vera.handoff.from_task`, `to_task` (read off `target.id` after `_directive_target`
resolves), `reason="flow_rule"`, `directive_type`, and `rule_key` (for a `SkipToTask`). This is
also what makes a rule that *evaluates but doesn't fire* visible for the first time.

### 5.6 LLM-call purpose tagging (observer extraction / health_observer / coaching)

Each is Vera's own explicit LLM call site (not SDK-internal). Wrap each in its own span —
`vera.observer.extraction_llm_call`, `vera.health_observer.llm_call`, `vera.coaching.llm_call` —
with `vera.llm.purpose` set accordingly. LiveKit's generic `llm_fallback_adapter`/`llm_request`
spans continue to nest underneath as children; the outer Vera span is what Langfuse groups/filters
on.

### 5.7 Control-plane dispatch spans (`queue_dispatcher.py:575,632`)

Wrap `compile_call_plan` and `PrefillFuser.fuse` in `vera.dispatch.compile_plan` /
`vera.dispatch.fuse_plan` spans, and call `call_trace_attributes(room_name)` on the outer dispatch
span (control plane never calls this today — only the agent worker does). Because both processes
derive the same `langfuse.session.id` from the identical room name, this lands the dispatch-time
spans in the *same* Langfuse trace as the call that follows, giving end-to-end visibility from
schema compile through hangup.

## 6. Error handling

Every attribute/span-setting call is wrapped so a tracing failure can never affect the call or
the dispatch path — the same principle already used for the cursor write
(`plan_runtime.py:296-297`, "a Redis blip must never delay speech"):
`try/except Exception: logger.warning(..., type(exc).__name__)`. Never a bare `except` that would
swallow `asyncio.CancelledError`; never placed before a `session.say`/`update_agent`/dispatch call
in a way that could delay it.

## 7. Testing

Assert against OTel's `InMemorySpanExporter` (a test-only `TracerProvider`) for span name +
attribute values at each of the 7 points in §5 — a real regression test for "handoff reason is
present and correct," not eyeballing Langfuse. These assertions slot into the existing tests that
already exercise `plan_runtime.py`'s handoff paths (e.g. the takeover interlock test) rather than
new test files.

## 8. Open follow-ons (not part of this design's implementation plan)

- PHI-redacting `SpanProcessor` for the pre-existing SDK spans (tracked in memory as
  `otel-spans-unredacted-pre-prod`) — schedule before any real production cutover.
- `Agent.llm_node()` override for main-conversation-turn LLM purpose tagging, if full positive
  coverage is wanted later.
