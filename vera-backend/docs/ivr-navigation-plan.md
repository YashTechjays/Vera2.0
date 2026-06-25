# IVR Navigation — Task 1: Generic Navigator (STT-only) + Voice Lab toggle

## Context

Vera 2.0 places outbound calls to insurance payers for benefit verification. Before a call
reaches a human rep or a self-service data line, it must traverse the payer's automated phone
menu (IVR) — the `CallStatus.IVR` phase between `RINGING` and `ACTIVE`.

Task 1 is the **generic** navigator: a provider-agnostic engine that listens to IVR audio,
understands the prompt, and responds to steer the call toward a goal (reach a
benefits/eligibility representative). It must work for any insurer with no pre-scripted path.

This document covers the **Task 1 audio runtime** only, scoped down to what's demoable today:

- **STT-only** — no DTMF this iteration (DTMF is a later add).
- **LLM-driven**, no keyword heuristics — the cascade already does perceive→decide→act
  (Deepgram STT → Gemini → Cartesia TTS). The "generic navigator" is therefore primarily a
  **distinct agent persona/prompt** plus wiring, not a new decision engine.
- **Integrated with the Voice Lab page** via a pre-call **toggle**. Toggle ON → the worker
  boots the IVR navigator; OFF → the current `VeraAgent` (infertility verification).

Confirmed decisions:
- The toggle is a **pre-call setting** — it decides which agent the worker boots, not a live
  mid-call hot-swap.
- When the navigator reaches a human, it **announces it and stops** — no handoff to the
  verification agent (future scope).

Why this maps cleanly: Voice Lab already passes per-session flags to the worker through LiveKit
**dispatch metadata** (`wait_for_speaker`, `publish_transcript`, set in
`api/v1/voice_lab.py` → `LiveKitGateway.create_call_room` → `create_dispatch`, read in
`agent_worker/main.py` via `json.loads(ctx.job.metadata)`). The toggle is one more flag
(`ivr_navigation`) on that same proven seam.

## Intended outcome

On the Voice Lab page, a user flips an "IVR Navigation" switch, starts an in-browser session,
and speaks payer-IVR menu prompts into the mic ("For claims press 1, for eligibility and
benefits press or say 2, to reach a representative say agent..."). The agent — instead of
greeting and asking benefit questions — listens, picks the path toward an eligibility/benefits
rep, and speaks the right response; when it "reaches a rep" it says so and goes quiet. With the
switch off, Voice Lab behaves exactly as today.

## What already exists (reuse, do not recreate)

- The cascade: `agent_worker/cascade.py` (`build_session`) — Deepgram(Flux) → Gemini → Cartesia,
  local Silero VAD, `EnglishModel` turn detection, `interruption.mode="vad"`. Reused as-is.
- The agent + PHI seams: `agent_worker/agent.py` (`VeraAgent` with `stt_node`/`transcription_node`
  /`tts_node` overrides), `agent_worker/seams.py` (`redact_event`/`hydrate_stream`).
- The persona/prompt strings: `agent_worker/prompt.py` (`SYSTEM_PROMPT`, `GREETING`,
  `CARTESIA_MARKUP_GUIDE`, `_INSTRUCTIONS`).
- The navigator brain reference: `docs/generic-IVR-system-prompt.md` — the full, production-shaped
  navigator prompt. It is written for a **structured-action controller** (per-turn JSON actions
  `speak`/`dtmf`/`wait`/`hangup` via `responseSchema`, with `call_data` injected). This iteration
  does **not** use that runtime (see "Prompt adaptation" below); the doc is kept as the target for
  a later structured-controller iteration.
- The per-session metadata seam: `agent_worker/main.py:155` (`meta = json.loads(...)`),
  `control_plane/livekit_gateway.py` (`create_call_room(room_name, metadata)`),
  `control_plane/api/v1/voice_lab.py` (`start_voice_session`).
- DTOs: `vera_core/schemas/dto.py` (`StartVoiceSessionRequest`, `VoiceSessionResponse`).
- Frontend: `vera-frontend/src/pages/VoiceLab.tsx`, `src/lib/api/voiceLab.ts`,
  `src/components/ui/switch.tsx` (Switch pattern already used in `LiveMonitoring.tsx:195`).

## Design

### Backend — agent worker

**1. IVR-navigator persona/prompt** — `agent_worker/prompt.py`

Add `IVR_NAVIGATOR_SYSTEM_PROMPT` and
`_IVR_NAVIGATOR_INSTRUCTIONS = f"{IVR_NAVIGATOR_SYSTEM_PROMPT}\n\n{CARTESIA_MARKUP_GUIDE}"`
(reuse the existing `CARTESIA_MARKUP_GUIDE`).

**Prompt adaptation — from `docs/generic-IVR-system-prompt.md`, cascade-compatible.** The doc's
prompt is the source of the navigator's behavior, but it targets a structured-action controller;
in this iteration the LLM's text output is **spoken directly by Cartesia TTS** (the cascade's
`tts_node`), so the prompt must produce **plain spoken words, never JSON**. Derive
`IVR_NAVIGATOR_SYSTEM_PROMPT` by adapting the doc as follows:

