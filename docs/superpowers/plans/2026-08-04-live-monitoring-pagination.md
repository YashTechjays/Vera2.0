# Live Monitoring Pagination (VR2-160) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paginate the Live Monitoring page — server-side pages for the Completed tab (all history reachable, not just the 50 newest), client-side 20-row windows for Active/Critical.

**Architecture:** `GET /calls?scope=history` gains `page`/`page_size` and returns the same `{items, page, page_size, total}` envelope `/call-history` uses (window-count query + bare-count fallback). The live scope stays an unbounded array so the notification deep-link, SSE-ended pinning, and stat-card agreement keep working; the page slices it locally. One shared `PaginationFooter` component (extracted from Call History) renders the controls on both pages.

**Tech Stack:** FastAPI + SQLAlchemy async + pytest (backend); React + TypeScript + vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-08-04-live-monitoring-pagination-design.md`

## Global Constraints

- Branch: `fix/live-monitoring-pagination` (already created off `origin/dev`).
- Commit gate (Azad's workflow): at every "Commit" step, STAGE the changes (`git add`) and STOP — Azad reviews the staged diff in his IDE and gives the go-ahead before any `git commit` runs. Never commit unreviewed work.
- No `Co-Authored-By: Claude` trailers in any commit message (team git rule).
- Backend gate: `just check` run verbatim (ruff check + format --check + mypy --strict + pytest) from `vera-backend/`. Windows note: 5 known env-only pytest failures exist (anchor `:` filenames, schema CRLF) — anything else red is yours.
- Frontend gate: all four of `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` from `vera-frontend/`.
- Backend responses: always `ResponseModel[T]` via `ok(payload)`; errors via `CustomAPIException` subclasses — never `HTTPException`.
- Comments: only what the code cannot say, one line; single-sentence docstrings.
- Page numbers/sizes in query strings are fine (not PHI); never put PHI in a URL.
- Backend venv lives at `C:\venvs\vera-backend`; use `uv sync --all-packages` if deps are ever missing.
- After all tasks: run the code-simplifier agent ("simplify code") before claiming done (repo CLAUDE.md mandate).

---

### Task 1: Backend — paginated history scope on `GET /calls`

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py:833-895` (`list_calls`)
- Test: `vera-backend/tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: existing `_summary`, `_visible_to`, `_pct`, `seed_call`/`rbac_world`/`_auth` test fixtures, `PaginatedCalls` (the `/call-history` model, as the shape precedent).
- Produces: `GET /calls?scope=history&page=N&page_size=M` → `ResponseModel[PaginatedCallSummaries]` where `PaginatedCallSummaries = {items: list[CallSummary], page: int, page_size: int, total: int}`. `scope=live` response unchanged (`list[CallSummary]`, optional `limit` still live-only).

- [ ] **Step 1: Write the failing tests** — append to `tests/integration/control_plane/test_calls.py` (same fixtures as `test_list_calls_history_scope_returns_terminal_calls_only`):

```python
@pytest.mark.asyncio
async def test_history_scope_returns_paginated_envelope(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = [
        str(
            await seed_call(
                admin_sessionmaker,
                rbac_world.tenant_id,
                seeded_form_id,
                initiated_by_id=rbac_world.admin_id,
                status=CallStatus.COMPLETED.value,
            )
        )
        for _ in range(3)
    ]

    first = await client.get(
        "/api/v1/calls",
        params={"scope": "history", "page": 1, "page_size": 2},
        headers=_auth(rbac_world.admin_token),
    )
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert data["total"] >= 3
    assert len(data["items"]) == 2

    second = await client.get(
        "/api/v1/calls",
        params={"scope": "history", "page": 2, "page_size": 2},
        headers=_auth(rbac_world.admin_token),
    )
    ids_first = {c["id"] for c in data["items"]}
    ids_second = {c["id"] for c in second.json()["data"]["items"]}
    assert not ids_first & ids_second  # no row repeats across pages
    assert set(seeded) <= ids_first | ids_second


@pytest.mark.asyncio
async def test_history_scope_orders_newest_first_across_pages(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    older = str(
        await seed_call(
            admin_sessionmaker,
            rbac_world.tenant_id,
            seeded_form_id,
            initiated_by_id=rbac_world.admin_id,
            status=CallStatus.COMPLETED.value,
        )
    )
    newer = str(
        await seed_call(
            admin_sessionmaker,
            rbac_world.tenant_id,
            seeded_form_id,
            initiated_by_id=rbac_world.admin_id,
            status=CallStatus.COMPLETED.value,
        )
    )
    resp = await client.get(
        "/api/v1/calls",
        params={"scope": "history", "page": 1, "page_size": 50},
        headers=_auth(rbac_world.admin_token),
    )
    ids = [c["id"] for c in resp.json()["data"]["items"]]
    assert ids.index(newer) < ids.index(older)


@pytest.mark.asyncio
async def test_history_scope_past_the_end_page_is_empty_with_total(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.COMPLETED.value,
    )
    resp = await client.get(
        "/api/v1/calls",
        params={"scope": "history", "page": 999, "page_size": 20},
        headers=_auth(rbac_world.admin_token),
    )
    data = resp.json()["data"]
    assert data["items"] == []
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_live_scope_response_shape_unchanged(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.ACTIVE.value,
    )
    resp = await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))
    assert isinstance(resp.json()["data"], list)  # live stays a bare array
