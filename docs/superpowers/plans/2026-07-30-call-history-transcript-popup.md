# Call History Transcript Popup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "View transcript" trigger on each Call History row that opens a read-only popup showing that completed call's transcript.

**Architecture:** Reuse the existing `GET /calls/{id}/events` endpoint (it already replays a terminal call's stored transcript and emits the PHI-read audit) and the existing `CallTranscript` component (renders turns, session-only state). The only new data is a `transcript_available` boolean on the call-history row DTO to gate the trigger; the popup is a thin shadcn `Dialog` wrapping `CallTranscript`.

**Tech Stack:** Backend — FastAPI, SQLAlchemy async, Postgres (control_plane). Frontend — React + Vite + TypeScript, shadcn/Radix Dialog, Vitest + @testing-library/react.

## Global Constraints

- **Backend gate:** `just check` (ruff check + ruff format --check + mypy --strict + pytest) green before done. The new integration test needs a live Postgres (CI provides one).
- **Frontend gate:** `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build`, all green on the pushed tree.
- **Permission:** the trigger is gated only by `transcript_available`; viewing the Call History page already requires `calls:read`, which is exactly what `GET /calls/{id}/events` enforces. No new permission check.
- **PHI:** transcript turns live in `CallTranscript` component state only, discarded on unmount; nothing logged, cached, or placed in a URL. The reused endpoint already sets `Cache-Control: no-store` and audits the disclosure.
- **Comments:** only where they explain something the code cannot; one line max. Docstrings one sentence.

---

### Task 1: Backend — `transcript_available` on the call-history row

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py` (`CallHistoryRow` DTO ~line 909; `list_call_history` query ~lines 976–1058)
- Test: `vera-backend/tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Produces: `CallHistoryRow.transcript_available: bool` on the `/call-history` response items — `true` iff a `Transcript` row exists for the call.

- [ ] **Step 1: Write the failing test**

Add to `vera-backend/tests/integration/control_plane/test_calls.py` (imports `Transcript`, `seed_call`, `tenant_session`, `_auth` already exist; add `TranscriptSource` to the `from vera_core.models.enums import ...` line):

```python
@pytest.mark.asyncio
async def test_call_history_reports_transcript_available(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A call with a stored Transcript row reports transcript_available=True; one with
    none reports False (gates the row's 'View transcript' trigger)."""
    with_tx = await seed_call(
        admin_sessionmaker, rbac_world.tenant_id, seeded_form_id,
        initiated_by_id=rbac_world.admin_id, status="completed",
    )
    without_tx = await seed_call(
        admin_sessionmaker, rbac_world.tenant_id, seeded_form_id,
        initiated_by_id=rbac_world.admin_id, status="completed",
    )
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        session.add(
            Transcript(
                tenant_id=rbac_world.tenant_id, call_id=with_tx, seq=0,
                source=TranscriptSource.BOT.value, role="assistant",
                message="Hello, this is Vera.",
            )
        )

    resp = await client.get("/api/v1/call-history", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    by_id = {r["id"]: r for r in resp.json()["data"]["items"]}
    assert by_id[str(with_tx)]["transcript_available"] is True
    assert by_id[str(without_tx)]["transcript_available"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_calls.py::test_call_history_reports_transcript_available -q`
Expected: FAIL — `KeyError: 'transcript_available'` (field not in the response). *(If Postgres/`vera_test` isn't available locally it errors on DB setup instead; run in CI. See CLAUDE.md.)*

- [ ] **Step 3: Add the DTO field**

In `calls.py`, in `class CallHistoryRow(BaseModel)`, add after `recording_available: bool`:

```python
    recording_available: bool
    # True when a stored transcript exists for this call — gates the row's "View transcript".
    transcript_available: bool
```

- [ ] **Step 4: Add the EXISTS subquery and populate the field**

In `list_call_history`, next to the existing `has_recording = ...`, add:

```python
    has_transcript = select(Transcript.id).where(Transcript.call_id == Call.id).exists()
```

Add `has_transcript.label("has_transcript"),` to the `select(...)` column list in `_fetch_page` (right after `has_recording.label("has_recording"),`).

In the `CallHistoryRow(...)` construction inside the `items = [...]` comprehension, add:

```python
            transcript_available=r.has_transcript,
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_calls.py::test_call_history_reports_transcript_available -q`
Expected: PASS.

- [ ] **Step 6: Run the backend gate for the changed files**

Run: `cd vera-backend && uv run ruff check apps/control_plane/src/control_plane/api/v1/calls.py tests/integration/control_plane/test_calls.py && uv run ruff format --check apps/control_plane/src/control_plane/api/v1/calls.py && uv run mypy apps/control_plane/src/control_plane/api/v1/calls.py`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py \
        vera-backend/tests/integration/control_plane/test_calls.py
git commit -m "feat(call-history): expose transcript_available on the row DTO"
```

---

### Task 2: Frontend — `CallHistoryRow` type field + `TranscriptDialog` component

**Files:**
- Modify: `vera-frontend/src/lib/api/calls.ts` (`CallHistoryRow` type)
- Create: `vera-frontend/src/components/monitoring/TranscriptDialog.tsx`
- Test: `vera-frontend/src/components/monitoring/TranscriptDialog.test.tsx`

**Interfaces:**
- Consumes: `CallTranscript({ callId })` from `@/components/monitoring/CallTranscript`; `formatDateTime` from `@/lib/patient-forms/display`; `Dialog`/`DialogContent`/`DialogHeader`/`DialogTitle` from `@/components/ui/dialog`.
- Produces: `TranscriptDialog({ call, onOpenChange })` and `type TranscriptDialogCall = { id: string; patient_name: string | null; created_at: string }`; `CallHistoryRow.transcript_available: boolean`.

- [ ] **Step 1: Add the type field**

In `vera-frontend/src/lib/api/calls.ts`, in `export type CallHistoryRow = { ... }`, add after `recording_available: boolean`:

```ts
  recording_available: boolean
  /** True when a stored transcript exists for this call — gates the "View transcript" trigger. */
  transcript_available: boolean
```

- [ ] **Step 2: Write the failing test**

Create `vera-frontend/src/components/monitoring/TranscriptDialog.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

// Stub CallTranscript so the test doesn't open the SSE.
vi.mock("@/components/monitoring/CallTranscript", () => ({
  CallTranscript: ({ callId }: { callId: string }) => <div>transcript:{callId}</div>,
}))

import { TranscriptDialog } from "./TranscriptDialog"

describe("TranscriptDialog", () => {
  it("renders the title and transcript for the given call", () => {
    render(
      <TranscriptDialog
        call={{ id: "c1", patient_name: "Jane Doe", created_at: "2026-07-21T12:00:00Z" }}
        onOpenChange={() => {}}
      />,
    )
    expect(screen.getByText(/Transcript — Jane Doe/)).toBeTruthy()
    expect(screen.getByText("transcript:c1")).toBeTruthy()
  })

  it("renders nothing when call is null (closed)", () => {
    render(<TranscriptDialog call={null} onOpenChange={() => {}} />)
    expect(screen.queryByText(/Transcript —/)).toBeNull()
  })
})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd vera-frontend && npx vitest run src/components/monitoring/TranscriptDialog.test.tsx`
Expected: FAIL — cannot resolve `./TranscriptDialog`.

- [ ] **Step 4: Create the component**

Create `vera-frontend/src/components/monitoring/TranscriptDialog.tsx`:

```tsx
import { CallTranscript } from "@/components/monitoring/CallTranscript"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { formatDateTime } from "@/lib/patient-forms/display"

export type TranscriptDialogCall = {
  id: string
  patient_name: string | null
  created_at: string
}

/** Read-only popup showing a completed call's transcript, keyed by call id so a
 *  different call remounts CallTranscript (and its session-only turn state). */
export function TranscriptDialog({
  call,
  onOpenChange,
}: {
  call: TranscriptDialogCall | null
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={call !== null} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton
        className="flex max-h-[85vh] flex-col gap-0 p-0 sm:max-w-2xl"
      >
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle>
            Transcript — {call?.patient_name || "—"}
            {call ? ` · ${formatDateTime(call.created_at)}` : ""}
          </DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {call && <CallTranscript key={call.id} callId={call.id} />}
        </div>
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd vera-frontend && npx vitest run src/components/monitoring/TranscriptDialog.test.tsx`
Expected: PASS (both cases).

- [ ] **Step 6: Typecheck + lint the new/changed files**

Run: `cd vera-frontend && npx tsc -b && npx eslint src/components/monitoring/TranscriptDialog.tsx src/components/monitoring/TranscriptDialog.test.tsx src/lib/api/calls.ts`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add vera-frontend/src/lib/api/calls.ts \
        vera-frontend/src/components/monitoring/TranscriptDialog.tsx \
        vera-frontend/src/components/monitoring/TranscriptDialog.test.tsx
git commit -m "feat(call-history): TranscriptDialog + transcript_available type field"
```

---

### Task 3: Frontend — "View transcript" trigger on the row + page wiring

**Files:**
- Modify: `vera-frontend/src/pages/CallHistory.tsx` (`CallRow` component ~line 257; page component state + row mapping ~lines 66–212)
- Test: `vera-frontend/src/pages/CallHistory.test.tsx`

**Interfaces:**
- Consumes: `TranscriptDialog`, `type TranscriptDialogCall` from `@/components/monitoring/TranscriptDialog`; `CallHistoryRow.transcript_available`.
- Produces: `CallRow` gains a required prop `onViewTranscript: () => void`.

- [ ] **Step 1: Write the failing tests (and update the shared test helpers)**

In `vera-frontend/src/pages/CallHistory.test.tsx`:

1. Add `transcript_available: false,` to the `row()` factory defaults (after `recording_available: false,`).
2. Add `onViewTranscript={noop}` to the `<CallRow .../>` inside the `render` helper.
3. Add these tests inside `describe("CallRow", ...)`:

```tsx
  it("shows the View transcript control when a transcript is available", () => {
    expect(render(row({ transcript_available: true }), true)).toContain("View transcript")
  })

  it("hides the View transcript control when no transcript is available", () => {
    expect(render(row({ transcript_available: false }), true)).not.toContain("View transcript")
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-frontend && npx vitest run src/pages/CallHistory.test.tsx`
Expected: FAIL — the two new tests fail (no "View transcript" rendered); a TS error on the missing `onViewTranscript` prop is also expected until Step 3.

- [ ] **Step 3: Add the button + prop to `CallRow`**

In `CallHistory.tsx`, extend `CallRow`'s props (both the destructure and the type object) with `onViewTranscript: () => void`:

```tsx
export function CallRow({
  call: c,
  canPlay,
  playerOpen,
  onOpenForm,
  onTogglePlayer,
  onViewTranscript,
}: {
  call: CallHistoryRow
  canPlay: boolean
  playerOpen: boolean
  onOpenForm: () => void
  onTogglePlayer: () => void
  onViewTranscript: () => void
}) {
```

Immediately after the existing `{canPlay && c.recording_available && ( ... )}` "Play recording" block (inside the same `<div className="flex items-center gap-2">`), add:

```tsx
            {c.transcript_available && (
              <button
                type="button"
                className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                onClick={(e) => {
                  e.stopPropagation()
                  onViewTranscript()
                }}
              >
                View transcript
              </button>
            )}
```

- [ ] **Step 4: Wire the dialog into the page component**

At the top of `CallHistory.tsx`, add the import:

```tsx
import { TranscriptDialog, type TranscriptDialogCall } from "@/components/monitoring/TranscriptDialog"
```

In the page component (where `openPlayerId` state lives, ~line 75), add:

```tsx
  const [transcriptCall, setTranscriptCall] = useState<TranscriptDialogCall | null>(null)
```

In the `rows.map((c) => <CallRow ... />)`, add the prop:

```tsx
                onViewTranscript={() =>
                  setTranscriptCall({
                    id: c.id,
                    patient_name: c.patient_name,
                    created_at: c.created_at,
                  })
                }
```

After the table/pagination block returned by the page component (a sibling element, before the component's closing tag), render the dialog:

```tsx
      <TranscriptDialog
        call={transcriptCall}
        onOpenChange={(open) => {
          if (!open) setTranscriptCall(null)
        }}
      />
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd vera-frontend && npx vitest run src/pages/CallHistory.test.tsx`
Expected: PASS (new and existing CallRow tests).

- [ ] **Step 6: Run the full frontend gate**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: all green. (tsc will confirm every `CallHistoryRow` construction includes `transcript_available`; fix any test fixture it flags.)

- [ ] **Step 7: Commit**

```bash
git add vera-frontend/src/pages/CallHistory.tsx vera-frontend/src/pages/CallHistory.test.tsx
git commit -m "feat(call-history): View transcript trigger opens the transcript popup"
```

---

## Notes for the implementer

- **Empty transcript edge:** the trigger is gated on `transcript_available`, so an open dialog effectively always has turns. If a replay yields zero turns, `CallTranscript` renders an empty feed (acceptable); no extra placeholder is required for v1.
- **Do not** add a `usePermission` gate on the "View transcript" button — page access already implies `calls:read`, and the `/calls/{id}/events` endpoint re-enforces it.
- **Do not** touch the recording button/flow; `View transcript` sits beside it as an independent control.
- After all three tasks, run `/simplify` on the diff, then re-run both gates before opening the PR (repo CLAUDE.md rule).