- **Keep:** the ROLE / PRIMARY GOAL framing (provider-side caller verifying **eligibility and
  benefits** — generic IBV, not infertility-specific); the stage-based *intent* recognition
  (caller gate → provider auth → department/intent menu → escalate); and the escalation /
  remain-on-hold-over-callback logic — all expressed as the **literal words to speak**.
- **Strip (out of scope this iteration):**
  - The structured-output contract — no `responseSchema`, no `{"action":…,"value":…}` JSON, no
    `reasoning`/`confidence`/`stage` fields. Output is just the spoken response.
  - All `dtmf` actions — speak the menu option's spoken equivalent instead ("eligibility and
    benefits", "representative", "agent", "provider services"). No DTMF this iteration.
  - `call_data` injection and the member-ID quirks — there is no per-turn data object and no raw
    PHI in the prompt (PHI wall). If the IVR asks for member ID / NPI / DOB / tax ID, ask for a
    representative instead (keeps PHI out of Voice Lab entirely).
  - The full `rep` phase — when a human is reached, say one short acknowledgement and **stop**
    (no detail hand-off, no benefit walkthrough, no reference-number capture).
- **Also include:** wait for the full menu before responding and pick the single best match for
  the goal; reuse the short / plain-sentences / no-symbols spoken-output discipline; do not select
  language-change / enrollment / claims / authorizations branches unless they lead to
  eligibility/benefits.

Keep `SYSTEM_PROMPT`, `GREETING`, `_INSTRUCTIONS`, `build_instructions()` unchanged. Optionally
add `build_ivr_instructions()` mirroring `build_instructions()`.

**2. Navigator agent + shared PHI plumbing** — `agent_worker/agent.py`

Refactor so both agents share the PHI-wall node overrides instead of duplicating them:

- Extract `PHIWallAgent(Agent)` holding `_boundary`/`_session_id` and the three node overrides
  (`stt_node` redact, `transcription_node` pass-through, `tts_node` hydrate).
- `VeraAgent(PHIWallAgent)` — unchanged: `instructions=_INSTRUCTIONS`, `on_enter` says `GREETING`.
- New `IvrNavigatorAgent(PHIWallAgent)` — `instructions=_IVR_NAVIGATOR_INSTRUCTIONS`, `tools=[]`,
  and **`on_enter` does NOT greet** (the IVR starts talking; the navigator listens first).
  Inherit the default no-op `on_enter` or define an explicit commented no-op.

This split also makes a future navigator→verification handoff a one-line change.

**3. Agent selection from dispatch metadata** — `agent_worker/agent.py` + `agent_worker/main.py`

Add a small, unit-testable selector in `agent.py`:

```python
def build_agent(meta: dict, *, boundary, session_id) -> Agent:
    if meta.get("ivr_navigation"):
        return IvrNavigatorAgent(boundary=boundary, session_id=session_id)
    return VeraAgent(boundary=boundary, session_id=session_id)
```

Call it in `main.py` (~line 204-208) in place of the hardcoded `VeraAgent(...)`. Everything else
(PHI boundary, `wait_for_speaker`, transcript publishing, `room_input_options`) is unchanged and
applies to both agents. `cascade.py` is reused as-is; IVR-specific endpointing tuning is a
possible follow-up, not in scope.

### Control plane — thread the flag through

- `vera_core/schemas/dto.py` — add `ivr_navigation: bool = False` to `StartVoiceSessionRequest`.
- `control_plane/api/v1/voice_lab.py` (`start_voice_session`) — include it in dispatch metadata:

