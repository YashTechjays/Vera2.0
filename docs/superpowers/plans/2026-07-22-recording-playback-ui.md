# Recording Playback in the IBV Call History Tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user play a call attempt's recording from the IBV form modal's Call History tab, advertising a recording only to callers the backend will actually let fetch it.

**Architecture:** Backend gains a read-only enrichment — a shared owner-or-published visibility predicate plus a `recording` field on the existing `GET /patient-forms/{id}/calls` DTO (no probing of the playback endpoint, which would mint signed URLs and write `RECORDING_ACCESSED` audit rows just to render a list). Frontend gains a `getRecordingPlayback` wrapper, a `RecordingPlayer` component (explicit-click fetch of the 10-minute signed URL, native `<audio>`, one expired-URL refetch), and gating in `CallHistoryTab` (`recording === "available"` AND `usePermission("recordings:read")`, one player open at a time).

**Tech Stack:** FastAPI + SQLAlchemy async + pytest (backend, `mypy --strict`, ruff); React + TypeScript + vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-07-22-recording-playback-ui-design.md`

## Global Constraints

- Branch: `feat/recording-playback-ui` (worktree `.claude/worktrees/recording-playback-ui`).
- Backend gate: `just check` from `vera-backend/` — run verbatim, never a subset.
- Frontend gate: `npx tsc -b` + `npx eslint .` + `NODE_OPTIONS=--no-experimental-webstorage npm test` + `npm run build` from `vera-frontend/`.
- The playback endpoint (`GET /calls/{call_id}/recording`), its `recordings:read` permission, and the owner-or-published rule are NOT changed.
- New backend tests live under `vera-backend/tests/{unit,integration}/…` (CI testpaths), co-located `*.test.ts(x)` on the frontend.
- Backend responses use the `ResponseModel` envelope; FE errors are inline `role="alert"` text, no toasts.

---

### Task 1: Shared call-visibility predicate

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/services/call_visibility.py`
- Create: `vera-backend/tests/unit/services/test_call_visibility.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py:150-158` (`_call_hidden_from`)

**Interfaces:**
- Produces: `call_hidden_from(initiated_by_id: UUID | None, published: bool, user_id: UUID | None) -> bool` — used by Task 3 and by `calls.py`.

- [ ] **Step 1: Write the failing test**

`vera-backend/tests/unit/services/test_call_visibility.py`:

```python
"""The owner-or-published visibility predicate shared by the playback endpoint
and the call-attempt DTO enrichment (spec 2026-07-22-recording-playback-ui)."""

from uuid import uuid4

from vera_core.services.call_visibility import call_hidden_from

OWNER = uuid4()
OTHER = uuid4()


def test_owner_always_sees_their_call() -> None:
    assert call_hidden_from(OWNER, False, OWNER) is False
    assert call_hidden_from(OWNER, True, OWNER) is False


def test_non_owner_hidden_until_published() -> None:
    assert call_hidden_from(OWNER, False, OTHER) is True
    assert call_hidden_from(OWNER, True, OTHER) is False


def test_ownerless_call_is_tenant_visible() -> None:
    assert call_hidden_from(None, False, OTHER) is False
    assert call_hidden_from(None, False, None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `vera-backend/`): `uv run pytest tests/unit/services/test_call_visibility.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vera_core.services.call_visibility'`

- [ ] **Step 3: Write the module**

`vera-backend/packages/vera_core/src/vera_core/services/call_visibility.py`:

```python
"""Owner-or-published call visibility (call-recording-persistence spec decision 6).

One predicate, two consumers — the playback endpoint's 404 gate
(`api/v1/calls.py::_call_hidden_from`) and the call-attempt DTO's `recording`
enrichment (`api/v1/patient_forms.py`) — so the gates can never diverge.
"""

from uuid import UUID


def call_hidden_from(
    initiated_by_id: UUID | None, published: bool, user_id: UUID | None
) -> bool:
    """Whether *user_id* must NOT see the call (the caller maps this to the same
    404 as a missing row, so a private call is never revealed by enumeration).
    A non-owner sees it only when it is published or ownerless."""
    if initiated_by_id == user_id:
        return False
    return initiated_by_id is not None and not published
```

