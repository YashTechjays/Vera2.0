# Call Health Observer — Design

**Date:** 2026-07-17
**Branch:** `feat/call-health-observer-agent`
**Status:** Approved design, pending implementation plan

## 1. Overview

A per-call observer that runs concurrently with the voice cascade and **never blocks it**.
It periodically analyzes the conversation-so-far with an LLM, produces a health score
(0–100) and an intervention flag, persists the results for reporting, and pushes them to
the frontend in realtime so Live Monitoring shows at a glance which active calls need a
supervisor.

Once a supervisor intervenes (takeover), the observer stops permanently for that call —
its purpose is fulfilled.

### Goals

- Continuous per-call health score + intervention flag, visible live in the UI.
- Realtime "intervention needed" notifications to the right users.
- Durable, report-friendly data: % of calls needing intervention, category breakdown,
  flagged-vs-actually-intervened.
- Zero impact on the audio pipeline; graceful degradation when LLMs fail.

### Non-goals (v1)

- Notification persistence / inbox (notifications are ephemeral push).
- Per-analysis score history in the domain DB (observability/Langfuse is the home for that).
- Automated actions from the flag (the observer informs humans; it never acts on the call).

## 2. Decisions (settled during brainstorming)

| Decision | Choice |
|---|---|
| Run location | Per-call asyncio task inside the **agent worker** job |
| Trigger | **User-turn driven** (bot spoke, user replied) + min interval, one in flight |
| LLM | `ResilientLLM` — primary `google:gemini-3.1-flash-lite`, fallback `openai:gpt-5.4-mini` |
| Flag vocabulary | Existing `InterventionCategory` values + `supervisor_requested` + `none` |
| Persistence | `CallEvent(HEALTH)` **on flag transitions only** + denormalized `Call` columns updated every analysis + `ACTIVE↔CRITICAL` status flips |
| Realtime | New `health` envelope on the per-call stream **and** a new login-session, user-scoped notification SSE |
| Notification audience | Unpublished call → owner only; published call → tenant-wide (mirrors owner-or-published read visibility) |
| Scope | Backend + frontend |
| Token cost | Rely on provider **implicit prompt caching**; prompt built prefix-stable; chunked re-anchoring window |

## 3. Architecture

```
agent worker (per call job)                     control plane                      frontend
┌──────────────────────────────┐   Redis    ┌──────────────────────────┐   SSE  ┌──────────────────┐
│ FanOutTurnPublisher          │  streams   │ WorkerEventConsumer      │        │ NotificationsPro- │
│  ├─ transcript publisher     │            │  _handle_call_health:    │        │ vider (app root,  │
│  ├─ CallStreamService sink ──┼─ percall ─►│  - idempotency guard     │        │ login session)    │
│  └─ CallHealthObserver       │  stream    │  - Call.health_* update  │        ├──────────────────┤
│      │ user turn + cooldown  │  (TYPE_    │  - transition detection  │        │ LiveMonitoring    │
│      ▼                       │  HEALTH)   │  - CallEvent(HEALTH)     │        │  health badges,   │
│  ResilientLLM.complete()     │            │  - ACTIVE↔CRITICAL flip  │        │  Critical tab     │
│  (gemini-3.1-flash-lite →    │  worker    │  - NotificationService ──┼─ SSE ─►│                   │
│   gpt-5.4-mini)              │─ events ──►│    (transitions only)    │        │ LiveCallModal     │
│  stops on takeover latch     │ call.health│                          │        │  live health      │
└──────────────────────────────┘            └──────────────────────────┘        └──────────────────┘
```

The worker never writes the DB (existing invariant): analysis results travel over the
existing worker→control-plane Redis event stream and are persisted by `WorkerEventConsumer`.

## 4. Components

### 4.1 `CallHealthObserver` (new: `apps/agent_worker/src/agent_worker/health_observer.py`)

Attached in `entrypoint()` **only when `publish_events` dispatch metadata is set** (real
`/calls` flow; voice-lab test calls are skipped).

- **Feed:** implements the `TurnPublisher` protocol
  (`transcript_publisher.py:58`) and is registered as an extra sink in the existing
  `FanOutTurnPublisher` (`main.py:438`), receiving the same ordered,
  barge-in-corrected turn stream as the other sinks. Turns accumulate in memory.
- **Arming gate:** no analysis until ≥ `health_min_user_turns` (default 2) user turns
  exist.
