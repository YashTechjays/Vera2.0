# `/agent-prompt` Editor Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the platform-admin `/agent-prompt` page as a 3-pane PromptDocument editor (session block + sparse task overrides + rendered preview) against the new prompt architecture, adding three small backend affordances it needs.

**Architecture:** Backend: three additions to the existing platform-gated prompts router (pinned schema version on version payloads, a published-schema read, a stateless preview POST). Frontend: a rewritten typed API client, a pure document-editing helper module (the unit-tested core), five presentational components under `src/components/agent-prompt/`, and a rewritten orchestrating page using local `useState` + `useEffect` (house convention — no Redux for server data, no new deps).

**Tech Stack:** FastAPI + SQLAlchemy async + pydantic (backend); React 19 + Vite + TS + shadcn-style components + vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-07-09-agent-prompt-editor-design.md` — read it first; it is the authority on behavior.

## Global Constraints

- Backend errors: raise `ConflictError` / `BadRequestError` / `NotFoundError` from `control_plane.exceptions`, never `HTTPException`; every route returns `ResponseModel[T]` via `ok(...)` with `responses=CustomAPIResponse.custom(...)`.
- Backend gates: `cd vera-backend && just check` (ruff + mypy --strict + pytest). Integration tests need local infra: `just up && just migrate` once.
- Frontend style (`vera-frontend/CLAUDE.md` + existing code): ES modules, `function` keyword for top-level functions, **explicit return types**, no nested ternaries, selectors as arrow-consts. No PHI in browser storage (nothing here is PHI — global catalog text).
- Frontend gates: `cd vera-frontend && npm run lint && npm test && npm run build` (build runs `tsc -b`).
- No new dependencies, frontend or backend.
- Repo rule: after implementation is complete, run the **code-simplifier** agent, then re-run all gates (Task 9).
- Commit after every task (small, conventional-commit messages; no Co-Authored-By).

## File Map

| File | Responsibility |
|---|---|
| `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py` | +pinned schema fields, +`GET /{id}/schema`, +`POST /{id}/preview` |
| `vera-backend/tests/integration/control_plane/test_prompts.py` | integration tests for the three additions |
| `vera-frontend/src/lib/api/prompts.ts` | rewritten typed client for the new contract |
| `vera-frontend/src/lib/api/prompts.test.ts` | client unit tests (module-mock idiom) |
| `vera-frontend/src/lib/prompts/document.ts` | pure document ops, error parsing, schema extraction |
| `vera-frontend/src/lib/prompts/document.test.ts` | unit tests for the above |
| `vera-frontend/src/components/agent-prompt/PlaceholderPicker.tsx` | dialog picker, inserts `{{token}}` |
| `vera-frontend/src/components/agent-prompt/PromptTextarea.tsx` | shared label+help+textarea+picker+errors unit |
| `vera-frontend/src/components/agent-prompt/OverrideFieldRow.tsx` | one intro/outro/instructions row with provenance |
| `vera-frontend/src/components/agent-prompt/SessionEditor.tsx` | three session textareas |
| `vera-frontend/src/components/agent-prompt/TaskOverrideEditor.tsx` | three OverrideFieldRows for a task |
| `vera-frontend/src/components/agent-prompt/PreviewPane.tsx` | dumb renderer of preview sections |
| `vera-frontend/src/components/agent-prompt/VersionList.tsx` | version rows with Load/Publish |
| `vera-frontend/src/components/agent-prompt/componentTests.test.tsx` | static-markup smoke tests |
| `vera-frontend/src/pages/AgentPrompt.tsx` | rewritten orchestrating page |
| `vera-frontend/src/pages/agentPrompt.helpers.ts` | `pickInitialVersion` (kept as-is) |

---

### Task 1: Backend — expose the pinned schema version on prompt versions

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`
- Test: `vera-backend/tests/integration/control_plane/test_prompts.py`

**Interfaces:**
- Produces: `PromptVersionSummary` and `PromptVersionDetail` responses gain `schema_version_id: UUID` and `schema_version: int` (the pinned `SchemaVersion.version` number). Internal helper `_schema_version_number(session, schema_version_id) -> int`. `_detail(v, schema_version)` gains the second parameter.

- [ ] **Step 1: Write the failing test**

Append to `vera-backend/tests/integration/control_plane/test_prompts.py`:

```python
async def test_versions_expose_pinned_schema_version(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    versions = (
        await client.get(f"/api/v1/prompts/{ids.prompt_id}/versions", headers=_auth(w.super_token))
    ).json()["data"]
    assert versions[0]["schema_version_id"] == str(ids.schema_version_id)
    assert versions[0]["schema_version"] == 1

    detail = (
        await client.get(
            f"/api/v1/prompts/{ids.prompt_id}/versions/{ids.version_id}",
            headers=_auth(w.super_token),
        )
    ).json()["data"]
    assert detail["schema_version_id"] == str(ids.schema_version_id)
    assert detail["schema_version"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_prompts.py::test_versions_expose_pinned_schema_version -q`
Expected: FAIL with `KeyError: 'schema_version_id'`

- [ ] **Step 3: Implement**

In `prompts.py`:

Extend the two response models:

```python
class PromptVersionSummary(BaseModel):
    id: UUID
    version: int
    status: str
    created_at: datetime
    schema_version_id: UUID
    schema_version: int


class PromptVersionDetail(BaseModel):
    id: UUID
    version: int
    status: str
    created_at: datetime
    schema_version_id: UUID
    schema_version: int
    composite_json: dict[str, Any]
```

Change `_detail` and add a number-lookup helper:

```python
def _detail(v: PromptVersion, schema_version: int) -> PromptVersionDetail:
    return PromptVersionDetail(
        id=v.id,
        version=v.version,
        status=v.status,
        created_at=v.created_at,
        schema_version_id=v.schema_version_id,
        schema_version=schema_version,
        composite_json=v.composite_json,
    )


async def _schema_version_number(session: AsyncSession, schema_version_id: UUID) -> int:
    return (
        await session.execute(
            select(SchemaVersion.version).where(SchemaVersion.id == schema_version_id)
        )
    ).scalar_one()
```

In `list_versions`, join `SchemaVersion` (both tables have a `version` column — label the joined one):

```python
    rows = (
        await session.execute(
            select(
                PromptVersion.id,
                PromptVersion.version,
                PromptVersion.status,
                PromptVersion.created_at,
                PromptVersion.schema_version_id,
                SchemaVersion.version.label("schema_version"),
            )
            .join(SchemaVersion, SchemaVersion.id == PromptVersion.schema_version_id)
            .where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.version.desc())
        )
    ).all()
    return ok(
        [
            PromptVersionSummary(
                id=r.id,
                version=r.version,
                status=r.status,
                created_at=r.created_at,
                schema_version_id=r.schema_version_id,
                schema_version=r.schema_version,
            )
            for r in rows
        ]
    )
```

Update every `_detail(...)` call site:

- `get_version`:

```python
async def get_version(
    prompt_id: UUID,
    version_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptVersionDetail]:
    version = await _require_version(session, prompt_id, version_id)
    return ok(_detail(version, await _schema_version_number(session, version.schema_version_id)))
```

- `create_draft` (the pinned schema is already in hand): `return ok(_detail(draft, published_schema.version))`
- `publish_version`: after `target = await _require_version(...)`, add
  `schema_version = await _schema_version_number(session, target.schema_version_id)`
  and use `_detail(target, schema_version)` in **both** returns (the idempotent no-op and the final return).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_prompts.py -q`
Expected: all PASS (existing tests unaffected — they don't assert absent keys)

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py vera-backend/tests/integration/control_plane/test_prompts.py
git commit -m "feat(prompts-api): expose pinned schema_version_id + version number on prompt versions"
```

---