- [ ] **Step 4: Rewire `_call_hidden_from` in calls.py to delegate**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py`, replace the body of `_call_hidden_from` (keep the function — ~15 call sites use its `(call, user_id)` shape):

```python
def _call_hidden_from(call: Call, user_id: UUID | None) -> bool:
    """Whether *user_id* must NOT see this call (→ the same 404 as a missing row,
    so a private call is never revealed by enumeration).

    A non-owner sees it only when it is published or ownerless. Shared by
    join-token, the event stream, end, and (via vera_core.services.call_visibility)
    the call-attempt recording enrichment, so the visibility gates never diverge.
    """
    return call_hidden_from(call.initiated_by_id, call.published, user_id)
```

Add the import alongside the other `vera_core.services` imports in calls.py:

```python
from vera_core.services.call_visibility import call_hidden_from
```

- [ ] **Step 5: Run tests and the existing playback/visibility suites**

Run: `uv run pytest tests/unit/services/test_call_visibility.py tests/unit/http/test_recording_playback.py tests/integration/control_plane/test_calls.py -v`
Expected: all PASS (integration file skips without local Postgres — fine, `just check` covers it).

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/services/call_visibility.py tests/unit/services/test_call_visibility.py apps/control_plane/src/control_plane/api/v1/calls.py
git commit -m "refactor: extract owner-or-published call visibility into vera_core"
```

---

### Task 2: `CallAttempt` recording-availability enrichment

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/services/call_provenance.py` (`CallAttempt` dataclass ~line 43, `load_call_attempts` ~line 63)
- Modify: `vera-backend/tests/integration/test_call_provenance.py` (`test_attempts_lineage_and_diffs`, fixture `two_call_form_ctx`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `CallAttempt` gains `initiated_by_id: UUID | None = None`, `published: bool = False`, `recording_available: bool = False` (defaults keep the workbook/export constructors compiling). `load_call_attempts` fills all three.

- [ ] **Step 1: Extend the integration test (failing first)**

In `vera-backend/tests/integration/test_call_provenance.py`:

Add to the imports:

```python
from vera_core.models.enums import RecordingStatus
from vera_core.models.transcript import Recording
```

In the `two_call_form_ctx` fixture's seeding block (same `session.begin()` that adds the `CallFormSnapshot` rows), seed one AVAILABLE recording on call2 and a PENDING one on call1:

```python
            session.add(
                Recording(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    call_id=call1_id,
                    gcs_uri=f"gs://bucket/recordings/{tenant_id}/{call1_id}.ogg",
                    status=RecordingStatus.PENDING.value,
                )
            )
            session.add(
                Recording(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    call_id=call2_id,
                    gcs_uri=f"gs://bucket/recordings/{tenant_id}/{call2_id}.ogg",
                    status=RecordingStatus.AVAILABLE.value,
                )
            )
```

Add `Recording` to the fixture's teardown deletes, BEFORE the `Call` delete (FK order):

```python
                await session.execute(
                    text("DELETE FROM recording WHERE call_id IN (:c1, :c2)").bindparams(
                        c1=call1_id, c2=call2_id
                    )
                )