```python
await livekit.create_call_room(
    room_name,
    metadata={
        "wait_for_speaker": True,
        "publish_transcript": True,
        "ivr_navigation": body.ivr_navigation,
    },
)
```

No other endpoint changes; `create_call_room`/`create_dispatch` already serialize the dict.

### Frontend — Voice Lab toggle

- `src/lib/api/voiceLab.ts` — add `ivr_navigation?: boolean` to `StartVoiceSessionPayload`.
- `src/pages/VoiceLab.tsx` — in the pre-call `<Card>` (`!session` branch, near the phone-input
  block at lines 183-207):
  - `const [ivrNavigation, setIvrNavigation] = useState(false)`.
  - Render a `<Switch>` + `<Label>` row (import `Switch` from `@/components/ui/switch`; pattern
    from `LiveMonitoring.tsx:195`) using the existing `space-y-2` / `flex items-center
    justify-between` conventions, with short helper copy ("Navigate the payer's phone menu
    automatically before reaching a rep").
  - In `start(mode)`, include `ivr_navigation: ivrNavigation` in the `startVoiceSession` payload
    for both modes.
  - The switch lives only in the pre-call card (pre-call setting); it disappears once a session
    starts.

## Critical files

**Edit (backend):** `agent_worker/prompt.py`, `agent_worker/agent.py`, `agent_worker/main.py`,
`vera_core/schemas/dto.py`, `control_plane/api/v1/voice_lab.py`.
**Edit (frontend):** `src/lib/api/voiceLab.ts`, `src/pages/VoiceLab.tsx`.
**Reference (unchanged):** `agent_worker/cascade.py`, `agent_worker/seams.py`,
`control_plane/livekit_gateway.py`.

## Tests

**Backend (`apps/agent_worker/tests/unit/`):**
- `test_prompt.py` — navigator instructions exist, mention navigating a menu/representative, no
  tool machinery, reuse the Cartesia guide.
- `test_agent.py` — `IvrNavigatorAgent` carries navigator instructions, `tools == []`, and
  (unlike `VeraAgent`) emits no greeting on enter; both route through the shared `PHIWallAgent`
  node overrides. `build_agent` test: `{"ivr_navigation": True}` → `IvrNavigatorAgent`,
  absent/false → `VeraAgent`.

**Backend (control-plane integration, `tests/integration/control_plane/`):** extend the Voice
Lab test to assert `ivr_navigation` reaches dispatch metadata via `FakeLiveKit.dispatch_metadata`
(true when toggled, false/default otherwise).

**Frontend (`src/lib/api/voiceLab.test.ts`):** assert `ivr_navigation` is included in the POST
body when set.

## Verification

- Backend gate: `just check` (ruff + mypy --strict + pytest) from `vera-backend/`, then `/simplify`
  on the diff and re-run `just check` (per repo CLAUDE.md).
- Frontend: Vitest suite + typecheck/build in `vera-frontend/`.
- Manual end-to-end (local): `just up && just migrate && just api && just worker`; open Voice Lab,
  flip **IVR Navigation** on, "Start in-browser session", and speak mock IVR menu prompts into the
  mic. Expect: no greeting, the agent speaks the goal-advancing option, and on a "reached a
  representative" prompt it acknowledges and stops. Flip the toggle off and confirm the original
  greeting + verification behavior is unchanged. The live transcript panel shows both sides.

## Out of scope (later iterations / vera-2.x)

The **structured-action controller** runtime (per-turn `responseSchema` JSON actions
`speak`/`dtmf`/`wait`/`hangup` with `call_data`) described in `docs/generic-IVR-system-prompt.md` —
the target for a later iteration, not built here; DTMF tone emission; per-call `call_data` / goal
injection (member ID, NPI, DOB, tax ID) and the member-ID quirk handling (raw PHI in the prompt —
needs a deterministic non-LLM seam, not literal values in the LLM context); the full rep-phase
conversation (detail hand-off, benefit walkthrough, reference-number capture); live mid-call toggle
/ agent hot-swap; navigator→verification handoff; IVR-specific cascade/endpointing tuning; and
Tasks 2–4
(per-provider playbook DSL + CRUD, runtime generic-vs-playbook selection, max-attempts /
hold-timeout → `CALL_FAILED` resilience), which build on the existing `InsuranceProvider` /
`IvrPlaybook` models and `CallStatus.{IVR,WAITING,FAILED}` / `FormStatus.CALL_FAILED` enums.