```

- [ ] **Step 2: Update the two existing history tests to the new envelope.** In `test_list_calls_history_scope_returns_terminal_calls_only` (~line 391) and the published-call history test (~line 580), every `history.json()["data"]` that is read as a list becomes `history.json()["data"]["items"]`.

- [ ] **Step 3: Run to verify the new tests fail** (envelope is still a bare list):

Run (from `vera-backend/`): `uv run pytest tests/integration/control_plane/test_calls.py -k "history_scope or live_scope_response" -v`
Expected: the new tests FAIL (`TypeError: list indices must be integers` / KeyError `"items"`); needs `just up` + `just migrate` for the integration DB.

- [ ] **Step 4: Implement.** In `calls.py`, add the model next to `PaginatedCalls` (~line 963):

```python
class PaginatedCallSummaries(BaseModel):
    items: list[CallSummary]
    page: int
    page_size: int
    total: int
```

(Declare it above `list_calls` or forward-reference — keep both paginated models adjacent for discoverability.)

Rework `list_calls`:

```python
@router.get(
    "/calls",
    response_model=ResponseModel[list[CallSummary] | PaginatedCallSummaries],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_calls(
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    scope: Literal["live", "history"] = "live",
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary] | PaginatedCallSummaries]:
    """`scope=live` (default) lists in-flight calls — unbounded unless `limit`
    is passed (capping it by default could silently hide live calls from
    monitoring); `scope=history` returns terminal calls as `page`/`page_size`
    pages with a `total` (the `/call-history` envelope)."""
    response.headers["Cache-Control"] = "no-store"
    status_cond = (
        Call.current_status.in_(list(_ACTIVE_STATUSES))
        if scope == "live"
        else Call.current_status.in_(TERMINAL_VALUES)
    )
    query = (
        select(
            Call,
            PatientForm.patient_name,
            PatientForm.insurance_provider,
            FormSchema.insurance_type,
            PatientForm.completion_pct,
        )
        .join(PatientForm, PatientForm.id == Call.form_id)
        .join(SchemaVersion, SchemaVersion.id == PatientForm.schema_version_id)
        .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
        .where(status_cond)
        .where(_visible_to(caller.user_id))
        # id (UUIDv7) tie-break keeps pages stable across equal timestamps.
        .order_by(Call.created_at.desc(), Call.id.desc())
    )
    payload: list[CallSummary] | PaginatedCallSummaries
    if scope == "live":
        if limit is not None:
            query = query.limit(limit)
        rows = (await session.execute(query)).all()
        payload = [
            _summary(c, name, caller.user_id, provider, insurance_type, _pct(completion))
            for c, name, provider, insurance_type, completion in rows
        ]
    else:
        paged = query.offset((page - 1) * page_size).limit(page_size)
        rows = (
            await session.execute(paged.add_columns(func.count().over().label("total")))
        ).all()
        if rows:
            total = int(rows[0].total)
        else:
            # Out-of-range page returns no rows; fall back to a bare count.
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(Call)
                    .where(status_cond)
                    .where(_visible_to(caller.user_id))
                )
            ).scalar_one()
        payload = PaginatedCallSummaries(
            items=[
                _summary(c, name, caller.user_id, provider, insurance_type, _pct(completion))
                for c, name, provider, insurance_type, completion, _ in rows
            ],
            page=page,
            page_size=page_size,
            total=total,
        )
    # PHI disclosure — audit field names, mirroring list_patient_forms.
    await emit_phi_read_audit(
        audit,
        request,
        tenant_id=tenant_id,
        caller=caller,
        resource_type="call",
        resource_id="list",
        fields=["patient_name", "insurance_provider", "health_reason"],
    )
    return ok(payload)