```

Extend `test_attempts_lineage_and_diffs` with assertions after the existing ones:

```python
    assert attempts[0].recording_available is False  # PENDING is not playable
    assert attempts[1].recording_available is True
    assert attempts[0].published is False
    assert attempts[0].initiated_by_id is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_call_provenance.py -v` (needs `just up` + `just migrate` once)
Expected: FAIL — `AttributeError: 'CallAttempt' object has no attribute 'recording_available'`

- [ ] **Step 3: Implement the enrichment**

In `vera-backend/packages/vera_core/src/vera_core/services/call_provenance.py`:

Add imports:

```python
from vera_core.models.enums import AnswerSource, RecordingStatus
from vera_core.models.transcript import Recording
```

(`AnswerSource` is already imported — fold `RecordingStatus` into that line.)

Extend the dataclass (defaults last, so `CallAttempt(...)` construction in `forms/export.py` tests keeps working):

```python
@dataclass(frozen=True)
class CallAttempt:
    id: UUID
    attempt: int
    mode: str
    status: str
    created_at: datetime
    retry_of: UUID | None
    changed_paths: list[str]
    # Visibility inputs + playability for the caller-aware `recording` DTO field
    # (vera_core stays caller-agnostic; the API layer applies call_hidden_from).
    initiated_by_id: UUID | None = None
    published: bool = False
    recording_available: bool = False
```

In `load_call_attempts`, widen the calls select:

```python
        select(
            Call.id,
            Call.mode,
            Call.current_status,
            Call.created_at,
            Call.initiated_by_id,
            Call.published,
        )
```

After the `snapshots` query, add the availability set:

```python
    playable = {
        row.call_id
        for row in (
            await session.execute(
                select(Recording.call_id).where(
                    Recording.call_id.in_(ids),
                    Recording.status == RecordingStatus.AVAILABLE.value,
                )
            )
        ).all()
    }
```

And in the `CallAttempt(...)` construction inside the loop, add:

```python
                initiated_by_id=c.initiated_by_id,
                published=c.published,
                recording_available=c.id in playable,
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_call_provenance.py tests/unit/forms/test_export_workbook.py -v`
Expected: PASS (workbook test proves the dataclass defaults kept old constructors valid).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/services/call_provenance.py tests/integration/test_call_provenance.py
git commit -m "feat: expose recording availability + visibility inputs on CallAttempt"
```

---

### Task 3: `recording` field on the call-attempt DTO

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`CallAttemptView` ~line 425, `_call_attempt_view` ~line 435, `list_form_calls` ~line 728)
- Create: `vera-backend/tests/integration/control_plane/test_form_calls_recording.py`

**Interfaces:**
- Consumes: Task 1 `call_hidden_from`, Task 2 `CallAttempt.{initiated_by_id,published,recording_available}`.
- Produces: `CallAttemptView.recording: Literal["available"] | None` — the FE (Task 4) keys off `"available"`.

- [ ] **Step 1: Write the failing integration test**

`vera-backend/tests/integration/control_plane/test_form_calls_recording.py` — mirrors `test_form_provenance.py`'s fixture pattern (RBACWorld tenant, superuser seed engine, FK-ordered teardown):

