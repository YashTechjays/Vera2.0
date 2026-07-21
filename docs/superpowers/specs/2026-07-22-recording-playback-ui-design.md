# Recording playback in the IBV form UI — design

**Date:** 2026-07-22
**Branch:** `feat/recording-playback-ui` (origin/dev + feat/review-and-export)
**Status:** approved in brainstorming; this document records the design.

## Goal

Let a user play a call attempt's recording from the IBV form modal's Call History
tab. Playback authorization stays exactly as the backend already enforces it
(spec decision 6 of the call-recording-persistence design): the caller needs the
`recordings:read` permission AND the call must be visible to them — its owner
(`initiated_by_id`) always, other permission holders only once the call is
published. No new access rules.

## Non-goals

- No change to the playback endpoint, its permission, or the owner-or-published
  visibility rule.
- No download button, waveform, or transcript-synced player.
- No recording surface outside the Call History tab.

## Backend — DTO enrichment only

`GET /patient-forms/{form_id}/calls` (`list_form_calls`, patient_forms.py):

- `CallAttemptView` gains `recording: Literal["available"] | None`.
- Computed with one query: the latest `Recording` row per attempt call id,
  taking `RecordingStatus.AVAILABLE` only.
- The caller-visibility rule is applied server-side per call using the same
  predicate as the playback endpoint (`_call_hidden_from`, extracted from
  `api/v1/calls.py` into a shared helper so the two gates cannot diverge).
  A hidden call's attempt row still renders (history is `forms:read`), but its
  `recording` field is `null` — the UI never advertises a recording the caller
  cannot fetch.
- Everything else (no recording row, PENDING/FAILED/DISCARDED/DELETED, hidden
  call) → `null`.

Why not probe `GET /calls/{id}/recording` per attempt from the FE: each probe
mints a signed URL and writes a `RECORDING_ACCESSED` audit row — rendering a
list must not create disclosure-audit noise.

## Frontend

New API wrapper — `src/lib/api/calls.ts`:

- `getRecordingPlayback(callId): Promise<RecordingPlayback>` →
  `GET /calls/{callId}/recording`, `RecordingPlayback = { url: string;
  expires_at: string }`. Rides `apiRequest` (envelope unwrap, ApiError).

New component — `src/components/ibv/RecordingPlayer.tsx`:

- Props: `{ callId: string }`. Renders a "Play recording" button; on click,
  fetches the signed URL (explicit user action only — the fetch is audited
  server-side) and swaps to a native `<audio controls autoPlay>`.
- Caches `{url, expires_at}` in state; a collapse/expand inside the TTL does
  not refetch.
- If the `<audio>` element errors and the URL is past `expires_at`, refetch
  once and resume from the last `currentTime`; a second failure shows the
  inline error.
- Errors (404/409/network) render as inline `role="alert"` text — "Recording
  unavailable." — matching CallHistoryTab's conventions. No toasts.

`CallHistoryTab.tsx`:

- Per attempt, when `attempt.recording === "available"` AND
  `usePermission("recordings:read")`, render `<RecordingPlayer callId={...} />`.
- Only one attempt's player is expanded at a time (expanding another collapses
  the first — also prevents overlapping audio).

Types — `src/lib/patient-forms/types.ts`: `CallAttempt` gains
`recording: "available" | null`.

## Error handling

- FE hides the control entirely when the DTO says `null` or the permission is
  absent; the backend remains the enforcement point (404/403/409).
- Signed-URL expiry mid-listen: one silent refetch + resume, then inline error.

## Testing

Backend (`tests/unit/` + `tests/integration/control_plane/`):
- Enrichment matrix on `list_form_calls`: owner sees `available`; non-owner on
  an unpublished call sees `null`; non-owner on a published call sees
  `available`; PENDING/FAILED/DISCARDED/no-recording → `null`.
- Shared visibility helper: playback endpoint behavior unchanged (existing
  tests keep passing).

Frontend (vitest, co-located):
- `calls.test.ts`: `getRecordingPlayback` calls the right path via `apiRequest`.
- `RecordingPlayer.test.tsx`: click fetches URL and renders audio; fetch error
  shows inline alert; expired-URL error path refetches once.
- `CallHistoryTab.test.tsx`: button gated by `recording` field + permission.

Gates: backend `just check`; frontend `tsc -b` + `eslint` + `vitest` + `build`.
