# Call History — transcript popup

**Date:** 2026-07-30
**Status:** design approved, pending spec review

## Goal

On the Call History page (`/call-history`), let a user open a **completed call's
transcript** in a popup, from a trigger next to the existing "Play recording" link on
each call row. Transcript-only (VERA ↔ rep turns), read-only, mirroring the
"Transcription" view already used in the Live Monitoring call modal.

## Non-goals

- No AI summary, copy, or export in the popup (transcript turns only).
- No transcript trigger inside the form modal's per-attempt "Call history" list
  (main table row only). Extendable later.
- No new transcript storage or streaming mechanism — reuse what exists.

## What already exists (reused, not rebuilt)

- Transcripts are persisted to the DB at call end (`Transcript` model,
  `transcript_finalizer.finalize_transcript`).
- `GET /calls/{call_id}/events` (SSE, gated on `calls:read`, visibility = owner or
  published/ownerless) **replays a terminal call's stored transcript** from the DB,
  then closes. It already emits the PHI-read audit for the disclosure.
- `CallTranscript` (`vera-frontend/src/components/monitoring/CallTranscript.tsx`)
  consumes that SSE and renders the turns, holding them in **session-only component
  state** (discarded on unmount).

## Approach (A — reuse)

Add a **"View transcript"** trigger to each Call History row, next to "Play
recording". Clicking it opens a **shadcn `Dialog`** whose body renders `CallTranscript`
for that `call_id`. `CallTranscript` opens `/calls/{call_id}/events`, the endpoint
replays the stored transcript, the turns render, and closing the dialog unmounts the
component (SSE closed, turns discarded). **No new data endpoint.**

### Decisions (approved)

- **Permission:** gated on `calls:read`. The transcript endpoint already requires
  exactly that, and viewing the Call History page already requires it — so the trigger
  adds no new exposure. (A stricter `recordings:read` parity was considered and
  declined; the recording button keeps its own `recordings:read` gate unchanged.)
- **Availability:** a new **`transcript_available: bool`** on the call-history row DTO
  (mirroring `recording_available`), so the trigger only appears for calls that
  actually have a stored transcript — hidden for NO-ANSWER / never-connected calls.

## Components & changes

### Backend

- **Row DTO** (`api/v1/calls.py`, the call-history row shape near `_call_attempt_view`):
  add `transcript_available: bool`, computed per call as "a `Transcript` row exists for
  this call" (an `EXISTS` against the `Transcript` table, batched with the existing
  call-history query — no N+1). Caller-aware only insofar as the row is already
  visibility-filtered; no extra permission needed (transcript = `calls:read` = page
  access).
- **No new endpoint.** The popup reuses `GET /calls/{call_id}/events`.

### Frontend

- **`CallHistoryRow` type** (`src/lib/api/calls.ts`): add `transcript_available: boolean`.
- **Call History row** (`src/pages/CallHistory.tsx`): next to the "Play recording"
  button, render a **"View transcript"** button when `c.transcript_available` is true
  (no extra `usePermission` gate — page access already implies `calls:read`). It
  `stopPropagation()`s the row's open-form click (same pattern as the recording button)
  and toggles a dialog open with `c.id`.
- **Transcript dialog** (new small component, e.g.
  `src/components/monitoring/TranscriptDialog.tsx`): a `Dialog` titled
  `Transcript — <patient> · <date/time>`, body renders the transcript. Reuse
  `CallTranscript`, trimming its live-call affordances for a static view:
  - drop auto-scroll-to-bottom / "live" indicators (finished call, static content);
  - it consumes the same `/calls/{id}/events` SSE, which for a terminal call yields the
    replay then ends — so the existing streaming code path is reused unchanged.
  - If `CallTranscript` can't be cleanly reused read-only, extract the turn-rendering
    into a shared presentational piece and feed it the replayed turns; decide during
    planning.

## Data flow

1. Row renders "View transcript" iff `transcript_available`.
2. Click → open `TranscriptDialog(callId, patient, createdAt)`.
3. Dialog mounts `CallTranscript(callId)` → opens `/calls/{callId}/events`.
4. Endpoint authorizes (`calls:read` + visibility), emits PHI-read audit, replays the
   stored transcript turns, closes the stream.
5. Turns render in the dialog (session-only state).
6. Close dialog → unmount → SSE closed, turns discarded (no persistence/caching).

## States & errors

- **No transcript:** trigger not shown (gated on `transcript_available`).
- **Empty replay** (flag true but no turns land): dialog shows a neutral
  "No transcript available for this call." placeholder.
- **Stream / auth error:** dialog shows a short non-PHI error message (reuse
  `CallTranscript`'s existing error handling); never surface raw error text.
- **PHI hygiene:** turns live in component state only, discarded on close; nothing
  logged, cached, or put in the URL. Inherits the endpoint's `Cache-Control: no-store`.

## Testing

- **Backend:** the call-history row DTO carries `transcript_available`; it is `true`
  when a `Transcript` row exists for the call and `false` when none do (unit/integration
  around the row query). Reuse existing `/calls/{id}/events` terminal-replay coverage.
- **Frontend:** "View transcript" renders only when `transcript_available` is true;
  clicking opens the dialog with the correct `call_id`; closing unmounts the transcript.

## Out of scope / follow-ups

- Copy/export, AI summary in the popup.
- Transcript trigger in the form modal's per-attempt list.
- Search/highlight within the transcript.