### Task 2: Backend — `GET /prompts/{prompt_id}/schema` (platform-gated published-schema read)

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`
- Test: `vera-backend/tests/integration/control_plane/test_prompts.py`

**Interfaces:**
- Produces: `GET /api/v1/prompts/{prompt_id}/schema` → `ResponseModel[PromptSchemaDetail]` where `PromptSchemaDetail = {id, schema_id, version, status, insurance_type, name, document}`; 409 when no published schema version; 403 for tenant users; gated `platform:prompts:read`.

*Why this exists (spec §2.2):* the existing `GET /schema-versions/{id}` is tenant-gated (`require("forms:read")` + `tenant_context`) — a platform operator without an elevation grant gets 403. The editor needs the published schema document for task text defaults and the placeholder namespace; the published version is what a new draft will pin.

- [ ] **Step 1: Write the failing tests**

Append to `test_prompts.py`:

```python
async def test_get_prompt_schema_returns_published_document(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.get(f"/api/v1/prompts/{ids.prompt_id}/schema", headers=_auth(w.super_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == str(ids.schema_version_id)
    assert data["version"] == 1
    assert data["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value
    assert data["document"]["system_fields"] == {"member_id": "sections.basics.plan_type"}
    assert data["document"]["tasks"][0]["task_key"] == "main"


async def test_get_prompt_schema_conflict_when_none_published(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, w, ids = prompts_world
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE schema_version SET status='draft' WHERE id=:i").bindparams(
                i=ids.schema_version_id
            )
        )
    resp = await client.get(f"/api/v1/prompts/{ids.prompt_id}/schema", headers=_auth(w.super_token))
    assert resp.status_code == 409


async def test_get_prompt_schema_forbidden_for_tenant(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/schema", headers=_auth(w.tenant_admin_token)
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_prompts.py -q -k get_prompt_schema`
Expected: 3 FAIL with 404 (route does not exist yet)

- [ ] **Step 3: Implement the route**

Add to `prompts.py` (place after `get_version`, before `preview_prompt`):

```python
class PromptSchemaDetail(BaseModel):
    """The published schema version the next draft will pin. Platform mirror of
    patient_forms' SchemaVersionDetail — that route is tenant-gated, so a platform
    operator without an elevation grant cannot use it (spec 2026-07-09 §2.2)."""

    id: UUID
    schema_id: UUID
    version: int
    status: str
    insurance_type: str
    name: str
    document: dict[str, Any]


@router.get(
    "/{prompt_id}/schema",
    response_model=ResponseModel[PromptSchemaDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def get_prompt_schema(
    prompt_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptSchemaDetail]:
    """The editor's source for task text defaults + the placeholder namespace."""
    prompt = await _require_prompt(session, prompt_id)
    schema_version = await _published_schema_version(session, prompt.schema_id)
    if schema_version is None:
        raise ConflictError(message="no published schema version")
    form_schema = (
        await session.execute(select(FormSchema).where(FormSchema.id == prompt.schema_id))
    ).scalar_one()
    return ok(
        PromptSchemaDetail(
            id=schema_version.id,
            schema_id=schema_version.schema_id,
            version=schema_version.version,
            status=schema_version.status,
            insurance_type=form_schema.insurance_type,
            name=form_schema.name,
            document=schema_version.schema_json,
        )
    )
```

(`FormSchema`, `ConflictError`, `select` are already imported.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_prompts.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py vera-backend/tests/integration/control_plane/test_prompts.py
git commit -m "feat(prompts-api): platform-gated GET /prompts/{id}/schema for the editor"
```

---

### Task 3: Backend — `POST /prompts/{prompt_id}/preview` (stateless render)

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`
- Test: `vera-backend/tests/integration/control_plane/test_prompts.py`

**Interfaces:**
- Produces: `POST /api/v1/prompts/{prompt_id}/preview` with body `PromptDocument` → `ResponseModel[PromptPreview]` where `PromptPreview = {errors: list[str], rendered: RenderedPrompts}`. HTTP 200 even with content errors (the renderer tolerates them; the editor polls this debounced — spec §2.3). 422 on shape errors (pydantic), 409 without a published schema, 403 for tenants. Gated `platform:prompts:read` (read-shaped dry run). Persists nothing.

- [ ] **Step 1: Write the failing tests**

Append to `test_prompts.py`:

```python
async def test_stateless_preview_renders_without_saving(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    headers = _auth(w.super_token)
    body = {**VALID_PROMPT_DOC, "task_overrides": {"main": {"prompt": "DRY RUN."}}}
    resp = await client.post(f"/api/v1/prompts/{ids.prompt_id}/preview", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["errors"] == []
    main = next(t for t in data["rendered"]["tasks"] if t["task_key"] == "main")
    assert main["prompt"].startswith("DRY RUN.")

    versions = (
        await client.get(f"/api/v1/prompts/{ids.prompt_id}/versions", headers=headers)
    ).json()["data"]
    assert len(versions) == 1  # no draft row was created


async def test_stateless_preview_reports_content_errors_but_still_renders(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    body = {
        **VALID_PROMPT_DOC,
        "session": {**VALID_PROMPT_DOC["session"], "persona": "Hi {{ghost}}."},
        "task_overrides": {"phantom": {"prompt": "x"}},
    }
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/preview", headers=_auth(w.super_token), json=body
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "session.persona: unknown placeholder {{ghost}}" in data["errors"]
    assert "task_overrides.phantom: unknown task_key" in data["errors"]
    assert data["rendered"]["persona"] == "Hi {{ghost}}."  # rendered anyway


async def test_stateless_preview_shape_error_is_422(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/preview", headers=_auth(w.super_token), json={"nope": 1}
    )
    assert resp.status_code == 422


async def test_stateless_preview_forbidden_for_tenant(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, w, ids = prompts_world
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/preview",
        headers=_auth(w.tenant_admin_token),
        json=VALID_PROMPT_DOC,
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_prompts.py -q -k stateless_preview`
Expected: 4 FAIL (405/404 — no POST route)

- [ ] **Step 3: Implement the route**

Add to `prompts.py`, right after the existing GET `preview_prompt`:

```python
class PromptPreview(BaseModel):
    """Stateless dry-run render. `errors` uses the exact save-time 400 strings
    (same validate_prompt_document); 200 even when non-empty because the renderer
    tolerates content errors and the editor polls this while typing (spec §2.3)."""

    errors: list[str]
    rendered: RenderedPrompts


@router.post(
    "/{prompt_id}/preview",
    response_model=ResponseModel[PromptPreview],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def preview_document(
    prompt_id: UUID,
    body: PromptDocument,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptPreview]:
    """Render an unsaved document against the published schema; persist nothing.

    Read-gated: a dry run that mutates nothing (POST only because the document
    travels in the body). Deliberately NOT idempotency-gated — non-mutating."""
    prompt = await _require_prompt(session, prompt_id)
    published_schema = await _published_schema_version(session, prompt.schema_id)
    if published_schema is None:
        raise ConflictError(message="no published schema to render against")
    schema_doc = FormSchemaDoc.model_validate(published_schema.schema_json)
    return ok(
        PromptPreview(
            errors=validate_prompt_document(body, schema_doc),
            rendered=render_task_prompts(schema_doc, body),
        )
    )
```

(All names already imported at the top of the file.)

- [ ] **Step 4: Run backend gate**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_prompts.py -q` then `cd vera-backend && just check`
Expected: all PASS, gate green

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py vera-backend/tests/integration/control_plane/test_prompts.py
git commit -m "feat(prompts-api): stateless POST /prompts/{id}/preview for unsaved-document rendering"
```

---

### Task 4: Frontend — rewrite the prompts API client

**Files:**
- Rewrite: `vera-frontend/src/lib/api/prompts.ts`
- Create: `vera-frontend/src/lib/api/prompts.test.ts`

**Interfaces:**
- Produces (consumed by every later task):

```ts
export type SessionBlock = { persona: string; goal: string; base_instructions: string }
// Server serializes unset override fields as null (pydantic model_dump).
export type TaskTextOverride = { intro?: string | null; outro?: string | null; prompt?: string | null }
export type PromptDocument = {
  kind: "prompt_document"
  session: SessionBlock
  task_overrides: Record<string, TaskTextOverride>
}
export type PromptSummary = { id: string; name: string; insurance_type: string; published_version: number | null }
export type PromptVersionSummary = {
  id: string; version: number; status: string; created_at: string
  schema_version_id: string; schema_version: number
}
export type PromptVersionDetail = PromptVersionSummary & { composite_json: PromptDocument }
export type RenderedTaskPrompt = { task_key: string; title: string; intro: string | null; outro: string | null; prompt: string }
export type RenderedPrompts = {
  name: string; insurance_type: string; dsl_version: string
  persona: string; goal: string; base_instructions: string
  tasks: RenderedTaskPrompt[]
}
export type PromptSchemaDetail = {
  id: string; schema_id: string; version: number; status: string
  insurance_type: string; name: string; document: unknown
}
export type PromptPreview = { errors: string[]; rendered: RenderedPrompts }

export function listPrompts(): Promise<PromptSummary[]>
export function listPromptVersions(promptId: string): Promise<PromptVersionSummary[]>
export function getPromptVersion(promptId: string, versionId: string): Promise<PromptVersionDetail>
export function createPromptDraft(promptId: string, doc: PromptDocument): Promise<PromptVersionDetail>
export function publishPromptVersion(promptId: string, versionId: string): Promise<PromptVersionDetail>
export function getPromptSchema(promptId: string): Promise<PromptSchemaDetail>
export function previewPromptVersion(promptId: string, versionId?: string): Promise<RenderedPrompts>
export function previewPromptDocument(promptId: string, doc: PromptDocument): Promise<PromptPreview>
```

- [ ] **Step 1: Write the failing tests**

Create `vera-frontend/src/lib/api/prompts.test.ts` (module-mock idiom from `voiceLab.test.ts` — the real client touches sessionStorage at import):

```ts
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    httpStatus: number
    errorCode: string | null
    constructor(httpStatus: number, errorCode: string | null, message: string) {
      super(message)
      this.name = "ApiError"
      this.httpStatus = httpStatus
      this.errorCode = errorCode
    }
  }
  return { apiRequest: vi.fn(), ApiError }
})

import { apiRequest } from "@/lib/api/client"
import {
  createPromptDraft,
  getPromptSchema,
  previewPromptDocument,
  previewPromptVersion,
  type PromptDocument,
} from "./prompts"

const doc: PromptDocument = {
  kind: "prompt_document",
  session: { persona: "p", goal: "g", base_instructions: "b" },
  task_overrides: { wrap_up: { outro: "bye" } },
}

describe("prompts api client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("posts the document itself as the draft body (not {composite_json})", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await createPromptDraft("p1", doc)
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/versions", { method: "POST", body: doc })
  })

  it("fetches the published schema for a prompt", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await getPromptSchema("p1")
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/schema")
  })

  it("GET-previews a named version via query param, url-encoded", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await previewPromptVersion("p1", "v9")
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/preview?version_id=v9")
  })

  it("GET-previews the published version with no query param", async () => {
    vi.mocked(apiRequest).mockResolvedValue({})
    await previewPromptVersion("p1")
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/preview")
  })

  it("POST-previews an unsaved document", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ errors: [], rendered: {} })
    await previewPromptDocument("p1", doc)
    expect(apiRequest).toHaveBeenCalledWith("/prompts/p1/preview", { method: "POST", body: doc })
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-frontend && npx vitest run src/lib/api/prompts.test.ts`
Expected: FAIL (imports don't exist yet / old shapes)

- [ ] **Step 3: Rewrite the client**

Replace the full contents of `vera-frontend/src/lib/api/prompts.ts`:

```ts
// Platform (super admin) prompt-catalog endpoints. Mirrors backend api/v1/prompts.py:
// composite_json is a PromptDocument (session block + sparse task overrides), never
// compiled text; rendering happens server-side (GET/POST /preview).
import { apiRequest } from "@/lib/api/client"

export type SessionBlock = {
  persona: string
  goal: string
  base_instructions: string
}

/** Sparse patch over one task's schema-authored text. The server serializes
 *  unset fields as null; treat null and absent identically. */
export type TaskTextOverride = {
  intro?: string | null
  outro?: string | null
  prompt?: string | null
}

export type PromptDocument = {
  kind: "prompt_document"
  session: SessionBlock
  task_overrides: Record<string, TaskTextOverride>
}

export type PromptSummary = {
  id: string
  name: string
  insurance_type: string
  published_version: number | null
}

export type PromptVersionSummary = {
  id: string
  version: number
  status: string
  created_at: string
  /** The schema_version this immutable version pins (renders/validates against). */
  schema_version_id: string
  schema_version: number
}

export type PromptVersionDetail = PromptVersionSummary & {
  composite_json: PromptDocument
}

export type RenderedTaskPrompt = {
  task_key: string
  title: string
  intro: string | null
  outro: string | null
  prompt: string
}

export type RenderedPrompts = {
  name: string
  insurance_type: string
  dsl_version: string
  persona: string
  goal: string
  base_instructions: string
  tasks: RenderedTaskPrompt[]
}

/** The published schema version the next draft will pin (GET /prompts/{id}/schema). */
export type PromptSchemaDetail = {
  id: string
  schema_id: string
  version: number
  status: string
  insurance_type: string
  name: string
  document: unknown
}

/** Stateless dry-run render; `errors` uses the exact save-time 400 strings. */
export type PromptPreview = {
  errors: string[]
  rendered: RenderedPrompts
}

export function listPrompts(): Promise<PromptSummary[]> {
  return apiRequest<PromptSummary[]>("/prompts")
}

export function listPromptVersions(promptId: string): Promise<PromptVersionSummary[]> {
  return apiRequest<PromptVersionSummary[]>(`/prompts/${encodeURIComponent(promptId)}/versions`)
}

export function getPromptVersion(promptId: string, versionId: string): Promise<PromptVersionDetail> {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}`,
  )
}

/** Every save creates a new immutable draft; the body IS the document. */
export function createPromptDraft(promptId: string, doc: PromptDocument): Promise<PromptVersionDetail> {
  return apiRequest<PromptVersionDetail>(`/prompts/${encodeURIComponent(promptId)}/versions`, {
    method: "POST",
    body: doc,
  })
}

export function publishPromptVersion(promptId: string, versionId: string): Promise<PromptVersionDetail> {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}/publish`,
    { method: "POST" },
  )
}

export function getPromptSchema(promptId: string): Promise<PromptSchemaDetail> {
  return apiRequest<PromptSchemaDetail>(`/prompts/${encodeURIComponent(promptId)}/schema`)
}

/** Authoritative render of a SAVED version (no id → the published one). */
export function previewPromptVersion(promptId: string, versionId?: string): Promise<RenderedPrompts> {
  const base = `/prompts/${encodeURIComponent(promptId)}/preview`
  const path = versionId === undefined ? base : `${base}?version_id=${encodeURIComponent(versionId)}`
  return apiRequest<RenderedPrompts>(path)
}

/** Stateless render of the editing buffer; persists nothing. */
export function previewPromptDocument(promptId: string, doc: PromptDocument): Promise<PromptPreview> {
  return apiRequest<PromptPreview>(`/prompts/${encodeURIComponent(promptId)}/preview`, {
    method: "POST",
    body: doc,
  })
}
```

Note: `src/pages/AgentPrompt.tsx` still imports the deleted `CompositeJson` type and old `createPromptDraft` signature — it will not compile until Task 8 rewrites it. That is fine for `vitest` (Step 4) but do NOT run `npm run build` until Task 8.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-frontend && npx vitest run src/lib/api/prompts.test.ts`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/api/prompts.ts vera-frontend/src/lib/api/prompts.test.ts
git commit -m "feat(frontend): prompts api client for the PromptDocument contract"
```

---

### Task 5: Frontend — pure document helpers (`src/lib/prompts/document.ts`)

**Files:**
- Create: `vera-frontend/src/lib/prompts/document.ts`
- Test: `vera-frontend/src/lib/prompts/document.test.ts`

**Interfaces:**
- Consumes: `PromptDocument`, `TaskTextOverride`, `SessionBlock` from `@/lib/api/prompts` (Task 4).
- Produces (consumed by Tasks 6–8):

```ts
export type OverrideField = "intro" | "outro" | "prompt"
export type OverrideState = "overridden" | "default" | "no-default"
export type TaskDefaults = { task_key: string; title: string; intro?: string; outro?: string; prompt?: string }
export type PlaceholderEntry = { token: string; detail: string }
export type PlaceholderGroups = { system: PlaceholderEntry[]; context: PlaceholderEntry[] }
export type ParsedErrors = { fields: Record<string, string[]>; general: string[] }

export function normalizeDocument(doc: PromptDocument): PromptDocument
export function setOverrideField(doc: PromptDocument, taskKey: string, field: OverrideField, text: string): PromptDocument
export function clearOverrideField(doc: PromptDocument, taskKey: string, field: OverrideField): PromptDocument
export function removeOverrideEntry(doc: PromptDocument, taskKey: string): PromptDocument
export function overrideStateOf(doc: PromptDocument, taskKey: string, field: OverrideField, defaultText: string | undefined): OverrideState
export function documentsEqual(a: PromptDocument, b: PromptDocument): boolean
export function clientValidationErrors(doc: PromptDocument): Record<string, string[]>
export function parsePromptErrors(joined: string): ParsedErrors
export function taskDefaultsOf(rawDoc: unknown): TaskDefaults[]
export function placeholderGroupsOf(rawDoc: unknown): PlaceholderGroups
export function insertToken(text: string, token: string, caret: number | null): { next: string; caret: number }
```

- [ ] **Step 1: Write the failing tests**

Create `vera-frontend/src/lib/prompts/document.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import type { PromptDocument } from "@/lib/api/prompts"
import {
  clearOverrideField,
  clientValidationErrors,
  documentsEqual,
  insertToken,
  normalizeDocument,
  overrideStateOf,
  parsePromptErrors,
  placeholderGroupsOf,
  removeOverrideEntry,
  setOverrideField,
  taskDefaultsOf,
} from "./document"

function doc(overrides: PromptDocument["task_overrides"] = {}): PromptDocument {
  return {
    kind: "prompt_document",
    session: { persona: "p", goal: "g", base_instructions: "b" },
    task_overrides: overrides,
  }
}

// Shaped like the raw schema_version document (GET /prompts/{id}/schema `document`).
const rawSchemaDoc = {
  dsl_version: "2.1",
  name: "IBV",
  insurance_type: "infertility_treatment",
  system_fields: { member_id: "sections.basics.plan_type", patient_name: "sections.info.name" },
  sections: {
    basics: {
      title: "Basics",
      fields: {
        plan_type: { type: "text", title: "Plan Type", role: "ask", prompt: { ask: "?" } },
        meta: {
          type: "group",
          title: "Meta",
          fields: { bg: { type: "text", title: "Background", role: "context" } },
        },
      },
    },
    info: {
      title: "Info",
      fields: { name: { type: "text", title: "Name", role: "context" } },
    },
  },
  tasks: [
    { task_key: "main", title: "Main", intro: "Hello.", prompt: "Do the thing.", sections: ["basics"] },
    { task_key: "wrap_up", title: "Wrap Up", outro: null, sections: [] },
  ],
}

describe("override ops", () => {
  it("setOverrideField creates the entry and field immutably", () => {
    const d = doc()
    const next = setOverrideField(d, "wrap_up", "outro", "bye")
    expect(next.task_overrides).toEqual({ wrap_up: { outro: "bye" } })
    expect(d.task_overrides).toEqual({})
  })

  it("clearOverrideField drops the field, and the entry when it was the last field", () => {
    const d = doc({ wrap_up: { outro: "bye", intro: "hi" } })
    const one = clearOverrideField(d, "wrap_up", "intro")
    expect(one.task_overrides).toEqual({ wrap_up: { outro: "bye" } })
    const none = clearOverrideField(one, "wrap_up", "outro")
    expect(none.task_overrides).toEqual({})
  })

  it("removeOverrideEntry drops a whole entry (orphaned-override cleanup)", () => {
    const d = doc({ ghost: { prompt: "x" }, wrap_up: { outro: "bye" } })
    expect(removeOverrideEntry(d, "ghost").task_overrides).toEqual({ wrap_up: { outro: "bye" } })
  })

  it("overrideStateOf distinguishes overridden / default / no-default", () => {
    const d = doc({ main: { intro: "custom" } })
    expect(overrideStateOf(d, "main", "intro", "Hello.")).toBe("overridden")
    expect(overrideStateOf(d, "main", "prompt", "Do the thing.")).toBe("default")
    expect(overrideStateOf(d, "wrap_up", "outro", undefined)).toBe("no-default")
  })
})

describe("normalize + equality", () => {
  it("normalizeDocument strips null fields and empty entries (server nulls)", () => {
    const server = doc({ wrap_up: { intro: null, outro: "bye", prompt: null }, empty: { intro: null } })
    expect(normalizeDocument(server).task_overrides).toEqual({ wrap_up: { outro: "bye" } })
  })

  it("documentsEqual ignores null-vs-absent and key order", () => {
    const a = doc({ wrap_up: { outro: "bye", intro: null }, main: { prompt: "x" } })
    const b = doc({ main: { prompt: "x" }, wrap_up: { outro: "bye" } })
    expect(documentsEqual(a, b)).toBe(true)
    expect(documentsEqual(a, doc({ wrap_up: { outro: "bye!" }, main: { prompt: "x" } }))).toBe(false)
  })
})

describe("validation", () => {
  it("clientValidationErrors flags empty session fields and empty overrides", () => {
    const d: PromptDocument = {
      kind: "prompt_document",
      session: { persona: "", goal: "g", base_instructions: "b" },
      task_overrides: { main: { intro: "  " } },
    }
    const errors = clientValidationErrors(d)
    expect(errors["session.persona"]).toEqual(["Required."])
    expect(errors["task_overrides.main.intro"]).toEqual([
      "An override cannot be empty — use Reset to remove it.",
    ])
    expect(Object.keys(clientValidationErrors(doc())).length).toBe(0)
  })

  it("parsePromptErrors maps location-prefixed messages onto fields", () => {
    const parsed = parsePromptErrors(
      "session.persona: unknown placeholder {{patietn}}; " +
        "task_overrides.wrap_up.outro: unknown placeholder {{a}}; " +
        "task_overrides.wrap_up.outro: unknown placeholder {{b}}; " +
        "task_overrides.ghost: unknown task_key",
    )
    expect(parsed.fields["session.persona"]).toEqual(["unknown placeholder {{patietn}}"])
    expect(parsed.fields["task_overrides.wrap_up.outro"]).toEqual([
      "unknown placeholder {{a}}",
      "unknown placeholder {{b}}",
    ])
    expect(parsed.general).toEqual(["task_overrides.ghost: unknown task_key"])
  })
})

describe("schema extraction", () => {
  it("taskDefaultsOf lists tasks with null text normalized to undefined", () => {
    expect(taskDefaultsOf(rawSchemaDoc)).toEqual([
      { task_key: "main", title: "Main", intro: "Hello.", outro: undefined, prompt: "Do the thing." },
      { task_key: "wrap_up", title: "Wrap Up", intro: undefined, outro: undefined, prompt: undefined },
    ])
  })

  it("placeholderGroupsOf collects system_fields keys and context leaf paths (nested groups)", () => {
    const groups = placeholderGroupsOf(rawSchemaDoc)
    expect(groups.system).toEqual([
      { token: "member_id", detail: "sections.basics.plan_type" },
      { token: "patient_name", detail: "sections.info.name" },
    ])
    expect(groups.context).toEqual([
      { token: "sections.basics.meta.bg", detail: "Background" },
      { token: "sections.info.name", detail: "Name" },
    ])
  })

  it("extraction tolerates a malformed document", () => {
    expect(taskDefaultsOf(null)).toEqual([])
    expect(placeholderGroupsOf({ sections: 7 })).toEqual({ system: [], context: [] })
  })
})

describe("insertToken", () => {
  it("inserts {{token}} at the caret and reports the new caret", () => {
    expect(insertToken("Hello world", "member_id", 6)).toEqual({
      next: "Hello {{member_id}}world",
      caret: 19,
    })
  })

  it("appends when the caret is unknown", () => {
    expect(insertToken("Hi", "member_id", null)).toEqual({ next: "Hi{{member_id}}", caret: 15 })
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-frontend && npx vitest run src/lib/prompts/document.test.ts`
Expected: FAIL — module `./document` not found

- [ ] **Step 3: Implement**

Create `vera-frontend/src/lib/prompts/document.ts`:

```ts
// Pure editing/parsing helpers for the /agent-prompt editor. No React, no I/O —
// the unit-tested core (spec 2026-07-09 §3.1). PromptDocument semantics:
// `session` is literal content; `task_overrides` is a sparse patch where an
// absent field falls through to the schema default and an EMPTY override is
// invalid server-side (min_length=1) — removal, never blanking.
import type { PromptDocument, TaskTextOverride } from "@/lib/api/prompts"

export type OverrideField = "intro" | "outro" | "prompt"

export type OverrideState = "overridden" | "default" | "no-default"

/** One schema task's authored text defaults (from the raw schema document). */
export type TaskDefaults = {
  task_key: string
  title: string
  intro?: string
  outro?: string
  prompt?: string
}

export type PlaceholderEntry = { token: string; detail: string }

export type PlaceholderGroups = {
  /** system_fields keys; detail = the mapped field path */
  system: PlaceholderEntry[]
  /** role:"context" leaf paths; detail = the field title */
  context: PlaceholderEntry[]
}

export type ParsedErrors = {
  /** location (e.g. "task_overrides.wrap_up.outro") → messages */
  fields: Record<string, string[]>
  /** messages with no recognizable field location */
  general: string[]
}

const OVERRIDE_FIELDS: OverrideField[] = ["intro", "outro", "prompt"]

function presentFields(override: TaskTextOverride): TaskTextOverride {
  const out: TaskTextOverride = {}
  for (const field of OVERRIDE_FIELDS) {
    const value = override[field]
    if (typeof value === "string") out[field] = value
  }
  return out
}

/** Strip the server's explicit nulls and any empty entries so the editing
 *  buffer only ever carries present string fields. */
export function normalizeDocument(doc: PromptDocument): PromptDocument {
  const task_overrides: Record<string, TaskTextOverride> = {}
  for (const [key, override] of Object.entries(doc.task_overrides)) {
    const present = presentFields(override)
    if (Object.keys(present).length > 0) task_overrides[key] = present
  }
  return { kind: "prompt_document", session: { ...doc.session }, task_overrides }
}

export function setOverrideField(
  doc: PromptDocument,
  taskKey: string,
  field: OverrideField,
  text: string,
): PromptDocument {
  const entry = { ...presentFields(doc.task_overrides[taskKey] ?? {}), [field]: text }
  return { ...doc, task_overrides: { ...doc.task_overrides, [taskKey]: entry } }
}

/** "Reset to default": remove the override field; drop the entry when empty. */
export function clearOverrideField(
  doc: PromptDocument,
  taskKey: string,
  field: OverrideField,
): PromptDocument {
  const entry = presentFields(doc.task_overrides[taskKey] ?? {})
  delete entry[field]
  if (Object.keys(entry).length === 0) return removeOverrideEntry(doc, taskKey)
  return { ...doc, task_overrides: { ...doc.task_overrides, [taskKey]: entry } }
}

export function removeOverrideEntry(doc: PromptDocument, taskKey: string): PromptDocument {
  const task_overrides = { ...doc.task_overrides }
  delete task_overrides[taskKey]
  return { ...doc, task_overrides }
}

export function overrideStateOf(
  doc: PromptDocument,
  taskKey: string,
  field: OverrideField,
  defaultText: string | undefined,
): OverrideState {
  if (typeof doc.task_overrides[taskKey]?.[field] === "string") return "overridden"
  if (defaultText === undefined) return "no-default"
  return "default"
}

function canonical(doc: PromptDocument): string {
  const normalized = normalizeDocument(doc)
  const keys = Object.keys(normalized.task_overrides).sort()
  return JSON.stringify({
    session: [
      normalized.session.persona,
      normalized.session.goal,
      normalized.session.base_instructions,
    ],
    overrides: keys.map((key) => {
      const entry = normalized.task_overrides[key]
      return [key, entry.intro ?? null, entry.outro ?? null, entry.prompt ?? null]
    }),
  })
}

/** Dirty check: null-vs-absent and key order do not count as changes. */
export function documentsEqual(a: PromptDocument, b: PromptDocument): boolean {
  return canonical(a) === canonical(b)
}

/** Pre-save checks the server would reject with 422/400 shape errors.
 *  Placeholder validation is deliberately NOT done client-side — the preview
 *  endpoint's `errors` is the authority (spec §3.6). */
export function clientValidationErrors(doc: PromptDocument): Record<string, string[]> {
  const errors: Record<string, string[]> = {}
  const session: Record<string, string> = {
    persona: doc.session.persona,
    goal: doc.session.goal,
    base_instructions: doc.session.base_instructions,
  }
  for (const [field, value] of Object.entries(session)) {
    if (value.trim() === "") errors[`session.${field}`] = ["Required."]
  }
  for (const [key, override] of Object.entries(doc.task_overrides)) {
    for (const field of OVERRIDE_FIELDS) {
      const value = override[field]
      if (typeof value === "string" && value.trim() === "") {
        errors[`task_overrides.${key}.${field}`] = [
          "An override cannot be empty — use Reset to remove it.",
        ]
      }
    }
  }
  return errors
}

const FIELD_LOCATION_RE =
  /^(session\.(?:persona|goal|base_instructions)|task_overrides\.[^\s.:]+\.(?:intro|outro|prompt)): (.*)$/

/** Split the server's "; "-joined, location-prefixed content errors (draft-save
 *  400 message and POST-preview `errors[]` use identical strings). */
export function parsePromptErrors(joined: string): ParsedErrors {
  const fields: Record<string, string[]> = {}
  const general: string[] = []
  for (const part of joined.split("; ")) {
    const message = part.trim()
    if (message === "") continue
    const match = FIELD_LOCATION_RE.exec(message)
    if (match === null) {
      general.push(message)
    } else {
      const existing = fields[match[1]] ?? []
      fields[match[1]] = [...existing, match[2]]
    }
  }
  return { fields, general }
}

// ---------------------------------------------------------------------------
// Raw schema document extraction. The ibv/types.ts UI subset intentionally
// omits `tasks`, so this module reads the raw JSON with its own narrow types.
// ---------------------------------------------------------------------------

type RawRecord = Record<string, unknown>

function asRecord(value: unknown): RawRecord | null {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as RawRecord
  }
  return null
}

function asOptionalText(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined
}

export function taskDefaultsOf(rawDoc: unknown): TaskDefaults[] {
  const tasks = asRecord(rawDoc)?.tasks
  if (!Array.isArray(tasks)) return []
  const out: TaskDefaults[] = []
  for (const raw of tasks) {
    const task = asRecord(raw)
    if (task === null) continue
    const taskKey = asOptionalText(task.task_key)
    if (taskKey === undefined) continue
    out.push({
      task_key: taskKey,
      title: asOptionalText(task.title) ?? taskKey,
      intro: asOptionalText(task.intro),
      outro: asOptionalText(task.outro),
      prompt: asOptionalText(task.prompt),
    })
  }
  return out
}

function collectContextLeaves(prefix: string, fields: unknown, out: PlaceholderEntry[]): void {
  const record = asRecord(fields)
  if (record === null) return
  for (const [key, raw] of Object.entries(record)) {
    const field = asRecord(raw)
    if (field === null) continue
    const path = `${prefix}.${key}`
    if (field.type === "group") {
      collectContextLeaves(path, field.fields, out)
    } else if (field.role === "context") {
      out.push({ token: path, detail: asOptionalText(field.title) ?? path })
    }
  }
}

/** The valid {{token}} namespace of a schema document: system_fields keys plus
 *  root-anchored paths of role:"context" leaves (spec 2026-07-08 §4). */
export function placeholderGroupsOf(rawDoc: unknown): PlaceholderGroups {
  const doc = asRecord(rawDoc)
  const system: PlaceholderEntry[] = []
  const context: PlaceholderEntry[] = []
  const systemFields = asRecord(doc?.system_fields)
  if (systemFields !== null) {
    for (const [token, path] of Object.entries(systemFields)) {
      system.push({ token, detail: asOptionalText(path) ?? "" })
    }
  }
  const sections = asRecord(doc?.sections)
  if (sections !== null) {
    for (const [sectionKey, rawSection] of Object.entries(sections)) {
      const section = asRecord(rawSection)
      if (section !== null) {
        collectContextLeaves(`sections.${sectionKey}`, section.fields, context)
      }
    }
  }
  return { system, context }
}

/** Insert `{{token}}` at the caret (append when unknown); returns the new caret. */
export function insertToken(
  text: string,
  token: string,
  caret: number | null,
): { next: string; caret: number } {
  const at = caret ?? text.length
  const inserted = `{{${token}}}`
  return { next: `${text.slice(0, at)}${inserted}${text.slice(at)}`, caret: at + inserted.length }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-frontend && npx vitest run src/lib/prompts/document.test.ts`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/prompts/document.ts vera-frontend/src/lib/prompts/document.test.ts
git commit -m "feat(frontend): pure PromptDocument editing/parsing helpers"
```

---

### Task 6: Frontend — `PlaceholderPicker` + shared `PromptTextarea`

**Files:**
- Create: `vera-frontend/src/components/agent-prompt/PlaceholderPicker.tsx`
- Create: `vera-frontend/src/components/agent-prompt/PromptTextarea.tsx`
- Test: `vera-frontend/src/components/agent-prompt/componentTests.test.tsx` (started here, extended in Task 7)

**Interfaces:**
- Consumes: `PlaceholderGroups`, `insertToken` from `@/lib/prompts/document`; ui components `dialog`, `button`, `input`, `label`, `textarea`.
- Produces:

```tsx
// PlaceholderPicker.tsx
export function PlaceholderPicker(props: {
  groups: PlaceholderGroups
  onInsert: (token: string) => void
}): JSX.Element

// PromptTextarea.tsx — label + help + textarea + picker + inline errors; owns the
// caret so insertion lands at the cursor.
export function PromptTextarea(props: {
  id: string
  label: string
  help: string
  value: string
  errors: string[]
  groups: PlaceholderGroups
  onChange: (text: string) => void
}): JSX.Element
```

- [ ] **Step 1: Write the failing test**

Create `vera-frontend/src/components/agent-prompt/componentTests.test.tsx`:

```tsx
import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { PromptTextarea } from "./PromptTextarea"
import type { PlaceholderGroups } from "@/lib/prompts/document"

const groups: PlaceholderGroups = {
  system: [{ token: "member_id", detail: "sections.basics.plan_type" }],
  context: [{ token: "sections.info.name", detail: "Name" }],
}

describe("PromptTextarea", () => {
  it("renders label, help, value and the picker trigger", () => {
    const html = renderToStaticMarkup(
      <PromptTextarea
        id="t1"
        label="Persona"
        help="Who the agent is."
        value="You are VERA."
        errors={[]}
        groups={groups}
        onChange={() => undefined}
      />,
    )
    expect(html).toContain("Persona")
    expect(html).toContain("Who the agent is.")
    expect(html).toContain("You are VERA.")
    expect(html).toContain("Insert placeholder")
  })

  it("renders inline errors and marks the textarea invalid", () => {
    const html = renderToStaticMarkup(
      <PromptTextarea
        id="t1"
        label="Persona"
        help=""
        value="Hi {{ghost}}"
        errors={["unknown placeholder {{ghost}}"]}
        groups={groups}
        onChange={() => undefined}
      />,
    )
    expect(html).toContain("unknown placeholder")
    expect(html).toContain('aria-invalid="true"')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-frontend && npx vitest run src/components/agent-prompt/componentTests.test.tsx`
Expected: FAIL — modules not found

- [ ] **Step 3: Implement both components**

Create `vera-frontend/src/components/agent-prompt/PlaceholderPicker.tsx`:

```tsx
import { useMemo, useState, type JSX } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import type { PlaceholderEntry, PlaceholderGroups } from "@/lib/prompts/document"

type PlaceholderPickerProps = {
  groups: PlaceholderGroups
  onInsert: (token: string) => void
}

function matches(entry: PlaceholderEntry, needle: string): boolean {
  const q = needle.trim().toLowerCase()
  if (q === "") return true
  return entry.token.toLowerCase().includes(q) || entry.detail.toLowerCase().includes(q)
}

function TokenList(props: {
  heading: string
  entries: PlaceholderEntry[]
  onPick: (token: string) => void
}): JSX.Element | null {
  if (props.entries.length === 0) return null
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{props.heading}</p>
      {props.entries.map((entry) => (
        <button
          key={entry.token}
          type="button"
          onClick={() => props.onPick(entry.token)}
          className="flex w-full items-baseline justify-between gap-3 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
        >
          <code className="font-mono text-xs">{`{{${entry.token}}}`}</code>
          <span className="truncate text-xs text-muted-foreground">{entry.detail}</span>
        </button>
      ))}
    </div>
  )
}

/** Searchable dialog over the pinned schema's valid placeholder tokens
 *  (system_fields keys + context-leaf paths). Selecting inserts at the caret. */
export function PlaceholderPicker(props: PlaceholderPickerProps): JSX.Element {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const system = useMemo(
    () => props.groups.system.filter((e) => matches(e, query)),
    [props.groups.system, query],
  )
  const context = useMemo(
    () => props.groups.context.filter((e) => matches(e, query)),
    [props.groups.context, query],
  )

  function pick(token: string): void {
    props.onInsert(token)
    setOpen(false)
    setQuery("")
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          Insert placeholder
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[70vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Insert placeholder</DialogTitle>
          <DialogDescription>
            Tokens hydrate per patient form at call time. Valid here: system fields and
            context-role field paths of the published schema. ({"{{value}}"} belongs to
            schema field prompts only, not to session or override text.)
          </DialogDescription>
        </DialogHeader>
        <Input
          autoFocus
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <TokenList heading="System fields" entries={system} onPick={pick} />
        <TokenList heading="Context fields" entries={context} onPick={pick} />
        {system.length === 0 && context.length === 0 && (
          <p className="text-sm text-muted-foreground">No matching placeholders.</p>
        )}
      </DialogContent>
    </Dialog>
  )
}
```

Create `vera-frontend/src/components/agent-prompt/PromptTextarea.tsx`:

```tsx
import { useRef, type JSX } from "react"

import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { PlaceholderPicker } from "@/components/agent-prompt/PlaceholderPicker"
import { insertToken, type PlaceholderGroups } from "@/lib/prompts/document"

type PromptTextareaProps = {
  id: string
  label: string
  help: string
  value: string
  errors: string[]
  groups: PlaceholderGroups
  onChange: (text: string) => void
}

/** Labeled prompt-text editor: help line, placeholder picker wired to the caret,
 *  inline (server- or client-reported) errors. */
export function PromptTextarea(props: PromptTextareaProps): JSX.Element {
  const ref = useRef<HTMLTextAreaElement>(null)

  function handleInsert(token: string): void {
    const caret = ref.current === null ? null : ref.current.selectionStart
    const { next } = insertToken(props.value, token, caret)
    props.onChange(next)
    ref.current?.focus()
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={props.id}>{props.label}</Label>
        <PlaceholderPicker groups={props.groups} onInsert={handleInsert} />
      </div>
      {props.help !== "" && <p className="text-xs text-muted-foreground">{props.help}</p>}
      <Textarea
        id={props.id}
        ref={ref}
        className="min-h-28 font-mono text-xs"
        value={props.value}
        aria-invalid={props.errors.length > 0}
        onChange={(e) => props.onChange(e.target.value)}
      />
      {props.errors.map((message) => (
        <p key={message} className="text-xs text-destructive">
          {message}
        </p>
      ))}
    </div>
  )
}
```

Note: if `Textarea` (check `src/components/ui/textarea.tsx`) does not forward refs via a `ref` prop (React 19 components generally accept `ref` as a prop), pass it through — with React 19 + the shadcn function-component style this works as written.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-frontend && npx vitest run src/components/agent-prompt/componentTests.test.tsx`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/components/agent-prompt/PlaceholderPicker.tsx vera-frontend/src/components/agent-prompt/PromptTextarea.tsx vera-frontend/src/components/agent-prompt/componentTests.test.tsx
git commit -m "feat(frontend): placeholder picker + shared prompt textarea"
```

---

### Task 7: Frontend — editor, preview, and version components

**Files:**
- Create: `vera-frontend/src/components/agent-prompt/OverrideFieldRow.tsx`
- Create: `vera-frontend/src/components/agent-prompt/SessionEditor.tsx`
- Create: `vera-frontend/src/components/agent-prompt/TaskOverrideEditor.tsx`
- Create: `vera-frontend/src/components/agent-prompt/PreviewPane.tsx`
- Create: `vera-frontend/src/components/agent-prompt/VersionList.tsx`
- Modify: `vera-frontend/src/components/agent-prompt/componentTests.test.tsx` (append)

**Interfaces:**
- Consumes: Task 4 types; Task 5 helpers; Task 6 `PromptTextarea`.
- Produces:

```tsx
export function OverrideFieldRow(props: {
  taskKey: string
  field: OverrideField
  label: string
  help: string
  state: OverrideState
  value: string                    // override text when state === "overridden"
  defaultText: string | undefined
  errors: string[]
  groups: PlaceholderGroups
  onChange: (text: string) => void // update the override
  onOverride: () => void           // start overriding (buffer gets default ?? "")
  onReset: () => void              // remove the override field
}): JSX.Element

export function SessionEditor(props: {
  session: SessionBlock
  errors: Record<string, string[]>
  groups: PlaceholderGroups
  onChange: (field: keyof SessionBlock, text: string) => void
}): JSX.Element

export function TaskOverrideEditor(props: {
  task: TaskDefaults
  document: PromptDocument
  errors: Record<string, string[]>
  groups: PlaceholderGroups
  onSet: (field: OverrideField, text: string) => void
  onClear: (field: OverrideField) => void
}): JSX.Element

export type PreviewSection = { label: string; text: string }
export function PreviewPane(props: {
  title: string
  meta: string                 // e.g. "v5 · pinned schema v2" / "unsaved changes · renders against schema v3 (published)"
  loading: boolean
  error: string | null
  sections: PreviewSection[]
}): JSX.Element

export function VersionList(props: {
  versions: PromptVersionSummary[]
  loadedVersionId: string | null
  busy: boolean
  publishingId: string | null
  onLoad: (versionId: string) => void
  onPublish: (versionId: string) => void
}): JSX.Element
```

- [ ] **Step 1: Write the failing tests**

Append to `componentTests.test.tsx` (place the new `import` lines with the existing ones at the top of the file, the `describe` blocks at the end):

```tsx
import { OverrideFieldRow } from "./OverrideFieldRow"
import { PreviewPane } from "./PreviewPane"
import { VersionList } from "./VersionList"
import type { PromptVersionSummary } from "@/lib/api/prompts"

describe("OverrideFieldRow", () => {
  const base = {
    taskKey: "wrap_up",
    field: "outro" as const,
    label: "Outro",
    help: "Spoken verbatim when the task completes.",
    errors: [] as string[],
    groups,
    onChange: () => undefined,
    onOverride: () => undefined,
    onReset: () => undefined,
  }

  it("default state: read-only default text + Override action", () => {
    const html = renderToStaticMarkup(
      <OverrideFieldRow {...base} state="default" value="" defaultText="Goodbye now." />,
    )
    expect(html).toContain("Schema default")
    expect(html).toContain("Goodbye now.")
    expect(html).toContain("Override")
    expect(html).not.toContain("Reset to default")
  })

  it("no-default state: Add action, no default text block", () => {
    const html = renderToStaticMarkup(
      <OverrideFieldRow {...base} state="no-default" value="" defaultText={undefined} />,
    )
    expect(html).toContain("No default")
    expect(html).toContain("Add")
  })

  it("overridden state: editable textarea + Reset + collapsible default", () => {
    const html = renderToStaticMarkup(
      <OverrideFieldRow {...base} state="overridden" value="Bye!" defaultText="Goodbye now." />,
    )
    expect(html).toContain("Overridden")
    expect(html).toContain("Bye!")
    expect(html).toContain("Reset to default")
    expect(html).toContain("Goodbye now.")
  })
})

describe("PreviewPane", () => {
  it("renders meta line and sections", () => {
    const html = renderToStaticMarkup(
      <PreviewPane
        title="Wrap Up"
        meta="v5 · pinned schema v2"
        loading={false}
        error={null}
        sections={[{ label: "Outro", text: "Goodbye now." }]}
      />,
    )
    expect(html).toContain("v5 · pinned schema v2")
    expect(html).toContain("Outro")
    expect(html).toContain("Goodbye now.")
  })
})

describe("VersionList", () => {
  const versions: PromptVersionSummary[] = [
    {
      id: "b",
      version: 2,
      status: "draft",
      created_at: "2026-07-09T10:00:00Z",
      schema_version_id: "s3",
      schema_version: 3,
    },
    {
      id: "a",
      version: 1,
      status: "published",
      created_at: "2026-07-08T10:00:00Z",
      schema_version_id: "s2",
      schema_version: 2,
    },
  ]

  it("shows version number, status badge, pinned schema, and actions", () => {
    const html = renderToStaticMarkup(
      <VersionList
        versions={versions}
        loadedVersionId="a"
        busy={false}
        publishingId={null}
        onLoad={() => undefined}
        onPublish={() => undefined}
      />,
    )
    expect(html).toContain("v2")
    expect(html).toContain("draft")
    expect(html).toContain("published")
    expect(html).toContain("pins schema v3")
    expect(html).toContain("Load")
    expect(html).toContain("Publish")
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-frontend && npx vitest run src/components/agent-prompt/componentTests.test.tsx`
Expected: FAIL — modules not found

- [ ] **Step 3: Implement the five components**

Create `vera-frontend/src/components/agent-prompt/OverrideFieldRow.tsx`:

```tsx
import { type JSX } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { PromptTextarea } from "@/components/agent-prompt/PromptTextarea"
import type { OverrideField, OverrideState, PlaceholderGroups } from "@/lib/prompts/document"

type OverrideFieldRowProps = {
  taskKey: string
  field: OverrideField
  label: string
  help: string
  state: OverrideState
  value: string
  defaultText: string | undefined
  errors: string[]
  groups: PlaceholderGroups
  onChange: (text: string) => void
  onOverride: () => void
  onReset: () => void
}

function DefaultBlock(props: { text: string }): JSX.Element {
  return (
    <pre className="rounded-md border bg-muted/50 p-2 font-mono text-xs whitespace-pre-wrap text-muted-foreground">
      {props.text}
    </pre>
  )
}

/** One intro/outro/instructions row with provenance: schema default (read-only,
 *  Override to edit), no-default (Add), or overridden (edit + Reset + the
 *  collapsible default for comparison). Reset REMOVES the override — empty
 *  overrides are invalid server-side (spec §3.3). */
export function OverrideFieldRow(props: OverrideFieldRowProps): JSX.Element {
  if (props.state === "overridden") {
    return (
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <Badge>Overridden</Badge>
          <Button type="button" variant="ghost" size="sm" onClick={props.onReset}>
            Reset to default
          </Button>
        </div>
        <PromptTextarea
          id={`override-${props.taskKey}-${props.field}`}
          label={props.label}
          help={props.help}
          value={props.value}
          errors={props.errors}
          groups={props.groups}
          onChange={props.onChange}
        />
        {props.defaultText !== undefined && (
          <Collapsible>
            <CollapsibleTrigger className="text-xs text-muted-foreground underline-offset-2 hover:underline">
              Schema default
            </CollapsibleTrigger>
            <CollapsibleContent forceMount className="mt-1">
              <DefaultBlock text={props.defaultText} />
            </CollapsibleContent>
          </Collapsible>
        )}
      </div>
    )
  }

  const hasDefault = props.state === "default" && props.defaultText !== undefined
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{props.label}</span>
          <Badge variant="secondary">{hasDefault ? "Schema default" : "No default"}</Badge>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={props.onOverride}>
          {hasDefault ? "Override" : "Add"}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">{props.help}</p>
      {hasDefault && props.defaultText !== undefined && <DefaultBlock text={props.defaultText} />}
    </div>
  )
}
```

Note on `forceMount`: `renderToStaticMarkup` cannot open a closed radix collapsible, and the comparison block is cheap — keep it always mounted; the trigger still toggles visibility styling. If `forceMount`'s hidden-state styling proves confusing in the browser, swap to the native `<details><summary>Schema default</summary>…</details>` element instead (no radix) — behavior requirement is only "collapsed by default, expandable".

Create `vera-frontend/src/components/agent-prompt/SessionEditor.tsx`:

```tsx
import { type JSX } from "react"

import { PromptTextarea } from "@/components/agent-prompt/PromptTextarea"
import type { SessionBlock } from "@/lib/api/prompts"
import type { PlaceholderGroups } from "@/lib/prompts/document"

type SessionEditorProps = {
  session: SessionBlock
  errors: Record<string, string[]>
  groups: PlaceholderGroups
  onChange: (field: keyof SessionBlock, text: string) => void
}

// Help texts mirror the SessionBlock Field(description=…) intents in
// vera_core/forms/prompting.py — the meaning lives where the model lives.
const SESSION_FIELDS: { field: keyof SessionBlock; label: string; help: string }[] = [
  {
    field: "persona",
    label: "Persona",
    help:
      "Who the agent is: name (VERA), voice and temperament, speech pacing habits, " +
      "how it refers to itself, pronunciation tendencies.",
  },
  {
    field: "goal",
    label: "Goal",
    help:
      "What the call is for — the north star the agent falls back on when the " +
      "conversation drifts.",
  },
  {
    field: "base_instructions",
    label: "Base instructions",
    help:
      "Global behavior rules applied across every task: turn-taking discipline, " +
      "value-recording rules, hold handling, role enforcement, anti-repetition.",
  },
]

/** The literal session block — no default/override concept here; what you see
 *  is what ships (spec §3.2). All three fields are required. */
export function SessionEditor(props: SessionEditorProps): JSX.Element {
  return (
    <div className="space-y-4">
      {SESSION_FIELDS.map(({ field, label, help }) => (
        <PromptTextarea
          key={field}
          id={`session-${field}`}
          label={label}
          help={help}
          value={props.session[field]}
          errors={props.errors[`session.${field}`] ?? []}
          groups={props.groups}
          onChange={(text) => props.onChange(field, text)}
        />
      ))}
    </div>
  )
}
```

Create `vera-frontend/src/components/agent-prompt/TaskOverrideEditor.tsx`:

```tsx
import { type JSX } from "react"

import { OverrideFieldRow } from "@/components/agent-prompt/OverrideFieldRow"
import type { PromptDocument } from "@/lib/api/prompts"
import {
  overrideStateOf,
  type OverrideField,
  type PlaceholderGroups,
  type TaskDefaults,
} from "@/lib/prompts/document"

type TaskOverrideEditorProps = {
  task: TaskDefaults
  document: PromptDocument
  errors: Record<string, string[]>
  groups: PlaceholderGroups
  onSet: (field: OverrideField, text: string) => void
  onClear: (field: OverrideField) => void
}

const TASK_FIELDS: { field: OverrideField; label: string; help: string }[] = [
  { field: "intro", label: "Intro", help: "Spoken verbatim when the task starts." },
  { field: "outro", label: "Outro", help: "Spoken verbatim when the task completes." },
  {
    field: "prompt",
    label: "Instructions",
    help:
      "Leads the compiled task prompt; schema-derived questions and rules are " +
      "appended after it.",
  },
]

/** Effective text per field = override ?? schema default; editing creates the
 *  override, Reset removes it (spec §3.3). */
export function TaskOverrideEditor(props: TaskOverrideEditorProps): JSX.Element {
  return (
    <div className="space-y-6">
      {TASK_FIELDS.map(({ field, label, help }) => {
        const defaultText = props.task[field]
        const override = props.document.task_overrides[props.task.task_key]?.[field]
        return (
          <OverrideFieldRow
            key={field}
            taskKey={props.task.task_key}
            field={field}
            label={label}
            help={help}
            state={overrideStateOf(props.document, props.task.task_key, field, defaultText)}
            value={typeof override === "string" ? override : ""}
            defaultText={defaultText}
            errors={props.errors[`task_overrides.${props.task.task_key}.${field}`] ?? []}
            groups={props.groups}
            onChange={(text) => props.onSet(field, text)}
            onOverride={() => props.onSet(field, defaultText ?? "")}
            onReset={() => props.onClear(field)}
          />
        )
      })}
    </div>
  )
}
```

Create `vera-frontend/src/components/agent-prompt/PreviewPane.tsx`:

```tsx
import { type JSX } from "react"
import { Loader2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"

export type PreviewSection = { label: string; text: string }

type PreviewPaneProps = {
  title: string
  meta: string
  loading: boolean
  error: string | null
  sections: PreviewSection[]
}

/** Dumb renderer of the selection's rendered prompt text — the operator's view
 *  of what the agent actually receives (spec §3.4 decides GET vs POST upstream). */
export function PreviewPane(props: PreviewPaneProps): JSX.Element {
  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">{props.title}</h3>
        <p className="text-xs text-muted-foreground">{props.meta}</p>
      </div>
      {props.error !== null && (
        <Alert variant="destructive">
          <AlertDescription>{props.error}</AlertDescription>
        </Alert>
      )}
      {props.loading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Rendering…
        </p>
      ) : (
        props.sections.map((section) => (
          <div key={section.label} className="space-y-1">
            <p className="text-xs font-medium text-muted-foreground">{section.label}</p>
            <pre className="max-h-96 overflow-y-auto rounded-md border bg-muted/30 p-2 font-mono text-xs whitespace-pre-wrap">
              {section.text}
            </pre>
          </div>
        ))
      )}
    </div>
  )
}
```

Create `vera-frontend/src/components/agent-prompt/VersionList.tsx`:

```tsx
import { type JSX } from "react"
import { Loader2 } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { PromptVersionSummary } from "@/lib/api/prompts"

type VersionListProps = {
  versions: PromptVersionSummary[]
  loadedVersionId: string | null
  busy: boolean
  publishingId: string | null
  onLoad: (versionId: string) => void
  onPublish: (versionId: string) => void
}

/** Version history: every save is an immutable draft; one published per prompt.
 *  Load = the copy flow (edit + Save draft → new version). */
export function VersionList(props: VersionListProps): JSX.Element {
  if (props.versions.length === 0) {
    return <p className="text-sm text-muted-foreground">No versions yet.</p>
  }
  return (
    <div className="space-y-2">
      {props.versions.map((v) => (
        <div
          key={v.id}
          className={
            v.id === props.loadedVersionId
              ? "rounded-md border border-ring p-2"
              : "rounded-md border p-2"
          }
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">v{v.version}</span>
            <Badge variant={v.status === "published" ? "default" : "secondary"}>{v.status}</Badge>
          </div>
          <p className="text-xs text-muted-foreground">
            pins schema v{v.schema_version} · {new Date(v.created_at).toLocaleDateString()}
          </p>
          <div className="mt-1.5 flex gap-1.5">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={props.busy}
              onClick={() => props.onLoad(v.id)}
            >
              Load
            </Button>
            {v.status !== "published" && (
              <Button
                type="button"
                size="sm"
                disabled={props.busy}
                onClick={() => props.onPublish(v.id)}
              >
                {props.publishingId === v.id ? <Loader2 className="animate-spin" /> : null} Publish
              </Button>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-frontend && npx vitest run src/components/agent-prompt/componentTests.test.tsx`
Expected: all PASS. If the `forceMount` collapsible does not emit the default text in static markup, apply the `<details>` fallback described in Step 3 and re-run.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/components/agent-prompt/
git commit -m "feat(frontend): agent-prompt editor, preview, and version components"
```

---

### Task 8: Frontend — rewrite the `AgentPrompt` page

**Files:**
- Rewrite: `vera-frontend/src/pages/AgentPrompt.tsx`
- Keep unchanged: `vera-frontend/src/pages/agentPrompt.helpers.ts` (`pickInitialVersion` — published else newest; already tested)

**Interfaces:**
- Consumes: everything from Tasks 4–7. Route/gating stay as-is (`/agent-prompt` in `App.tsx`, page self-gates on `selectIsSuperAdmin`).
- Produces: the working page.

**Behavior contract (spec §3.2–§3.6):**
- Prompt selector (fixes the old first-prompt-only bug — dev has two prompts).
- Rail: "Session" + schema tasks (dot when overridden) + orphaned-override warnings (override keys not in the schema's tasks, each with a Remove action) + `VersionList`.
- Preview: pristine buffer + loaded version → GET `previewPromptVersion(promptId, loadedVersionId)`, meta `v5 · pinned schema v2`; dirty (or no loaded version) → debounced 500 ms POST `previewPromptDocument`, meta `unsaved changes · renders against schema v3 (published)`; POST `errors[]` parse onto fields.
- Load with unsaved changes → discard-confirm dialog. Save = new draft, becomes the loaded version. Publish per row. Bootstrap (no versions): seed the buffer's session from GET preview's factory text.
- Save 400 → `parsePromptErrors` onto fields; unmapped + 409s → page-level Alert.

- [ ] **Step 1: Rewrite the page**

Replace the full contents of `vera-frontend/src/pages/AgentPrompt.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useState, type JSX } from "react"
import { Bot, Loader2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Select } from "@/components/ui/select"
import { PreviewPane, type PreviewSection } from "@/components/agent-prompt/PreviewPane"
import { SessionEditor } from "@/components/agent-prompt/SessionEditor"
import { TaskOverrideEditor } from "@/components/agent-prompt/TaskOverrideEditor"
import { VersionList } from "@/components/agent-prompt/VersionList"
import { ApiError } from "@/lib/api/client"
import {
  createPromptDraft,
  getPromptSchema,
  getPromptVersion,
  listPromptVersions,
  listPrompts,
  previewPromptDocument,
  previewPromptVersion,
  publishPromptVersion,
  type PromptDocument,
  type PromptSchemaDetail,
  type PromptSummary,
  type PromptVersionSummary,
  type RenderedPrompts,
  type SessionBlock,
} from "@/lib/api/prompts"
import {
  clearOverrideField,
  clientValidationErrors,
  documentsEqual,
  normalizeDocument,
  parsePromptErrors,
  placeholderGroupsOf,
  removeOverrideEntry,
  setOverrideField,
  taskDefaultsOf,
  type OverrideField,
  type ParsedErrors,
} from "@/lib/prompts/document"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"
import { pickInitialVersion } from "@/pages/agentPrompt.helpers"

type Selection = { kind: "session" } | { kind: "task"; taskKey: string }

const NO_ERRORS: ParsedErrors = { fields: {}, general: [] }

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback
}

export function AgentPrompt(): JSX.Element {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [prompts, setPrompts] = useState<PromptSummary[]>([])
  const [promptId, setPromptId] = useState<string | null>(null)
  const [versions, setVersions] = useState<PromptVersionSummary[]>([])
  const [schema, setSchema] = useState<PromptSchemaDetail | null>(null)
  const [doc, setDoc] = useState<PromptDocument | null>(null)
  const [baseline, setBaseline] = useState<PromptDocument | null>(null)
  const [loadedVersionId, setLoadedVersionId] = useState<string | null>(null)
  const [selection, setSelection] = useState<Selection>({ kind: "session" })
  const [preview, setPreview] = useState<RenderedPrompts | null>(null)
  const [previewErrors, setPreviewErrors] = useState<ParsedErrors>(NO_ERRORS)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [saveErrors, setSaveErrors] = useState<ParsedErrors>(NO_ERRORS)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [publishingId, setPublishingId] = useState<string | null>(null)
  const [pendingLoadId, setPendingLoadId] = useState<string | null>(null)

  const tasks = useMemo(() => (schema === null ? [] : taskDefaultsOf(schema.document)), [schema])
  const groups = useMemo(
    () => (schema === null ? { system: [], context: [] } : placeholderGroupsOf(schema.document)),
    [schema],
  )
  const dirty = doc !== null && baseline !== null && !documentsEqual(doc, baseline)
  const clientErrors = useMemo(() => (doc === null ? {} : clientValidationErrors(doc)), [doc])
  const fieldErrors = useMemo(() => {
    const merged: Record<string, string[]> = { ...clientErrors }
    for (const source of [previewErrors.fields, saveErrors.fields]) {
      for (const [key, messages] of Object.entries(source)) {
        merged[key] = [...(merged[key] ?? []), ...messages]
      }
    }
    return merged
  }, [clientErrors, previewErrors.fields, saveErrors.fields])
  const generalErrors = useMemo(
    () => [...previewErrors.general, ...saveErrors.general],
    [previewErrors.general, saveErrors.general],
  )
  const orphanedKeys = useMemo(() => {
    if (doc === null) return []
    const known = new Set(tasks.map((t) => t.task_key))
    return Object.keys(doc.task_overrides).filter((key) => !known.has(key))
  }, [doc, tasks])
  const loadedVersion = versions.find((v) => v.id === loadedVersionId) ?? null

  const loadVersionIntoBuffer = useCallback(async (pid: string, versionId: string) => {
    const detail = await getPromptVersion(pid, versionId)
    const normalized = normalizeDocument(detail.composite_json)
    setDoc(normalized)
    setBaseline(normalized)
    setLoadedVersionId(versionId)
    setSaveErrors(NO_ERRORS)
  }, [])

  // Load the catalog once.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listPrompts()
      .then((list) => {
        if (cancelled) return
        setPrompts(list)
        setPromptId(list[0]?.id ?? null)
        if (list.length === 0) setLoading(false)
      })
      .catch((err) => {
        if (cancelled) return
        setError(errorMessage(err, "Could not load prompts."))
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  // (Re)load versions + schema + initial buffer when the selected prompt changes.
  useEffect(() => {
    if (promptId === null) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setSchema(null)
    setDoc(null)
    setBaseline(null)
    setLoadedVersionId(null)
    setSelection({ kind: "session" })
    setPreview(null)
    setPreviewErrors(NO_ERRORS)
    setSaveErrors(NO_ERRORS)
    async function bootstrap(pid: string): Promise<void> {
      const [vs, schemaDetail] = await Promise.all([listPromptVersions(pid), getPromptSchema(pid)])
      if (cancelled) return
      setVersions(vs)
      setSchema(schemaDetail)
      const initial = pickInitialVersion(vs)
      if (initial !== undefined) {
        await loadVersionIntoBuffer(pid, initial.id)
        return
      }
      // Bootstrap gap: no versions — seed the session from the factory render.
      const factory = await previewPromptVersion(pid)
      if (cancelled) return
      const seeded: PromptDocument = {
        kind: "prompt_document",
        session: {
          persona: factory.persona,
          goal: factory.goal,
          base_instructions: factory.base_instructions,
        },
        task_overrides: {},
      }
      setDoc(seeded)
      setBaseline(seeded)
    }
    bootstrap(promptId)
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err, "Could not load the prompt."))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [promptId, loadVersionIntoBuffer])

  // Preview: authoritative GET for a pristine loaded version; debounced stateless
  // POST for a dirty buffer (spec §3.4).
  useEffect(() => {
    if (promptId === null || doc === null) return
    let cancelled = false
    setPreviewLoading(true)
    setPreviewError(null)
    const pristine = !dirty && loadedVersionId !== null
    const buffer = doc

    async function renderPristine(pid: string, versionId: string): Promise<void> {
      const rendered = await previewPromptVersion(pid, versionId)
      if (cancelled) return
      setPreview(rendered)
      setPreviewErrors(NO_ERRORS)
    }
    async function renderBuffer(pid: string): Promise<void> {
      const result = await previewPromptDocument(pid, normalizeDocument(buffer))
      if (cancelled) return
      setPreview(result.rendered)
      setPreviewErrors(parsePromptErrors(result.errors.join("; ")))
    }

    function run(task: Promise<void>): void {
      task
        .catch((err) => {
          if (!cancelled) setPreviewError(errorMessage(err, "Could not render the preview."))
        })
        .finally(() => {
          if (!cancelled) setPreviewLoading(false)
        })
    }

    if (pristine && loadedVersionId !== null) {
      run(renderPristine(promptId, loadedVersionId))
      return () => {
        cancelled = true
      }
    }
    const timer = setTimeout(() => run(renderBuffer(promptId)), 500)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [promptId, doc, dirty, loadedVersionId])

  if (!isSuperAdmin) {
    return <p className="text-sm text-muted-foreground">This page is only available to platform operators.</p>
  }

  async function refreshVersions(pid: string): Promise<void> {
    setVersions(await listPromptVersions(pid))
  }

  async function onSave(): Promise<void> {
    if (promptId === null || doc === null || Object.keys(clientErrors).length > 0) return
    setBusy(true)
    setError(null)
    try {
      const created = await createPromptDraft(promptId, normalizeDocument(doc))
      const normalized = normalizeDocument(created.composite_json)
      setDoc(normalized)
      setBaseline(normalized)
      setLoadedVersionId(created.id)
      setSaveErrors(NO_ERRORS)
      await refreshVersions(promptId)
    } catch (err) {
      if (err instanceof ApiError && err.httpStatus === 400) {
        setSaveErrors(parsePromptErrors(err.message))
      } else {
        setError(errorMessage(err, "Could not save the draft."))
      }
    } finally {
      setBusy(false)
    }
  }

  async function onPublish(versionId: string): Promise<void> {
    if (promptId === null) return
    setPublishingId(versionId)
    setError(null)
    try {
      await publishPromptVersion(promptId, versionId)
      await refreshVersions(promptId)
    } catch (err) {
      setError(errorMessage(err, "Could not publish."))
    } finally {
      setPublishingId(null)
    }
  }

  function onLoadRequest(versionId: string): void {
    if (dirty) {
      setPendingLoadId(versionId)
      return
    }
    void onLoadConfirmed(versionId)
  }

  async function onLoadConfirmed(versionId: string): Promise<void> {
    if (promptId === null) return
    setPendingLoadId(null)
    setError(null)
    try {
      await loadVersionIntoBuffer(promptId, versionId)
    } catch (err) {
      setError(errorMessage(err, "Could not load the version."))
    }
  }

  function onSessionChange(field: keyof SessionBlock, text: string): void {
    if (doc === null) return
    setDoc({ ...doc, session: { ...doc.session, [field]: text } })
  }

  function onOverrideSet(taskKey: string, field: OverrideField, text: string): void {
    if (doc === null) return
    setDoc(setOverrideField(doc, taskKey, field, text))
  }

  function onOverrideClear(taskKey: string, field: OverrideField): void {
    if (doc === null) return
    setDoc(clearOverrideField(doc, taskKey, field))
  }

  const selectedTask =
    selection.kind === "task" ? (tasks.find((t) => t.task_key === selection.taskKey) ?? null) : null
  const previewTask =
    selection.kind === "task"
      ? (preview?.tasks.find((t) => t.task_key === selection.taskKey) ?? null)
      : null
  const previewSections: PreviewSection[] =
    selection.kind === "session"
      ? [
          { label: "Persona", text: preview?.persona ?? "" },
          { label: "Goal", text: preview?.goal ?? "" },
          { label: "Base instructions", text: preview?.base_instructions ?? "" },
        ]
      : [
          { label: "Intro (spoken on entry)", text: previewTask?.intro ?? "— none —" },
          { label: "Compiled instructions", text: previewTask?.prompt ?? "" },
          { label: "Outro (spoken on exit)", text: previewTask?.outro ?? "— none —" },
        ]
  const previewMeta =
    !dirty && loadedVersion !== null
      ? `v${loadedVersion.version} · pinned schema v${loadedVersion.schema_version}`
      : `unsaved changes · renders against schema v${schema?.version ?? "?"} (published)`
  const saveDisabled = busy || !dirty || Object.keys(clientErrors).length > 0 || schema === null

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Agent Prompt</h1>
          <p className="text-sm text-muted-foreground">
            Session text and per-task overrides; prompts render from the schema at call time.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {prompts.length > 1 && (
            <div className="w-56">
              <Select value={promptId ?? ""} onChange={(e) => setPromptId(e.target.value)}>
                {prompts.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </Select>
            </div>
          )}
          {dirty && <Badge variant="secondary">Unsaved changes</Badge>}
          <Button type="button" onClick={() => void onSave()} disabled={saveDisabled}>
            {busy ? <Loader2 className="animate-spin" /> : null} Save draft
          </Button>
        </div>
      </div>

      {error !== null && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {generalErrors.map((message) => (
        <Alert key={message} variant="destructive">
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      ))}

      {loading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading…
        </p>
      ) : promptId === null ? (
        <p className="text-sm text-muted-foreground">No prompts found.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[230px_minmax(0,1fr)_minmax(0,1fr)]">
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Bot className="size-4 text-muted-foreground" /> Sections
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-1">
                <button
                  type="button"
                  onClick={() => setSelection({ kind: "session" })}
                  className={
                    selection.kind === "session"
                      ? "w-full rounded-md bg-muted px-2 py-1.5 text-left text-sm font-medium"
                      : "w-full rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
                  }
                >
                  Session
                </button>
                <p className="px-2 pt-2 text-xs font-medium text-muted-foreground">Tasks</p>
                {tasks.map((task) => {
                  const active = selection.kind === "task" && selection.taskKey === task.task_key
                  const overridden = doc !== null && task.task_key in doc.task_overrides
                  return (
                    <button
                      key={task.task_key}
                      type="button"
                      onClick={() => setSelection({ kind: "task", taskKey: task.task_key })}
                      className={
                        active
                          ? "flex w-full items-center justify-between rounded-md bg-muted px-2 py-1.5 text-left text-sm font-medium"
                          : "flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
                      }
                    >
                      <span className="truncate">{task.title}</span>
                      {overridden && <span className="size-1.5 shrink-0 rounded-full bg-primary" />}
                    </button>
                  )
                })}
                {orphanedKeys.map((key) => (
                  <div
                    key={key}
                    className="flex items-center justify-between gap-2 rounded-md border border-destructive/50 px-2 py-1.5"
                  >
                    <span className="truncate text-xs text-destructive">
                      {key}: override for a task not in the published schema
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => doc !== null && setDoc(removeOverrideEntry(doc, key))}
                    >
                      Remove
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Versions</CardTitle>
              </CardHeader>
              <CardContent>
                <VersionList
                  versions={versions}
                  loadedVersionId={loadedVersionId}
                  busy={busy || publishingId !== null}
                  publishingId={publishingId}
                  onLoad={onLoadRequest}
                  onPublish={(id) => void onPublish(id)}
                />
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                {selection.kind === "session" ? "Session" : (selectedTask?.title ?? "Task")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {doc !== null && selection.kind === "session" && (
                <SessionEditor
                  session={doc.session}
                  errors={fieldErrors}
                  groups={groups}
                  onChange={onSessionChange}
                />
              )}
              {doc !== null && selectedTask !== null && (
                <TaskOverrideEditor
                  task={selectedTask}
                  document={doc}
                  errors={fieldErrors}
                  groups={groups}
                  onSet={(field, text) => onOverrideSet(selectedTask.task_key, field, text)}
                  onClear={(field) => onOverrideClear(selectedTask.task_key, field)}
                />
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="pt-6">
              <PreviewPane
                title={selection.kind === "session" ? "Session text" : (selectedTask?.title ?? "")}
                meta={previewMeta}
                loading={previewLoading}
                error={previewError}
                sections={previewSections}
              />
            </CardContent>
          </Card>
        </div>
      )}

      <Dialog open={pendingLoadId !== null} onOpenChange={(open) => !open && setPendingLoadId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Discard unsaved changes?</DialogTitle>
            <DialogDescription>
              Loading another version replaces your unsaved edits. Save a draft first if you want
              to keep them.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPendingLoadId(null)}>
              Keep editing
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => pendingLoadId !== null && void onLoadConfirmed(pendingLoadId)}
            >
              Discard and load
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
```

- [ ] **Step 2: Run the full frontend gate**

Run: `cd vera-frontend && npm run lint && npm test && npm run build`
Expected: all green. Common trip-points: unused imports after the rewrite; `react-hooks/exhaustive-deps` on the preview effect (its deps are exactly `[promptId, doc, dirty, loadedVersionId]` — `dirty` is derived from `doc`/`baseline`, so the linter may ask for `baseline`; adding it is harmless).

- [ ] **Step 3: Manually verify against the running backend**

```bash
cd vera-backend && just up && just migrate && just seed && just api   # terminal 1
cd vera-frontend && npm run dev                                       # terminal 2
```

Log in as the platform operator, open `/agent-prompt`, and check: prompt selector shows both prompts; session text loads from factory v1; selecting a task shows defaults with `Schema default` badges; overriding the wrap_up outro flips it to `Overridden`, preview meta switches to "unsaved changes · renders against schema v…", and the preview updates ~0.5 s after typing; inserting `{{member_id}}` via the picker works; typing `{{bogus}}` surfaces `unknown placeholder` inline on that field; Reset removes the override; Save draft creates v2 (draft) and the meta flips to `v2 · pinned schema v…`; Publish v2 demotes v1.

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/pages/AgentPrompt.tsx
git commit -m "feat(frontend): rebuild /agent-prompt as a 3-pane PromptDocument editor"
```

---

### Task 9: Simplify + full gates (repo mandate)

**Files:**
- Possibly modify: everything touched in Tasks 1–8

- [ ] **Step 1: Run the code-simplifier agent**

Trigger the `code-simplifier` agent from `code-simplifier@claude-plugins-official` on the changes from Tasks 1–8 (repo CLAUDE.md mandate — behavior-preserving clarity/consistency cleanup).

- [ ] **Step 2: Re-run every gate**

```bash
cd vera-backend && just check
cd vera-frontend && npm run lint && npm test && npm run build
```

Expected: all green. If the simplifier changed behavior anywhere, revert that hunk — it must be behavior-preserving.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: code-simplifier pass over the agent-prompt editor work"
```

(Skip the commit if the simplifier produced no changes.)

---

## Execution notes

- Tasks 1–3 are backend-sequential (same file); Tasks 4–7 are frontend-sequential (each consumes the previous); Task 8 needs 4–7; Task 9 is last. Task 4 leaves the frontend temporarily non-compiling (old page imports) — do not run `npm run build` between Tasks 4 and 8; `vitest` is unaffected.
- Backend integration tests need local infra once: `cd vera-backend && just up && just migrate`.
- Spec: `docs/superpowers/specs/2026-07-09-agent-prompt-editor-design.md`. Renderer/validation source of truth: `vera-backend/packages/vera_core/src/vera_core/forms/prompting.py`. Existing endpoint behaviors: `vera-backend/tests/integration/control_plane/test_prompts.py`.





