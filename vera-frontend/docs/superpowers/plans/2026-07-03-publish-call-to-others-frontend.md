# Publish-to-others — Frontend Implementation Plan

> Companion to the backend contract (already landed on `feat/publish-call-to-others`:
> `GET /calls`, `POST /calls/{id}/publish`, `GET /calls/{id}/join-token`,
> `POST /calls/{id}/revoke-access`). Design source: `vera-backend/docs/superpowers/specs/2026-07-02-publish-call-to-others-design.md` §4.5.
> Built on the same branch to ship one complete feature.

## Global constraints
- **TDD**, task-by-task. Each task: write the failing test → implement → green → commit.
- **Mirror existing patterns**: API client mirrors `src/lib/api/voiceLab.ts` (thin `apiRequest<T>` wrappers; `apiRequest` injects the bearer token, unwraps the `{data}` envelope, throws `ApiError`). Tests mirror `src/lib/api/voiceLab.test.ts`. Redux mirrors `src/store/authSlice.ts` + the existing `useAppDispatch`/`useAppSelector` hooks. LiveKit mirrors `VoiceLab.tsx`'s `<LiveKitRoom>` usage.
- **PHI/HIPAA** (`vera-frontend/CLAUDE.md`): no PHI in URLs (call ids are opaque UUIDs — fine); patient_name is session-scoped display only; never log identifiers; nothing in localStorage.
- **Gate** before "done": `tsc --noEmit` + `eslint` + `vitest` + `vite build`.

## Backend contract (what the client mirrors)
- `GET /calls` → `CallSummary[]` — `{ id, tenant_id, status, room_name, patient_name, started_at, created_at, published, is_owner }`. Server already scopes to owner-or-published + active statuses, newest first.
- `POST /calls/{id}/publish` → `CallSummary` — owner-only (`calls:publish`), one-way, idempotent.
- `GET /calls/{id}/join-token` → `{ token, url, room_name }` — non-owner allowed only for a published call (else 404); mints a supervisor (listen+talk) token.
- `POST /calls/{id}/revoke-access` `{ target_user_id }` → `null` — owner ejects a participant; LiveKit identity is `supervisor-{user_id}`.

---

## OPEN DECISIONS — resolve before Task 4 (Publish button)

**D1 (blocking Task 4) — Where does the owner start a *real* `/calls` call, and where does the Publish button live?**
The Publish button needs a `call_id`, but today `VoiceLab.tsx` starts **ephemeral voice-lab sessions** (`POST /voice-lab/sessions`) which create **no `Call` row**. So there is no `call_id` to publish. Options:
- **(a)** Wire a "Start call" action from **Data Management** (a patient form → `POST /calls {form_id}` → an owner live-call view that hosts `<LiveKitRoom>` + `SessionPanel` with the Publish button). *Most faithful to the spec's data flow (§5); largest.*
- **(b)** Repurpose/extend `VoiceLab` to create a real `Call` (add `startCall(formId)`), so its `SessionPanel` carries a real `call_id`.
- **(c)** Scope this PR to **Live Monitoring + Intervene only** (consume/join existing published calls) and defer the owner-side Publish UI to a follow-up. *Smallest; still delivers tenant-wide visibility + intervene.*
> **Recommendation:** (a) if we want the full owner→publish→intervene loop in this PR; (c) if we want to ship incrementally. **Pick one before Task 4.** Tasks 1–3 and 5 (intervene) don't depend on this.

**D2 — Intervene hosts its own room.** The browser currently hosts a single `<LiveKitRoom>` (VoiceLab). Intervening on another VA's call opens a *second* room. Plan: the **InterveneModal hosts its own `<LiveKitRoom>`** instance (token from `getJoinToken`), independent of any VoiceLab room. Confirmed approach.

**D3 — Row display mapping.** `CallSummary` has `status`, `patient_name`, `started_at`, `published`, `is_owner` — but the mock rows also show `agent`, `duration`, `category`, `confidence`, `formProgress` which the API does not return. Plan: derive `duration` from `started_at` (ticking), map `status → category/badge`, drop `agent`/`confidence`/`formProgress` (or show "—") until the API grows. Confirm which columns stay.

**D4 — Revoke target.** The owner revokes a specific intervener. Source the `target_user_id` by parsing the LiveKit participant identity `supervisor-{user_id}` from the room's participant list (owner's `SessionPanel`). Non-owner participants get a "Revoke" affordance.

**D5 — View Live vs Intervene.** `join-token` always mints a listen+talk token. "View Live" (published, non-owner) and "Intervene" can share the same join; if "View Live" should be listen-only, mute the local mic client-side. Confirm.

---