```

Notes: the old `effective_limit = (limit or 50) if scope == "history"` line dies — `limit` is now live-only. `func` is already imported (used by `call_stats`).

- [ ] **Step 5: Run the targeted tests, then the whole file:**

Run: `uv run pytest tests/integration/control_plane/test_calls.py -v`
Expected: PASS (including the two updated existing history tests).

- [ ] **Step 6: Run `just check`** (verbatim, from `vera-backend/`).
Expected: green, modulo the 5 known Windows-only failures.

- [ ] **Step 7: Stage for review**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py vera-backend/tests/integration/control_plane/test_calls.py
```

STOP — Azad reviews the staged diff, then (on his go-ahead):
`git commit -m "feat: paginate GET /calls history scope (VR2-160)"`

---

### Task 2: Frontend API — `listCompletedCalls` + generic `Paginated<T>`

**Files:**
- Modify: `vera-frontend/src/lib/api/calls.ts:59-121`
- Test: `vera-frontend/src/lib/api/calls.test.ts`

**Interfaces:**
- Consumes: `apiRequest` from `@/lib/api/client`; `CallSummary`, `CallHistoryRow` (unchanged).
- Produces: `listCalls(): Promise<CallSummary[]>` (live only — the `scope` param is removed); `listCompletedCalls(params?: {page?: number; page_size?: number}): Promise<Paginated<CallSummary>>`; `export type Paginated<T> = {items: T[]; page: number; page_size: number; total: number}`; `PaginatedCalls` becomes the alias `Paginated<CallHistoryRow>` (existing importers unaffected).

- [ ] **Step 1: Update/extend the tests** in `calls.test.ts` — replace the `"lists terminal calls with GET /calls?scope=history"` test and add defaults coverage:

```ts
it("lists completed calls as a page with GET /calls?scope=history", async () => {
  const done = { ...call, status: "completed", ended_at: "2026-07-04T10:05:00Z" }
  const page = { items: [done], page: 2, page_size: 20, total: 41 }
  vi.mocked(apiRequest).mockResolvedValue(page)
  const out = await listCompletedCalls({ page: 2 })
  expect(out).toEqual(page)
  expect(apiRequest).toHaveBeenCalledWith("/calls?scope=history&page=2&page_size=20")
})

it("listCompletedCalls defaults to page 1, size 20", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ items: [], page: 1, page_size: 20, total: 0 })
  await listCompletedCalls()
  expect(apiRequest).toHaveBeenCalledWith("/calls?scope=history&page=1&page_size=20")
})
```

Also update the `"lists active calls"` test's call from `listCalls()` (unchanged behavior, but the import list gains `listCompletedCalls` and the `Paginated` type).