```python
"""GET /patient-forms/{id}/calls `recording` enrichment: advertised only when an
AVAILABLE recording exists AND the call is visible to the caller (owner-or-
published — the playback endpoint's exact gate), so the UI never renders a
play button that would 404."""

from collections.abc import AsyncGenerator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.call import Call
from vera_core.models.enums import CallStatus, FormStatus, InsuranceType, RecordingStatus
from vera_core.models.patient_form import PatientForm
from vera_core.models.transcript import Recording

pytestmark = pytest.mark.integration


@pytest.fixture
async def recording_form_id(
    database_url: str, rbac_world: RBACWorld
) -> AsyncGenerator[UUID]:
    """Three completed calls on one form, owned by the supervisor:
    - call1: unpublished + AVAILABLE recording  → owner-only playback
    - call2: published + AVAILABLE recording    → tenant-visible playback
    - call3: unpublished + PENDING recording    → playable by nobody
    """
    tenant_id = rbac_world.tenant_id
    form_id = uuid7()
    call_ids = [uuid7(), uuid7(), uuid7()]
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    schema_id_to_delete: UUID | None = None
    try:
        async with sm() as session, session.begin():
            existing = (
                await session.execute(
                    select(FormSchema).where(
                        FormSchema.insurance_type == InsuranceType.DISEASE_ONLY.value
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                fs = FormSchema(
                    id=uuid7(),
                    insurance_type=InsuranceType.DISEASE_ONLY.value,
                    name="Recording Test Schema",
                )
                session.add(fs)
                await session.flush()
                schema_id = fs.id
                schema_id_to_delete = fs.id
            else:
                schema_id = existing.id
            session.add(
                SchemaVersion(
                    id=schema_version_id,
                    form_schema_id=schema_id,
                    version=994,
                    schema_json={"dsl_version": "1.0", "sections": {}},
                )
            )
            session.add(
                PatientForm(
                    id=form_id,
                    tenant_id=tenant_id,
                    schema_version_id=schema_version_id,
                    status=FormStatus.COMPLETED.value,
                )
            )
            published_flags = [False, True, False]
            for call_id, published in zip(call_ids, published_flags, strict=True):
                session.add(
                    Call(
                        id=call_id,
                        tenant_id=tenant_id,
                        form_id=form_id,
                        mode="full",
                        current_status=CallStatus.COMPLETED.value,
                        initiated_by_id=rbac_world.supervisor_id,
                        published=published,
                    )
                )
            statuses = [
                RecordingStatus.AVAILABLE.value,
                RecordingStatus.AVAILABLE.value,
                RecordingStatus.PENDING.value,
            ]
            for call_id, status in zip(call_ids, statuses, strict=True):
                session.add(
                    Recording(
                        id=uuid7(),
                        tenant_id=tenant_id,
                        call_id=call_id,
                        gcs_uri=f"gs://bucket/recordings/{tenant_id}/{call_id}.ogg",
                        status=status,
                    )
                )
        yield form_id
    finally:
        async with sm() as session, session.begin():
            await session.execute(
                text("DELETE FROM recording WHERE call_id = ANY(:ids)").bindparams(
                    ids=call_ids
                )
            )
            await session.execute(
                text("DELETE FROM call WHERE form_id = :f").bindparams(f=form_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE id = :f").bindparams(f=form_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sv").bindparams(
                    sv=schema_version_id
                )
            )
            if schema_id_to_delete is not None:
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fs").bindparams(
                        fs=schema_id_to_delete
                    )
                )
        await engine.dispose()


async def _recordings_by_attempt(
    client: httpx.AsyncClient, form_id: UUID, token: str
) -> list[str | None]:
    resp = await client.get(
        f"/api/v1/patient-forms/{form_id}/calls",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return [c["recording"] for c in resp.json()["data"]]


@pytest.mark.asyncio
async def test_owner_sees_available_on_owned_and_published_calls(
    client: httpx.AsyncClient, rbac_world: RBACWorld, recording_form_id: UUID
) -> None:
    assert await _recordings_by_attempt(
        client, recording_form_id, rbac_world.supervisor_token
    ) == ["available", "available", None]


@pytest.mark.asyncio
async def test_non_owner_sees_available_only_on_published_call(
    client: httpx.AsyncClient, rbac_world: RBACWorld, recording_form_id: UUID
) -> None:
    assert await _recordings_by_attempt(
        client, recording_form_id, rbac_world.admin_token
    ) == [None, "available", None]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/control_plane/test_form_calls_recording.py -v`