## Task 1 — Typed API client `src/lib/api/calls.ts` (+ `calls.test.ts`)
Mirror `voiceLab.ts`.
- **Types:** `CallSummary`, `JoinTokenResponse`, `StartCallRequest` (`{ form_id }`), `RevokeAccessRequest` (`{ target_user_id }`), `CallStatus` union.
- **Functions:** `listCalls()`, `publishCall(callId)`, `getJoinToken(callId)`, `revokeAccess(callId, targetUserId)`, `startCall(formId)` (include even if D1 defers its UI).
- [ ] Step 1: write `calls.test.ts` (mirror `voiceLab.test.ts`) — assert each call hits the right path/method/body and propagates `ApiError`. Mock `apiRequest`.
- [ ] Step 2: run → fails (module absent).
- [ ] Step 3: implement `calls.ts`.
- [ ] Step 4: `vitest src/lib/api/calls.test.ts` green.
- [ ] Step 5: commit `feat(calls-fe): typed calls API client`.

## Task 2 — `callsSlice` + store registration (+ `callsSlice.test.ts`)
Mirror `authSlice.ts`.
- **State:** `{ items: CallSummary[]; status: "idle"|"loading"|"error"; error: string | null }`.
- **Thunk:** `fetchCalls` (calls `listCalls`); **reducers/thunks** for `publish(callId)` (optimistic or refetch). Selectors: `selectActiveCalls`, `selectCallById`.
- Register `calls` reducer in `src/store/index.ts`. Reuse existing `useAppDispatch`/`useAppSelector`.
- [ ] Step 1: write `callsSlice.test.ts` — reducer transitions (loading→fulfilled→items; error) + publish updates the item's `published`.
- [ ] Step 2: run → fails.
- [ ] Step 3: implement the slice + register it.
- [ ] Step 4: green.
- [ ] Step 5: commit `feat(calls-fe): callsSlice + store wiring`.

## Task 3 — Live Monitoring on real `listCalls()` + polling
Replace `mock-data.ts` usage in `LiveMonitoring.tsx`.
- Dispatch `fetchCalls` on mount; **poll every 8s** via `setInterval` (cleared on unmount; pause when tab hidden via `document.visibilityState`).
- Map `CallSummary` → row per **D3**. **"Visible To All"** becomes a **read-only** indicator of `published` (not an editable `Switch`).
- Keep the tab/table/stat-card shell; feed rows from `selectActiveCalls`. Stats derived from the list (active count, etc.) or left static pending an endpoint (D3).
- Keep `CallOverviewModal` / `InterveneModal` but pass a real call (adapt their prop type from `LiveCall` → `CallSummary`).
- [ ] Step 1: write a component/integration test (mock `listCalls`) — rows render from fetched data; published shows read-only; poll re-fetches.
- [ ] Step 2: fails.
- [ ] Step 3: implement; delete the now-dead mock imports.
- [ ] Step 4: green.
- [ ] Step 5: commit `feat(calls-fe): Live Monitoring on real calls + polling`.

## Task 4 — Publish button in the owner's `SessionPanel` **(depends on D1)**
- Add a one-way **Publish** button into `SessionPanel`'s `actions` slot: calls `publishCall(callId)`, then disables and shows "Published ✓" (no un-publish). Errors via the existing `ErrorAlert`.
- Wire it into whichever owner live-call view D1 selects.
- [ ] Step 1: write a test — button calls `publishCall`, disables + shows published; hidden/absent for non-owners.
- [ ] Step 2–4: implement + green.
- [ ] Step 5: commit `feat(calls-fe): one-way Publish control`.

## Task 5 — View Live / Intervene via `join-token` (+ owner Revoke)
- **InterveneModal**: on open → `getJoinToken(callId)` → host its own `<LiveKitRoom serverUrl={url} token connect audio>` (per **D2/D5**) with `RoomAudioRenderer` + participant list; leaving disconnects.
- **View Live** (published, non-owner): same join; listen-only variant mutes local mic (D5).
- **Owner Revoke**: in the owner's `SessionPanel` participant list, a per-participant "Revoke" that parses `supervisor-{user_id}` → `revokeAccess(callId, userId)` (D4).
- [ ] Step 1: tests — modal requests a join token and mounts a room; revoke calls `revokeAccess` with the parsed id; 404 join surfaces a friendly error.
- [ ] Step 2–4: implement + green.
- [ ] Step 5: commit `feat(calls-fe): intervene via join-token + owner revoke`.

## Task 6 — Simplify pass + full frontend gate
- [ ] Run `/simplify` on the diff (quality only).
- [ ] `tsc --noEmit` + `eslint` + `vitest run` + `vite build` all green.
- [ ] Commit any cleanups; then this branch carries backend + frontend as one feature → open/refresh the PR.

---

## Testing summary (spec §7, frontend rows)
- **API client** — request shapes + `ApiError` propagation for all five methods.
- **Slice** — fetch lifecycle + publish state update.
- **Live Monitoring** — renders real rows; `published` read-only; poll re-fetches; empty state.
- **Publish** — one-way disable; owner-only visibility.
- **Intervene** — join-token requested, room mounted; revoke parses identity → id; private-call 404 → friendly error.

## Out of scope (spec §8 / backend follow-ups)
- No un-publish. No SSE/push — polling only (push is a later follow-up). No cross-tenant. No typed `POST /interventions` action endpoint (intervention == join the room).