- [ ] **Step 2: Run to verify failure:**

Run (from `vera-frontend/`): `npx vitest run src/lib/api/calls.test.ts`
Expected: FAIL — `listCompletedCalls` is not exported.

- [ ] **Step 3: Implement in `calls.ts`:**

```ts
/** A server-driven page envelope (matches the backend's paginated responses). */
export type Paginated<T> = {
  items: T[]
  page: number
  page_size: number
  total: number
}

/** GET /calls — in-flight calls the caller owns or that are published, newest first. */
export function listCalls(): Promise<CallSummary[]> {
  return apiRequest<CallSummary[]>("/calls")
}

/** GET /calls?scope=history — terminal calls, newest first, one server page at a time. */
export function listCompletedCalls(
  params: { page?: number; page_size?: number } = {},
): Promise<Paginated<CallSummary>> {
  const { page = 1, page_size = 20 } = params
  return apiRequest<Paginated<CallSummary>>(`/calls?scope=history&page=${page}&page_size=${page_size}`)
}

export type PaginatedCalls = Paginated<CallHistoryRow>
```

(The old `listCalls(scope)` union param goes away; `LiveMonitoring.tsx` still compiles because its `listCalls("history")` call is replaced in Task 5 — expect a transient `tsc` error until then, which is why Tasks 2–5 gate together, or temporarily leave the old signature accepting `"live"` only. Prefer: do the `LiveMonitoring.tsx` call-site swap for `listCalls("history")` → `listCompletedCalls(...)` in Task 5 and only run the full `tsc -b` gate there; this task's gate is the vitest file.)

- [ ] **Step 4: Run the test file:**

Run: `npx vitest run src/lib/api/calls.test.ts`
Expected: PASS.

- [ ] **Step 5: Stage for review**

```bash
git add vera-frontend/src/lib/api/calls.ts vera-frontend/src/lib/api/calls.test.ts
```

STOP for Azad's review; on go-ahead: `git commit -m "feat: listCompletedCalls paginated API wrapper (VR2-160)"`

---

### Task 3: Frontend — pure pagination helpers

**Files:**
- Create: `vera-frontend/src/lib/pagination.ts`
- Test: `vera-frontend/src/lib/pagination.test.ts`

**Interfaces:**
- Produces: `lastPageOf(total: number, pageSize: number): number` (≥ 1 always); `slicePage<T>(rows: T[], page: number, pageSize: number): T[]`.

- [ ] **Step 1: Write the failing tests** (`pagination.test.ts`):

```ts
import { describe, expect, it } from "vitest"

import { lastPageOf, slicePage } from "./pagination"

describe("lastPageOf", () => {
  it("rounds up to whole pages", () => expect(lastPageOf(41, 20)).toBe(3))
  it("is exact on a full last page", () => expect(lastPageOf(40, 20)).toBe(2))
  it("never drops below page 1, even empty", () => expect(lastPageOf(0, 20)).toBe(1))
})

describe("slicePage", () => {
  const rows = ["a", "b", "c", "d", "e"]
  it("returns the requested window", () => expect(slicePage(rows, 2, 2)).toEqual(["c", "d"]))
  it("returns a short final page", () => expect(slicePage(rows, 3, 2)).toEqual(["e"]))
  it("returns empty past the end", () => expect(slicePage(rows, 9, 2)).toEqual([]))
})
```

- [ ] **Step 2: Run to verify failure:** `npx vitest run src/lib/pagination.test.ts` — FAIL (module not found).

- [ ] **Step 3: Implement (`pagination.ts`):**

```ts
/** Last page number for a total — never below 1, so "page 1 of 1" renders even when empty. */
export function lastPageOf(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / pageSize))
}

/** The rows belonging to a 1-based page. */
export function slicePage<T>(rows: T[], page: number, pageSize: number): T[] {
  return rows.slice((page - 1) * pageSize, page * pageSize)
}
```

- [ ] **Step 4: Run:** `npx vitest run src/lib/pagination.test.ts` — PASS.

- [ ] **Step 5: Stage for review**

```bash
git add vera-frontend/src/lib/pagination.ts vera-frontend/src/lib/pagination.test.ts
```

STOP for review; on go-ahead: `git commit -m "feat: pagination math helpers (VR2-160)"`

---

### Task 4: Frontend — shared `PaginationFooter`, adopted by Call History

**Files:**
- Create: `vera-frontend/src/components/ui/pagination-footer.tsx`
- Modify: `vera-frontend/src/pages/CallHistory.tsx:233-257` (replace the inline footer)

**Interfaces:**
- Consumes: `Button` from `@/components/ui/button`; `lastPageOf` from `@/lib/pagination` (Task 3).
- Produces: `PaginationFooter({page, pageSize, total, loaded, noun, onPageChange})` — `loaded` false renders "Loading…" in place of the count; `noun` is the singular item word ("call").

- [ ] **Step 1: Create the component** (markup lifted verbatim from Call History's footer so both pages keep the exact same look):

```tsx
import { Button } from "@/components/ui/button"
import { lastPageOf } from "@/lib/pagination"

type PaginationFooterProps = {
  page: number
  pageSize: number
  total: number
  /** False until the first fetch resolves — shows "Loading…" instead of a zero count. */
  loaded: boolean
  /** Singular item word for the count label, e.g. "call". */
  noun: string
  onPageChange: (page: number) => void
}

export function PaginationFooter({
  page,
  pageSize,
  total,
  loaded,
  noun,
  onPageChange,
}: PaginationFooterProps) {
  const lastPage = lastPageOf(total, pageSize)
  return (
    <div className="flex items-center justify-between gap-4 px-4 py-3">
      <span className="text-sm text-muted-foreground">
        {loaded
          ? `${total} ${noun}${total === 1 ? "" : "s"} · page ${page} of ${lastPage}`
          : "Loading…"}
      </span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={() => onPageChange(Math.max(1, page - 1))}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={page >= lastPage}
          onClick={() => onPageChange(page + 1)}
        >
          Next
        </Button>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Swap it into `CallHistory.tsx`** — delete the inline footer `div` (lines 233–257) and render:

```tsx
<PaginationFooter
  page={page}
  pageSize={PAGE_SIZE}
  total={total}
  loaded={items !== null}
  noun="call"
  onPageChange={setPage}
/>
```

Remove the now-unused `lastPage` local (`CallHistory.tsx:120`) and add the import. Behavior must be pixel-identical.

- [ ] **Step 3: Verify:** `npx tsc -b && npx eslint . && npx vitest run` — green; spot-check the Call History page renders unchanged if dev servers are up (Azad may prefer to run them himself).

- [ ] **Step 4: Stage for review**

```bash
git add vera-frontend/src/components/ui/pagination-footer.tsx vera-frontend/src/pages/CallHistory.tsx
```

STOP for review; on go-ahead: `git commit -m "refactor: extract shared PaginationFooter (VR2-160)"`

---

### Task 5: Frontend — wire pagination into Live Monitoring

**Files:**
- Modify: `vera-frontend/src/pages/LiveMonitoring.tsx`

**Interfaces:**
- Consumes: `listCompletedCalls`, `Paginated<CallSummary>` (Task 2); `slicePage`, `lastPageOf` (Task 3); `PaginationFooter` (Task 4).
- Produces: the finished VR2-160 behavior; no new exports.

- [ ] **Step 1: State + fetch changes.**
  - `const PAGE_SIZE = 20` beside `POLL_MS`.
  - Add `const [page, setPage] = useState(1)`.
  - Replace `const [history, setHistory] = useState<CallSummary[]>([])` with `useState<Paginated<CallSummary> | null>(null)`.
  - In `load()`: third promise becomes `tab === "completed" ? listCompletedCalls({ page, page_size: PAGE_SIZE }) : Promise.resolve(null)`; effect dependency array becomes `[tab, page]` so a page change refetches and the 8s poll always re-reads the current page.
  - Tab buttons: `onClick={() => { setTab(t.key); setPage(1) }}` — page resets on every tab switch.

- [ ] **Step 2: Rows, totals, clamp.**

```tsx
const filtered = useMemo(() => {
  if (tab === "critical") return calls.filter((c) => categoryOf(c.status) === "critical")
  if (tab === "completed") return history?.items ?? []
  return calls
}, [tab, calls, history])
// Completed is already a server page; the live tabs slice their full in-memory list.
const total = tab === "completed" ? (history?.total ?? 0) : filtered.length
const rows = tab === "completed" ? filtered : slicePage(filtered, page, PAGE_SIZE)
```

Clamp when rows vanish between polls (e.g. watching page 3 of Active as calls end):

```tsx
useEffect(() => {
  setPage((p) => Math.min(p, lastPageOf(total, PAGE_SIZE)))
}, [total])
```

- [ ] **Step 3: Render the footer** directly after `</Table>` inside the `Card`:

```tsx
<PaginationFooter
  page={page}
  pageSize={PAGE_SIZE}
  total={total}
  loaded={tab !== "completed" || history !== null}
  noun="call"
  onPageChange={setPage}
/>
```

- [ ] **Step 4: Confirm the untouched invariants** (read the diff, don't rely on memory): stat cards still fed by `/calls/stats`; the notification deep-link effect and `endedBySse` pinning still operate on the FULL `calls` array (they must — never on `rows`); `modalCall` lookup unchanged; empty state row unchanged.

- [ ] **Step 5: Full frontend gate:** `npx tsc -b && npx eslint . && npm test && npm run build` — all green.

- [ ] **Step 6: Manual pass** (Azad may start the servers himself — ask): seed >20 completed calls, check Completed pages through them and the total; check Active slices at >20 live calls if feasible; switch tabs and confirm the page resets; confirm stat-card counts still match Live Monitoring's full list, not the visible page.

- [ ] **Step 7: Stage for review**

```bash
git add vera-frontend/src/pages/LiveMonitoring.tsx
```

STOP for review; on go-ahead: `git commit -m "feat: paginate Live Monitoring tabs (VR2-160)"`

---

### Task 6: Simplify pass + final gates

**Files:**
- Possibly touched: everything from Tasks 1–5.

- [ ] **Step 1: Run the code-simplifier agent** on the branch's changes ("simplify code" — `code-simplifier@claude-plugins-official`), per repo CLAUDE.md. Behavior must not change.
- [ ] **Step 2: Re-run BOTH gates on the exact final tree:** backend `just check`; frontend `npx tsc -b && npx eslint . && npm test && npm run build`.
- [ ] **Step 3: Stage any simplifier refinements** and STOP for Azad's review; on go-ahead commit (`refactor: simplify VR2-160 changes`), then push with the explicit refspec:

```bash
git push origin HEAD:refs/heads/fix/live-monitoring-pagination
```

Open the PR from the URL Bitbucket prints (targets `dev`; `gh` does not work here).

---

## Self-review notes

- Spec coverage: backend envelope+params (Task 1), API wrapper (Task 2), clamp/slice/reset/footer (Tasks 3–5), untouched invariants asserted (Task 5 Step 4), tests per spec's list (Tasks 1–3), gates (Tasks 5–6). Out-of-scope items appear in no task. ✔
- The spec's "component tests" are delivered as pure-helper unit tests + API wrapper tests — the repo has no page-level component tests to pattern-match, and the paging logic lives in the extracted helpers precisely so it's testable this way.
- Type consistency: `Paginated<T>`/`listCompletedCalls` (Task 2) are what Tasks 4–5 import; `lastPageOf`/`slicePage` names match across Tasks 3–5. ✔