Expected: FAIL — `KeyError: 'recording'` (DTO doesn't carry the field yet).

- [ ] **Step 3: Implement the DTO enrichment**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`:

Add imports (`Literal` to the existing `typing` import if present, else add it; the predicate next to the other `vera_core.services` imports):

```python
from typing import Literal

from vera_core.services.call_visibility import call_hidden_from
```

Extend the DTO and its builder:

```python
class CallAttemptView(BaseModel):
    id: UUID
    attempt: int
    mode: str
    status: str
    created_at: datetime
    retry_of: UUID | None
    changed_paths: list[str]
    # "available" only when the recording is playable AND the call passes the
    # playback endpoint's owner-or-published gate for THIS caller — the UI must
    # never advertise a recording the caller can't fetch.
    recording: Literal["available"] | None


def _call_attempt_view(a: CallAttempt, caller_id: UUID | None) -> CallAttemptView:
    visible = not call_hidden_from(a.initiated_by_id, a.published, caller_id)
    return CallAttemptView(
        id=a.id,
        attempt=a.attempt,
        mode=a.mode,
        status=a.status,
        created_at=a.created_at,
        retry_of=a.retry_of,
        changed_paths=a.changed_paths,
        recording="available" if (a.recording_available and visible) else None,
    )
```

In `list_form_calls`, update the return line:

```python
    return ok([_call_attempt_view(a, caller.user_id) for a in attempts])
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/control_plane/test_form_calls_recording.py tests/integration/control_plane/test_form_provenance.py -v`
Expected: PASS — including the pre-existing `test_calls_timeline` (its calls have `initiated_by_id=None` → ownerless → visible, `recording` is simply `null`).

- [ ] **Step 5: Run the full backend gate**

Run: `just check`
Expected: lint + format + mypy --strict + full pytest all green.

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/patient_forms.py tests/integration/control_plane/test_form_calls_recording.py
git commit -m "feat: caller-aware recording availability on the call-attempt DTO"
```

---

### Task 4: Frontend API wrapper + types

**Files:**
- Modify: `vera-frontend/src/lib/api/calls.ts`
- Modify: `vera-frontend/src/lib/api/calls.test.ts`
- Modify: `vera-frontend/src/lib/patient-forms/types.ts` (`CallAttempt`, ~line 32)

**Interfaces:**
- Produces: `getRecordingPlayback(callId: string): Promise<RecordingPlayback>` with `RecordingPlayback = { url: string; expires_at: string }`; `CallAttempt.recording: "available" | null`. Tasks 5–6 consume both.

- [ ] **Step 1: Write the failing test**

Append to `vera-frontend/src/lib/api/calls.test.ts` (inside the existing describe/mock setup — `apiRequest` is already factory-mocked in this file):

```ts
it("getRecordingPlayback fetches the signed playback URL", async () => {
  const playback = { url: "https://storage.example/sig", expires_at: "2026-07-22T01:00:00Z" }
  vi.mocked(apiRequest).mockResolvedValueOnce(playback)

  await expect(getRecordingPlayback("call-1")).resolves.toEqual(playback)
  expect(apiRequest).toHaveBeenCalledWith("/calls/call-1/recording")
})
```

Add `getRecordingPlayback` to the file's import from `"./calls"`.

- [ ] **Step 2: Run to verify it fails**

Run (from `vera-frontend/`): `npx vitest run src/lib/api/calls.test.ts`
Expected: FAIL — `getRecordingPlayback` is not exported.

- [ ] **Step 3: Implement wrapper + type**

Append to `vera-frontend/src/lib/api/calls.ts`:

```ts
/** Short-lived signed playback URL for a call's recording (GET /calls/{id}/recording).
 *  Every fetch is audited server-side (RECORDING_ACCESSED) — call it only on an
 *  explicit user action, never to probe availability (the attempt DTO carries that). */
export type RecordingPlayback = { url: string; expires_at: string }

export async function getRecordingPlayback(callId: string): Promise<RecordingPlayback> {
  return apiRequest<RecordingPlayback>(`/calls/${encodeURIComponent(callId)}/recording`)
}
```

In `vera-frontend/src/lib/patient-forms/types.ts`, add to `CallAttempt`:

```ts
  /** "available" when the caller may fetch this attempt's recording (AVAILABLE
   *  + owner-or-published); null otherwise — null means render no play control. */
  recording: "available" | null
```

- [ ] **Step 4: Run to verify it passes (and nothing else breaks)**

Run: `npx vitest run src/lib/api/calls.test.ts src/lib/patient-forms/api.test.ts && npx tsc -b`
Expected: PASS. If `tsc` flags fixtures constructing `CallAttempt` without `recording` (e.g. in tests), add `recording: null` to those fixture objects.

- [ ] **Step 5: Commit**

```bash
git add src/lib/api/calls.ts src/lib/api/calls.test.ts src/lib/patient-forms/types.ts
git commit -m "feat: recording playback API wrapper + CallAttempt.recording type"
```

---

### Task 5: RecordingPlayer component

**Files:**
- Create: `vera-frontend/src/components/ibv/RecordingPlayer.tsx`
- Create: `vera-frontend/src/components/ibv/RecordingPlayer.test.tsx`

**Interfaces:**
- Consumes: Task 4 `getRecordingPlayback`.
- Produces: `<RecordingPlayer callId={string} />` — mounted by `CallHistoryTab` when the user expands an attempt's player (mount = the explicit click; the component fetches on mount).

- [ ] **Step 1: Write the failing test**

`vera-frontend/src/components/ibv/RecordingPlayer.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { RecordingPlayer, clearPlaybackCache } from "./RecordingPlayer"
import { getRecordingPlayback } from "@/lib/api/calls"

vi.mock("@/lib/api/calls", () => ({ getRecordingPlayback: vi.fn() }))

const FRESH = { url: "https://storage.example/sig", expires_at: "2999-01-01T00:00:00Z" }

describe("RecordingPlayer", () => {
  beforeEach(() => {
    vi.resetAllMocks()
    clearPlaybackCache()
  })

  it("fetches the signed URL on mount and renders the audio element", async () => {
    vi.mocked(getRecordingPlayback).mockResolvedValueOnce(FRESH)
    render(<RecordingPlayer callId="c1" />)

    const audio = await screen.findByLabelText<HTMLAudioElement>("Call recording")
    expect(audio.src).toBe(FRESH.url)
    expect(getRecordingPlayback).toHaveBeenCalledWith("c1")
  })

  it("reuses a cached unexpired URL instead of refetching (audit noise)", async () => {
    vi.mocked(getRecordingPlayback).mockResolvedValue(FRESH)
    const first = render(<RecordingPlayer callId="c1" />)
    await screen.findByLabelText("Call recording")
    first.unmount()

    render(<RecordingPlayer callId="c1" />)
    await screen.findByLabelText("Call recording")
    expect(getRecordingPlayback).toHaveBeenCalledTimes(1)
  })

  it("shows an inline alert when the fetch fails", async () => {
    vi.mocked(getRecordingPlayback).mockRejectedValueOnce(new Error("409"))
    render(<RecordingPlayer callId="c1" />)

    const alert = await screen.findByRole("alert")
    expect(alert.textContent).toContain("Recording unavailable")
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/components/ibv/RecordingPlayer.test.tsx`
Expected: FAIL — module `./RecordingPlayer` not found.

- [ ] **Step 3: Implement the component**

`vera-frontend/src/components/ibv/RecordingPlayer.tsx`:

```tsx
import { useEffect, useRef, useState } from "react"

import { getRecordingPlayback, type RecordingPlayback } from "@/lib/api/calls"

/** Signed URLs are valid ~10 min; cache per call so collapse/expand inside the
 *  TTL doesn't refetch — every fetch is a server-audited disclosure. */
const playbackCache = new Map<string, RecordingPlayback>()

/** Test seam: the cache is module-scoped so it survives unmount by design. */
export function clearPlaybackCache(): void {
  playbackCache.clear()
}

function fresh(p: RecordingPlayback | undefined): p is RecordingPlayback {
  return !!p && new Date(p.expires_at).getTime() > Date.now()
}

/** Inline audio player for one call attempt's recording. Mounted only on the
 *  user's explicit click (CallHistoryTab), so the audited URL fetch is always
 *  user-initiated. On a mid-playback error past expiry it refetches once and
 *  resumes; a second failure surfaces the inline error. */
export function RecordingPlayer({ callId }: { callId: string }) {
  const [playback, setPlayback] = useState<RecordingPlayback | null>(
    () => (fresh(playbackCache.get(callId)) ? (playbackCache.get(callId) ?? null) : null),
  )
  const [failed, setFailed] = useState(false)
  const retried = useRef(false)
  const resumeAt = useRef(0)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    if (playback) return
    let cancelled = false
    getRecordingPlayback(callId)
      .then((p) => {
        playbackCache.set(callId, p)
        if (!cancelled) setPlayback(p)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetch once per mount
  }, [callId])

  if (failed)
    return (
      <p className="mt-2 text-xs text-destructive" role="alert">
        Recording unavailable.
      </p>
    )
  if (!playback) return <p className="mt-2 text-xs text-muted-foreground">Loading recording…</p>

  const handleError = () => {
    // Expired mid-listen (long pause, late seek): refetch once, resume position.
    if (!retried.current && !fresh(playback)) {
      retried.current = true
      resumeAt.current = audioRef.current?.currentTime ?? 0
      playbackCache.delete(callId)
      getRecordingPlayback(callId)
        .then((p) => {
          playbackCache.set(callId, p)
          setPlayback(p)
        })
        .catch(() => setFailed(true))
      return
    }
    setFailed(true)
  }

  return (
    <audio
      ref={audioRef}
      className="mt-2 w-full"
      controls
      autoPlay
      preload="none"
      aria-label="Call recording"
      src={playback.url}
      onError={handleError}
      onLoadedMetadata={() => {
        if (resumeAt.current > 0 && audioRef.current) {
          audioRef.current.currentTime = resumeAt.current
          resumeAt.current = 0
        }
      }}
    />
  )
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npx vitest run src/components/ibv/RecordingPlayer.test.tsx`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/components/ibv/RecordingPlayer.tsx src/components/ibv/RecordingPlayer.test.tsx
git commit -m "feat: inline RecordingPlayer with TTL-cached signed URL + one expiry retry"
```

---

### Task 6: CallHistoryTab integration + full gates

**Files:**
- Modify: `vera-frontend/src/components/ibv/CallHistoryTab.tsx`
- Create: `vera-frontend/src/components/ibv/CallHistoryTab.test.tsx`

**Interfaces:**
- Consumes: Task 4 `CallAttempt.recording`, Task 5 `RecordingPlayer`, existing `usePermission` (`@/lib/auth/permissions`).

- [ ] **Step 1: Write the failing test**

`vera-frontend/src/components/ibv/CallHistoryTab.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { CallHistoryTab } from "./CallHistoryTab"
import { getPatientFormCalls } from "@/lib/patient-forms/api"
import { usePermission } from "@/lib/auth/permissions"
import type { CallAttempt } from "@/lib/patient-forms/types"

vi.mock("@/lib/patient-forms/api", () => ({ getPatientFormCalls: vi.fn() }))
vi.mock("@/lib/auth/permissions", () => ({ usePermission: vi.fn() }))
vi.mock("./IbvProvider", () => ({ useIbv: () => ({ formId: "form-1" }) }))
vi.mock("./RecordingPlayer", () => ({
  RecordingPlayer: ({ callId }: { callId: string }) => <div data-testid={`player-${callId}`} />,
}))

const attempt = (over: Partial<CallAttempt>): CallAttempt => ({
  id: "c1",
  attempt: 1,
  mode: "full",
  status: "completed",
  created_at: "2026-07-21T12:00:00Z",
  retry_of: null,
  changed_paths: [],
  recording: null,
  ...over,
})

describe("CallHistoryTab recording playback", () => {
  beforeEach(() => vi.resetAllMocks())

  it("shows a play control only for attempts with an available recording", async () => {
    vi.mocked(usePermission).mockReturnValue(true)
    vi.mocked(getPatientFormCalls).mockResolvedValueOnce([
      attempt({ id: "c1", recording: "available" }),
      attempt({ id: "c2", attempt: 2, recording: null }),
    ])
    render(<CallHistoryTab />)

    expect(await screen.findAllByRole("button", { name: "Play recording" })).toHaveLength(1)
  })

  it("hides play controls without the recordings:read permission", async () => {
    vi.mocked(usePermission).mockReturnValue(false)
    vi.mocked(getPatientFormCalls).mockResolvedValueOnce([
      attempt({ id: "c1", recording: "available" }),
    ])
    render(<CallHistoryTab />)

    await screen.findByText("Attempt 1")
    expect(usePermission).toHaveBeenCalledWith("recordings:read")
    expect(screen.queryByRole("button", { name: "Play recording" })).toBeNull()
  })

  it("opens one player at a time", async () => {
    vi.mocked(usePermission).mockReturnValue(true)
    vi.mocked(getPatientFormCalls).mockResolvedValueOnce([
      attempt({ id: "c1", recording: "available" }),
      attempt({ id: "c2", attempt: 2, recording: "available" }),
    ])
    const user = userEvent.setup()
    render(<CallHistoryTab />)

    const buttons = await screen.findAllByRole("button", { name: "Play recording" })
    await user.click(buttons[0])
    expect(screen.getByTestId("player-c1")).toBeDefined()

    await user.click(buttons[1])
    expect(screen.getByTestId("player-c2")).toBeDefined()
    expect(screen.queryByTestId("player-c1")).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/components/ibv/CallHistoryTab.test.tsx`
Expected: FAIL — no "Play recording" buttons rendered.

- [ ] **Step 3: Wire the player into CallHistoryTab**

In `vera-frontend/src/components/ibv/CallHistoryTab.tsx`:

Add imports:

```tsx
import { usePermission } from "@/lib/auth/permissions"
import { RecordingPlayer } from "./RecordingPlayer"
```

Add state + the permission next to the existing hooks:

```tsx
  const canPlayRecordings = usePermission("recordings:read")
  const [openPlayerId, setOpenPlayerId] = useState<string | null>(null)
```

Inside the attempt card (after the changed-paths `<button>`/`<ul>` block, still within the card `<div>`), add:

```tsx
            {canPlayRecordings && a.recording === "available" && (
              <button
                type="button"
                className="mt-1 block text-xs text-muted-foreground underline-offset-2 hover:underline"
                onClick={() => setOpenPlayerId((id) => (id === a.id ? null : a.id))}
              >
                {openPlayerId === a.id ? "Hide recording" : "Play recording"}
              </button>
            )}
            {openPlayerId === a.id && <RecordingPlayer callId={a.id} />}
```

(One `openPlayerId` means opening another attempt collapses the first — no overlapping audio.)

- [ ] **Step 4: Run to verify it passes**

Run: `NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/components/ibv/CallHistoryTab.test.tsx`
Expected: 3 PASS. (Note: the toggled label becomes "Hide recording" after click — the third test's second click uses `buttons[1]`, which still reads "Play recording"; if the query goes stale re-query after the first click.)

- [ ] **Step 5: Run every gate**

```bash
cd vera-frontend && npx tsc -b && npx eslint . && NODE_OPTIONS=--no-experimental-webstorage npm test && npm run build
cd ../vera-backend && just check
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/components/ibv/CallHistoryTab.tsx src/components/ibv/CallHistoryTab.test.tsx
git commit -m "feat: recording playback in the IBV call-history tab"
```

---

### Task 7: Simplify pass + verification (repo-mandated)

- [ ] **Step 1:** Run the `code-simplifier` agent on the change ("simplify code") per repo `CLAUDE.md`.
- [ ] **Step 2:** Re-run both gates (`just check`; `tsc -b` + `eslint` + `vitest` + `build`).
- [ ] **Step 3:** Commit any refinements.