- **Trigger:** a completed user turn requests an analysis. It runs immediately if none is
  in flight and ≥ `health_min_interval_seconds` (default 15.0) elapsed since the last
  run; otherwise exactly one deferred run is scheduled for when the cooldown expires.
  Silence triggers nothing.
- **Analysis:** one `ResilientLLM.complete(system=..., user=...)` call. The worker builds
  its own process-level `ResilientLLM` from the new health settings (same
  `provider:model` selector format as the summary chain).
- **Stop conditions:**
  - Takeover latch (`TakeoverState.engaged`) checked before *starting* a run **and**
    re-checked before *emitting* a completed result → stops permanently.
  - Cancelled in `_on_shutdown` when the call ends; re-checks "still running" before
    emitting, so late results are discarded.
- **Failure isolation:** `LLMUnavailableError` / parse failure → log warning with
  exception **type name only** (PHI-safe rule) and skip the cycle. The cascade never
  notices the observer exists.
- **Emission (per assessable analysis):**
  1. `CallStreamService` envelope, new `TYPE_HEALTH = "health"` — rides the existing
     `GET /calls/{id}/events` SSE.
  2. `call.health` worker event (new event type in `vera_core/events/`) with payload
     `{score, flag, reason, turn_count, analyzed_at}`.

### 4.2 LLM contract

**System prompt (static, byte-identical for every analysis of every call):**

> Given the ongoing conversation transcription so far, analyse whether the call can be
> completed fully by the bot agent. Give a health score 0–100 and categorize whether a
> supervisor intervention is needed or the bot can continue and finish the call itself.
> Early automated IVR/menu navigation is normal and must never be flagged as a loop.
> If the conversation so far is insufficient to judge, return `assessable: false` —
> never guess a low score to express uncertainty. Do not converse. Output only JSON.

**Output contract (parsed with Pydantic, mirroring `call_summary.parse_sections`):**

```json
{ "assessable": false }
{ "assessable": true, "call_health_score": 78, "intervention_flag": "none", "reason": "..." }
```

- `intervention_flag` ∈ `InterventionCategory` values (`repeated_questions`,
  `hallucination`, `conversation_loop`, `long_silence`, `off_script`, `low_confidence`,
  `other`) ∪ {`supervisor_requested`, `none`}.
- Lenient parsing: strip markdown fences; clamp score to 0–100; unknown flag → `other`;
  `assessable: true` with a missing/invalid score → treated as unassessable.
- **Unassessable result = complete no-op**: no columns updated, no event, no flip, no
  notification. A low score always means "the call is going badly", never "I don't know".

**Prompt construction rules (prefix caching):**

- User message = deterministically formatted diarized transcript (append-only rendering:
  a turn, once formatted, never changes) followed by a short trailing instruction. All
  dynamic content goes **after** the transcript.
- **Chunked re-anchoring window** instead of a per-request sliding window: transcript
  grows to `health_max_turns` (default 60), then truncates once to the newest 40 and
  grows back. Prefix stays byte-identical between re-anchors → Vertex Gemini implicit
  caching (~75% cached-token discount) and OpenAI automatic caching (~50%) stay hot.
- No explicit cache APIs (storage cost + lifecycle for a few-KB prompt — YAGNI).
- Test: `format(turns[0:k])` must be a strict string prefix of `format(turns[0:k+n])`
  while no re-anchor occurred.

### 4.3 Persistence (`WorkerEventConsumer._handle_call_health`, control plane)

Per `call.health` event, in order:

1. **Idempotency / staleness guard:** drop if `analyzed_at` ≤ `Call.health_analyzed_at`
   (protects against consumer-group redelivery and out-of-order duplicates).
2. **Terminal guard:** drop if `Call.current_status` is terminal (late result after
   `call.ended`).
3. **Intervener guard:** drop if `Call.intervener_user_id` is set (late result after
   takeover).
4. **Update `Call`** (every surviving analysis): `health_score`, `health_flag`,
   `health_reason` (the analyzer's one-line justification, VARCHAR(500), truncated on
   write as defense in depth — 2026-07-18 amendment: shown as the health tooltip in
   Live Monitoring, disclosed on list rows and audited alongside `patient_name`),
   `health_analyzed_at` — new nullable columns, idempotent migration. **Never cleared at
   closeout** (last-known state feeds reports).
5. **Episode state machine** with asymmetric hysteresis. State is fully encoded by
   existing/planned fields — no extra counter column:
   - *prior flag* = `Call.health_flag` before this update (the previous analysis's flag);
   - *in episode* = `current_status == CRITICAL`;
   - *episode category* = `event_value` of the call's most recent HEALTH `CallEvent`
     (one cheap indexed lookup, only when a transition is suspected).

   Rules (evaluated before step 4's column update):
   - **Not in episode, incoming flagged → open episode (immediate):** append
     `CallEvent(event_type=HEALTH, event_value=<flag>, detail={score, reason,
     turn_count})`; flip **any non-terminal, non-`CRITICAL` status → `CRITICAL`** (+
     STATUS `CallEvent`, existing pattern) — not conditioned on `current_status ==
     ACTIVE`, so a `call.health` event that races `call.answered`'s commit (still
     `INITIATED`/`RINGING`/`IVR`/`WAITING`) still opens the episode instead of being
     silently dropped (amendment, 2026-07-17; `_handle_call_answered` already treats
     `CRITICAL` as "already live" and skips its own `ACTIVE` flip in that case);
     publish notification.
   - **In episode, incoming flag ≠ episode category (and ≠ none) → category change:**
     append HEALTH event with the new category; status stays CRITICAL; publish
     notification. (Covers changes across a healthy blip too, since comparison is
     against the episode category, not the blip.)
   - **In episode, incoming = episode category:** re-confirmation (including
     re-escalation after a single healthy blip) — no event, no notification.
   - **In episode, incoming none:** recovery requires **2 consecutive healthy results** —
     i.e. close only if *prior flag* is also `none`. First healthy result just updates
     the columns; the second appends `CallEvent(HEALTH, event_value="none")`, flips
     `CRITICAL → ACTIVE`, no notification.
6. Transitions are only computed **between assessable results** — cold-start noise cannot
   create phantom episodes.

`CallEvent(HEALTH)` rows therefore exist **only at flag changes**: every row is a
transition by definition; a healthy 10-minute call writes zero HEALTH rows.

### 4.4 Notification channel (new)

- **`NotificationService`** (new: `packages/vera_core/src/vera_core/notifications.py`):
  per-tenant capped Redis stream `notify:{tenant_id}` (maxlen, ephemeral). Envelope:
  `{type: "intervention_needed", audience: {kind: "user", user_id} | {kind: "tenant"},
  data: {call_id, score, flag}, ts}`.
  (Minimum-necessary, 2026-07-18 final-review amendment: `reason` was dropped from
  `data` — no consumer reads it; it stays in `CallEvent.detail` for reporting.)
- **Publisher:** `WorkerEventConsumer`, on escalation transitions only (never on
  re-confirmations). Audience from the Call row at flag time: unpublished → owner only;
  published → tenant-wide.
- **Endpoint:** `GET /api/v1/notifications/stream` (SSE, authenticated). Tails the
  caller's tenant stream from "now"; forwards an event iff it is addressed to that user,
  or is tenant-wide and the user holds `calls:read`. Uses the existing
  `_sse_frame`/`frames_with_keepalive` utilities.
- **Scale:** an idle SSE connection ≈ socket + parked coroutine (~50–100 KB); 300
  concurrent users ≈ ~30 MB, negligible CPU. Each connection does its own Redis tail —
  no in-process broadcaster needed yet (YAGNI; noted as a later optimization).

### 4.5 API surface changes

- `GET /calls` (list): responses gain `health_score`, `health_flag`,
  `health_analyzed_at`.
- `GET /calls/{id}/events` (existing per-call SSE): new `health` envelope type flows
  through unchanged plumbing. The terminal-call replay branch does **not** replay health
  frames (final state lives on the Call row).
- `GET /api/v1/notifications/stream`: new login-session SSE described above.

### 4.6 Frontend

- **`lib/api/notifications.ts`** — `streamNotifications()` mirroring the
  `callEvents.ts` reconnecting-fetch pattern (Authorization header, self-reconnect).
- **`NotificationsProvider`** at app root: opens the stream at login, keeps it for the
  session. On `intervention_needed`: toast + refresh/patch the calls list query. On
  reconnect: refetch the calls list (SSE is an accelerant; `GET /calls` is the source of
  truth).
- **Notification bell** (2026-07-18 amendment): the topbar bell carries an unread badge
  and a popover inbox of intervention alerts (newest-first, capped at 50, session-scoped
  memory). Closing the panel marks all read; "Clear" empties the inbox. Read state is a
  single cursor (the newest-read SSE entry id) persisted in sessionStorage — opaque
  stream ids only, no PHI — which also makes the replay window idempotent for the user:
  read alerts never re-toast or re-count as unread across reloads.
- **Non-PHI call disambiguation + click-through** (2026-07-19 amendment): the
  notification payload was never given patient PHI — deliberately, since a toast/bell
  entry is ambient (on-screen without the user asking, unlike a page they opened) — but
  with several concurrently-flagged calls a bare flag+score line couldn't tell them
  apart. Fix: `shortCallRef()` renders a short, non-PHI fragment of the call's opaque id
  (e.g. `#B06E57` — last 6 hex chars of the UUID) alongside the flag/score in both the
  toast and the bell row, purely a disambiguation hint, never a lookup key. Clicking a
  bell item (or the toast's "View" action) navigates to Live Monitoring with the
  `callId` in React Router **state** (never the URL/query string — `PHI-never-in-URLs`
  is unaffected since router state isn't part of the URL, and `callId` isn't PHI on its
  own) and opens that exact call's modal once it appears in the polled list; state is
  cleared via a replace-navigation as soon as it's handled, so a manual refresh never
  re-opens it. If the call has since ended/left the visible list, the page surfaces
  "That call is no longer active." instead of a silent no-op or a wrongly-opened modal.
- **Live Monitoring page:** color-coded health badge + flag label on call cards from the
  new list fields. `NULL` score renders a neutral "Assessing…" badge — never 0, never
  red. Stale data (`health_analyzed_at` older than ~3× the analysis interval) grays out
  with "last assessed Xs ago". The **Critical tab lights up with zero categorizer
  changes** (it already buckets `CallStatus.CRITICAL`; the observer is the missing
  producer).
- **`callEvents.ts`:** new `asCallHealth` narrower; **LiveCallModal** shows a live health
  indicator from `health` frames.

### 4.7 Settings (new, env prefix `VERA_`)

| Setting | Default |
|---|---|
| `health_primary_model` | `google:gemini-3.1-flash-lite` |
| `health_fallback_models` | `["openai:gpt-5.4-mini"]` |
| `health_min_interval_seconds` | `15.0` |
| `health_min_user_turns` | `2` |
| `health_max_turns` | `60` (re-anchor to newest 40) |
| `health_attempt_timeout_seconds` | `8.0` |

(Vertex location: `_build_google` already forces `location="global"` —
`gemini-3.1-flash-lite` is not in `us-central1`.)

## 5. Edge cases (resolved)

| # | Case | Resolution |
|---|---|---|
| 1 | Analysis completes after call ended | Worker re-checks before emit; consumer terminal guard drops it |
| 2 | Analysis completes after takeover | Worker re-checks latch before emit; consumer intervener guard |
| 3 | Consumer redelivery / duplicates | `analyzed_at` idempotency guard |
| 4 | Flag flapping | Escalate immediately; recover only after 2 consecutive healthy results |
| 5 | Category change while flagged | New event + notification; status stays CRITICAL |
| 6 | Call ends while CRITICAL | Closeout/sweeper must treat CRITICAL as active — explicit tests (first real producer of this status) |
| 7 | Unbounded transcript cost | `health_max_turns` chunked re-anchoring window |
| 8 | IVR menu loops look like `conversation_loop` | Prompt clause + min-user-turns gate |
| 9 | Both LLM providers down | Cycles skip; data goes stale, not wrong; UI grays out via `health_analyzed_at` |
| 10 | Notification SSE reconnect gap | Server tails from a 60s replay window (not "now"); frontend dedupes by SSE entry id, so a reload/reconnect re-delivers rather than misses (2026-07-18 amendment). List refetch remains the state backstop. Filtered-out events also emit keepalive bytes so foreign-traffic bursts can't starve proxy timeouts |
| 11 | Cold start / too few turns | `health_min_user_turns` gate + `assessable: false` no-op + NULL-renders-neutral |
| 12 | Call published after flag raised | Tenant users see it via Critical tab on next poll (accepted for v1) |
| 13 | Listen-only supervisor joins | Observer keeps running (only the intervene latch stops it) |
| 14 | Rep hangs up immediately | Call ends with health NULL = "never assessed"; reports count separately |

## 6. Reporting enabled by this schema

- **% of calls needing intervention:** `COUNT(DISTINCT call_id)` over
  `call_event WHERE event_type='HEALTH' AND event_value != 'none'` ÷ calls in period;
  or last-known via `Call.health_flag`.
- **Category breakdown:** `GROUP BY event_value` on HEALTH rows (typed column, no JSONB
  extraction). Episodes = rows where `event_value != 'none'`.
- **Flagged vs actually intervened (observer precision/recall):** join HEALTH events
  with `InterventionEvent` (type `TAKEOVER`) per call.
- **Never-assessed calls:** `Call.health_score IS NULL`.

## 7. PHI & security

- Transcript goes only to BAA-covered LLMs (Vertex Gemini, OpenAI) via `vera_core.llm` —
  consistent with the 2026-07-08 decision (raw PHI allowed in the live pipeline;
  tokenize only logs/traces).
- LLM `reason` text may contain PHI: it lives in `CallEvent.detail` JSONB and in
  notification payloads delivered only to users already authorized to read that call
  (owner, or tenant users for published calls) — same audience rule as
  `_authorize_call_read`.
- All observer/consumer exception logging is type-name-only (no reprs/tracebacks around
  PHI I/O).
- Notification SSE is authenticated and tenant-scoped; audience filtering happens
  server-side.

## 8. Testing

- **Observer unit tests** (fake clock + fake LLM): user-turn trigger, cooldown, single
  in-flight, deferred run, min-user-turns gate, takeover stop (pre-start and pre-emit),
  shutdown cancellation, LLM-failure skip.
- **Contract tests:** JSON parsing, fence stripping, score clamping, flag coercion,
  unassessable no-op; prompt prefix-stability test; re-anchoring window behavior.
- **Consumer tests:** guards (terminal, intervener, idempotency), Call column updates,
  transition detection with hysteresis, CallEvent rows only on transitions, status flips
  both directions, notification on escalation only, audience computation
  (published/unpublished).
- **CRITICAL status interplay:** `_close_and_refill` closes a CRITICAL call;
  `PipelineSweeper` does not treat CRITICAL as stuck.
- **Notification SSE endpoint test:** auth, user-targeted vs tenant-wide filtering,
  keepalive.
- **Frontend:** narrower tests, provider behavior (toast, reconnect refetch), badge
  rendering (NULL → neutral, stale → gray).
- **Verification:** boot the real services and drive a call end-to-end (background-loop
  features are verified against the running service, not just unit tests), then the full
  gates: backend `just check`, frontend `tsc` + `eslint` + tests + build.

## 9. Scale notes — Redis connections (2026-07-18 amendment)

Reference load: 300 logged-in users, 1,500 concurrent outbound calls (= 1,500 worker
jobs), 300 concurrently-watched per-call streams.

**Blocked connections** (pinned in a `BLOCK`ing read for the lifetime of the thing;
cost ~zero Redis CPU while parked):

| Source | Count | Blocking read |
|---|---|---|
| Notification SSE (1 per logged-in user) | 300 | `XREAD BLOCK` on `vera:notify:{tenant}` |
| Per-call events SSE (1 per watcher) | 300 | `XREAD BLOCK` on `vera:call-events:{room}` |
| Worker-event consumer (1 per control-plane replica) | ~2 | `XREADGROUP BLOCK` |

Worker jobs contribute **zero** blocking connections — they only `XADD` (turns, health
frames, lifecycle events) and one-shot `GET/SET` (call plan).

**Open (non-blocking) connections:** each real-call worker job holds ~3 idle-between-
publishes clients (worker-event bus, call stream, call plan) → ~4,500 at 1,500 calls.
The health observer added none of these — it publishes through the job's existing
clients; its only new I/O is HTTPS to the LLM.

**Total ≈ 5,150 vs Memorystore `maxclients` 65,000 → ~8% utilization.** The dominant
term (worker publishers) predates this feature and scales with calls; the SSE terms
scale with humans.

Deliberately-deferred levers (numbers don't justify them yet):

1. **Consolidate the worker's three per-job clients into one** (same Redis; separation
   is tidiness, not necessity) → 4,500 → 1,500 at this load.
2. **In-process notification broadcaster** (one `XREAD` per app replica fanned out to
   local SSE subscribers) → collapses the notification term from N users to N replicas.
   The `NotificationService` seam isolates this swap from every consumer.

**Stream hygiene (no unbounded growth):** `vera:worker-events` is one global key,
`XADD MAXLEN ~10k`. `vera:notify:{tenant}` is `MAXLEN ~1k` + rolling 24h `EXPIRE`
refreshed on publish — idle tenants' keys self-delete. `vera:call-events:{room}` has a
rolling 1h `EXPIRE` (15min grace after the ended sentinel) and is explicitly deleted by
the transcript finalizer at closeout. `vera:notify` deliberately has NO consumer group
(broadcast fan-out — every connection must see every entry, filtered by audience);
`vera:worker-events` has one (exactly-one-process processing + XAUTOCLAIM reclaim,
with idempotent handlers for at-least-once redelivery).
