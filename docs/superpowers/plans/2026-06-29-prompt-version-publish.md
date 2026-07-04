# Prompt editor + version publish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a SUPER_ADMIN edit a prompt, save it as a draft version, and publish it (with version history), via a platform-scoped API + the `/agent-prompt` page.

**Architecture:** New platform-gated `api/v1/prompts.py` exposes the existing `prompt`/`prompt_version` catalog (no `tenant_id`, no RLS → request-path platform session reads/writes directly). Each "save" creates an immutable draft `prompt_version`; "publish" promotes one and demotes the prior published (enforced by the `uq_prompt_version_published_per_prompt` index from migration 0019). Frontend replaces the `/agent-prompt` stub with an editor + history.

**Tech Stack:** FastAPI + SQLAlchemy async (backend), React + Vite + TS + Redux Toolkit + shadcn (frontend), pytest + vitest.

## Global Constraints

- Backend: PEP 695 type params; `asyncio` only; `ResponseModel[T]` via `ok(...)`, never bare dicts; errors via `CustomAPIException` subclasses (`NotFoundError`/`ConflictError`/`BadRequestError`), never `HTTPException`; declare `response_model` + `responses=CustomAPIResponse.custom(...)` on routes; DB timestamps from `func.now()` only.
- Endpoints gated by `platform_require("platform:elevations:read")` (reuse — no new permission, no re-seed), session via `platform_scoped_session`.
- Versions are immutable rows; never edit a version's `composite_json` in place.
- `VersionStatus.DRAFT == "draft"`, `VersionStatus.PUBLISHED == "published"`.
- Frontend: ES modules; `function` keyword for top-level funcs; explicit return types; no nested ternaries; selectors are arrow-consts matching the existing `authSlice` style.
- After implementation, run **"simplify code"** then re-run checks (`just check`; `tsc`/`eslint`/tests/build) before committing (repo CLAUDE.md).

---

### Task 1: Backend — prompt read endpoints + router registration

**Files:**
- Create: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/__init__.py` (register router)
- Test: `vera-backend/tests/integration/control_plane/test_prompts.py`

**Interfaces:**
- Consumes: `platform_scoped_session`, `platform_require` (`control_plane.deps` / `control_plane.auth.rbac`); models `Prompt`, `PromptVersion`, `FormSchema`, `SchemaVersion` (`vera_core.models`); `VersionStatus` (`vera_core.models.enums`).
- Produces: router `router` (prefix `/prompts`); DTOs `PromptSummary`, `PromptVersionSummary`, `PromptVersionDetail`; helper `async def _require_prompt(session, prompt_id) -> Prompt`.

- [ ] **Step 1: Write `prompts.py` with DTOs, the prompt helper, and the three GET endpoints**

```python
"""Platform (SUPER_ADMIN) prompt-authoring catalog routes.

The prompt / prompt_version catalog is GLOBAL (no tenant_id, no RLS) and curated by
a platform operator. Authorization is platform_require (account_type='platform' + the
reused platform:elevations:read grant); no tenant context. Versions are immutable —
each save is a new draft; publishing promotes one and demotes the prior published
(uq_prompt_version_published_per_prompt enforces one published per prompt).
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import platform_scoped_session
from control_plane.exceptions import ConflictError, CustomAPIResponse, DefaultExceptionCode, NotFoundError
from control_plane.responses import ResponseModel, ok
from fastapi import Depends
from vera_core.models import FormSchema, Prompt, PromptVersion, SchemaVersion
from vera_core.models.enums import VersionStatus

router = APIRouter(prefix="/prompts", tags=["prompts"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:elevations:read")


class PromptSummary(BaseModel):
    id: UUID
    name: str
    insurance_type: str
    published_version: int | None


class PromptVersionSummary(BaseModel):
    id: UUID
    version: int
    status: str
    created_at: datetime


class PromptVersionDetail(BaseModel):
    id: UUID
    version: int
    status: str
    created_at: datetime
    composite_json: dict[str, Any]


def _detail(v: PromptVersion) -> PromptVersionDetail:
    return PromptVersionDetail(
        id=v.id,
        version=v.version,
        status=v.status,
        created_at=v.created_at,
        composite_json=v.composite_json,
    )


async def _require_prompt(session: AsyncSession, prompt_id: UUID) -> Prompt:
    prompt = (
        await session.execute(select(Prompt).where(Prompt.id == prompt_id))
    ).scalar_one_or_none()
    if prompt is None:
        raise NotFoundError(message="unknown prompt")
    return prompt


@router.get(
    "",
    response_model=ResponseModel[list[PromptSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_prompts(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[PromptSummary]]:
    rows = (
        await session.execute(
            select(Prompt.id, Prompt.name, FormSchema.insurance_type)
            .join(FormSchema, Prompt.schema_id == FormSchema.id)
            .order_by(Prompt.name)
        )
    ).all()
    summaries: list[PromptSummary] = []
    for row in rows:
        published_version = (
            await session.execute(
                select(PromptVersion.version).where(
                    PromptVersion.prompt_id == row.id,
                    PromptVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
        summaries.append(
            PromptSummary(
                id=row.id,
                name=row.name,
                insurance_type=row.insurance_type,
                published_version=published_version,
            )
        )
    return ok(summaries)


@router.get(
    "/{prompt_id}/versions",
    response_model=ResponseModel[list[PromptVersionSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def list_versions(
    prompt_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[PromptVersionSummary]]:
    await _require_prompt(session, prompt_id)
    rows = (
        await session.execute(
            select(
                PromptVersion.id,
                PromptVersion.version,
                PromptVersion.status,
                PromptVersion.created_at,
            )
            .where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.version.desc())
        )
    ).all()
    return ok([PromptVersionSummary(id=r.id, version=r.version, status=r.status, created_at=r.created_at) for r in rows])


@router.get(
    "/{prompt_id}/versions/{version_id}",
    response_model=ResponseModel[PromptVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def get_version(
    prompt_id: UUID,
    version_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptVersionDetail]:
    version = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id, PromptVersion.prompt_id == prompt_id
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError(message="unknown prompt version")
    return ok(_detail(version))
```

> Note: keep imports sorted per `ruff`; the `from fastapi import Depends` line above should be merged into the top fastapi import (`from fastapi import APIRouter, Depends, status`). `ConflictError` is imported now because Task 2 uses it (avoids re-touching imports).

- [ ] **Step 2: Register the router**

In `api/v1/__init__.py`, mirror the existing pattern: add an import alongside the others (e.g. `from control_plane.api.v1.prompts import router as prompts_router`) and `router.include_router(prompts_router)` next to `platform_router`.

- [ ] **Step 3: Write the read-path test**

```python
# tests/integration/control_plane/test_prompts.py
# Reuse the `world` fixture pattern from test_platform_elevation.py: it seeds roles,
# tenants, a platform super_token + a tenant_admin_token. Extend a local fixture to
# also seed a FormSchema + published SchemaVersion + Prompt + published PromptVersion.
```

Add a fixture that, inside the seeded world, inserts: a `FormSchema(insurance_type="infertility_treatment", name="IBV")`, a published `SchemaVersion`, a `Prompt(schema_id=..., name="IBV Standard Prompt")`, and a published `PromptVersion(version=1, composite_json={"name": "IBV Standard Prompt", "format": "text", "source": "x", "prompt": "hello"}, status=PUBLISHED, schema_version_id=...)`. Then:

```python
async def test_list_prompts_and_versions(prompts_world) -> None:
    client, w, ids = prompts_world
    listed = await client.get("/api/v1/prompts", headers=_auth(w.super_token))
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert any(p["id"] == str(ids.prompt_id) and p["published_version"] == 1 for p in data)

    versions = await client.get(f"/api/v1/prompts/{ids.prompt_id}/versions", headers=_auth(w.super_token))
    assert versions.status_code == 200
    assert versions.json()["data"][0]["status"] == "published"

    detail = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/versions/{ids.version_id}", headers=_auth(w.super_token)
    )
    assert detail.json()["data"]["composite_json"]["prompt"] == "hello"


async def test_tenant_user_forbidden(prompts_world) -> None:
    client, w, ids = prompts_world
    resp = await client.get("/api/v1/prompts", headers=_auth(w.tenant_admin_token))
    assert resp.status_code == 403
```

- [ ] **Step 4: Run + verify**

Run: `cd vera-backend && uv run --active pytest tests/integration/control_plane/test_prompts.py -q`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py vera-backend/apps/control_plane/src/control_plane/api/v1/__init__.py vera-backend/tests/integration/control_plane/test_prompts.py
git commit -m "feat(prompts): platform read API for the prompt catalog"
```

---

### Task 2: Backend — create-draft endpoint

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`
- Test: `vera-backend/tests/integration/control_plane/test_prompts.py`

**Interfaces:**
- Consumes: `_require_prompt`, `_detail`, models, `VersionStatus`.
- Produces: `POST /prompts/{prompt_id}/versions` (201); DTO `CreateDraftRequest`.

- [ ] **Step 1: Add the DTO + endpoint**

```python
class CreateDraftRequest(BaseModel):
    composite_json: dict[str, Any]


@router.post(
    "/{prompt_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=ResponseModel[PromptVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def create_draft(
    prompt_id: UUID,
    body: CreateDraftRequest,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptVersionDetail]:
    prompt = await _require_prompt(session, prompt_id)
    published_schema_id = (
        await session.execute(
            select(SchemaVersion.id).where(
                SchemaVersion.schema_id == prompt.schema_id,
                SchemaVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
    if published_schema_id is None:
        raise ConflictError(message="no published schema to bind the prompt to")
    max_version = (
        await session.execute(
            select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt.id)
        )
    ).scalar()
    draft = PromptVersion(
        prompt_id=prompt.id,
        schema_version_id=published_schema_id,
        version=(max_version or 0) + 1,
        composite_json=body.composite_json,
        status=VersionStatus.DRAFT,
    )
    session.add(draft)
    await session.flush()
    return ok(_detail(draft))
```

- [ ] **Step 2: Add tests**

```python
async def test_create_draft_increments_version(prompts_world) -> None:
    client, w, ids = prompts_world
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions",
        headers=_auth(w.super_token),
        json={"composite_json": {"name": "IBV Standard Prompt", "format": "text", "source": "x", "prompt": "edited"}},
    )
    assert resp.status_code == 201, resp.text
    d = resp.json()["data"]
    assert d["version"] == 2 and d["status"] == "draft"
    assert d["composite_json"]["prompt"] == "edited"
```

For the no-published-schema 409 path, add a fixture variant (or demote the schema version inside the test) and assert `POST .../versions` → 409. Minimal version:

```python
async def test_create_draft_without_published_schema_conflicts(
    prompts_world, admin_sessionmaker
) -> None:
    client, w, ids = prompts_world
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE schema_version SET status='draft' WHERE id=:i").bindparams(i=ids.schema_version_id)
        )
    resp = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions",
        headers=_auth(w.super_token),
        json={"composite_json": {"prompt": "x"}},
    )
    assert resp.status_code == 409
```

- [ ] **Step 3: Run + verify**

Run: `cd vera-backend && uv run --active pytest tests/integration/control_plane/test_prompts.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py vera-backend/tests/integration/control_plane/test_prompts.py
git commit -m "feat(prompts): create draft version endpoint"
```

---

### Task 3: Backend — publish endpoint

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`
- Test: `vera-backend/tests/integration/control_plane/test_prompts.py`

**Interfaces:**
- Produces: `POST /prompts/{prompt_id}/versions/{version_id}/publish`.

- [ ] **Step 1: Add the endpoint**

```python
@router.post(
    "/{prompt_id}/versions/{version_id}/publish",
    response_model=ResponseModel[PromptVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def publish_version(
    prompt_id: UUID,
    version_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptVersionDetail]:
    target = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id, PromptVersion.prompt_id == prompt_id
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError(message="unknown prompt version")
    if target.status == VersionStatus.PUBLISHED:
        return ok(_detail(target))  # idempotent no-op
    current = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt_id,
                PromptVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
    if current is not None:
        # Demote first to free uq_prompt_version_published_per_prompt before publishing.
        current.status = VersionStatus.DRAFT
        await session.flush()
    target.status = VersionStatus.PUBLISHED
    await session.flush()
    return ok(_detail(target))
```

- [ ] **Step 2: Add test**

```python
async def test_publish_promotes_and_demotes(prompts_world) -> None:
    client, w, ids = prompts_world
    draft = (await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions",
        headers=_auth(w.super_token),
        json={"composite_json": {"prompt": "v2"}},
    )).json()["data"]
    pub = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions/{draft['id']}/publish",
        headers=_auth(w.super_token),
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["data"]["status"] == "published"

    versions = (await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/versions", headers=_auth(w.super_token)
    )).json()["data"]
    published = [v for v in versions if v["status"] == "published"]
    assert len(published) == 1 and published[0]["version"] == 2
```

- [ ] **Step 3: Run + verify**

Run: `cd vera-backend && uv run --active pytest tests/integration/control_plane/test_prompts.py -q`
Expected: PASS (all prompt tests).

- [ ] **Step 4: Lint + typecheck the new module**

Run: `cd vera-backend && uv run --active ruff format apps/control_plane/src/control_plane/api/v1/prompts.py && uv run --active ruff check apps/control_plane/src/control_plane/api/v1/prompts.py && uv run --active mypy apps/control_plane/src/control_plane/api/v1/prompts.py`
Expected: clean (ignore third-party `import-untyped` notes).

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py vera-backend/tests/integration/control_plane/test_prompts.py
git commit -m "feat(prompts): publish endpoint (one published per prompt)"
```

---

### Task 4: Frontend — prompts API module

**Files:**
- Create: `vera-frontend/src/lib/api/prompts.ts`

**Interfaces:**
- Consumes: `apiRequest` (`@/lib/api/client`).
- Produces: types `PromptSummary`, `PromptVersionSummary`, `PromptVersionDetail`, `CompositeJson`; functions `listPrompts`, `listPromptVersions`, `getPromptVersion`, `createPromptDraft`, `publishPromptVersion`.

- [ ] **Step 1: Write the module**

```ts
// Platform (super admin) prompt-catalog endpoints. Mirrors backend api/v1/prompts.py.
import { apiRequest } from "@/lib/api/client"

export type CompositeJson = {
  name?: string
  format?: string
  source?: string
  prompt: string
  [key: string]: unknown
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
}

export type PromptVersionDetail = PromptVersionSummary & {
  composite_json: CompositeJson
}

export function listPrompts() {
  return apiRequest<PromptSummary[]>("/prompts")
}

export function listPromptVersions(promptId: string) {
  return apiRequest<PromptVersionSummary[]>(`/prompts/${encodeURIComponent(promptId)}/versions`)
}

export function getPromptVersion(promptId: string, versionId: string) {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}`,
  )
}

export function createPromptDraft(promptId: string, compositeJson: CompositeJson) {
  return apiRequest<PromptVersionDetail>(`/prompts/${encodeURIComponent(promptId)}/versions`, {
    method: "POST",
    body: { composite_json: compositeJson },
  })
}

export function publishPromptVersion(promptId: string, versionId: string) {
  return apiRequest<PromptVersionDetail>(
    `/prompts/${encodeURIComponent(promptId)}/versions/${encodeURIComponent(versionId)}/publish`,
    { method: "POST" },
  )
}
```

- [ ] **Step 2: Typecheck**

Run: `cd vera-frontend && npx tsc -b`
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add vera-frontend/src/lib/api/prompts.ts
git commit -m "feat(prompts): frontend api module"
```

---

### Task 5: Frontend — Agent Prompt editor page

**Files:**
- Create: `vera-frontend/src/pages/AgentPrompt.tsx`
- Modify: `vera-frontend/src/App.tsx` (route `agent-prompt` → `<AgentPrompt />` instead of `<Placeholder>`)
- Test: `vera-frontend/src/pages/agentPrompt.helpers.test.ts` (+ extract a tiny pure helper if needed)

**Interfaces:**
- Consumes: the `prompts.ts` functions; `selectIsSuperAdmin` (`@/store/authSlice`); shadcn `Card`, `Button`, `Label`, `Textarea`, `Badge`, `Alert`.
- Produces: `export function AgentPrompt()`.

- [ ] **Step 1: Write the page**

```tsx
import { useCallback, useEffect, useState, type FormEvent } from "react"
import { Bot, Loader2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api/client"
import {
  createPromptDraft,
  getPromptVersion,
  listPromptVersions,
  listPrompts,
  publishPromptVersion,
  type CompositeJson,
  type PromptSummary,
  type PromptVersionSummary,
} from "@/lib/api/prompts"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

export function AgentPrompt() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [prompt, setPrompt] = useState<PromptSummary | null>(null)
  const [versions, setVersions] = useState<PromptVersionSummary[]>([])
  const [composite, setComposite] = useState<CompositeJson | null>(null)
  const [text, setText] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<"save" | "publish" | null>(null)

  const loadVersionInto = useCallback(async (promptId: string, versionId: string) => {
    const detail = await getPromptVersion(promptId, versionId)
    setComposite(detail.composite_json)
    setText(detail.composite_json.prompt ?? "")
  }, [])

  const refresh = useCallback(async (promptId: string) => {
    const vs = await listPromptVersions(promptId)
    setVersions(vs)
    const current = vs.find((v) => v.status === "published") ?? vs[0]
    if (current) await loadVersionInto(promptId, current.id)
  }, [loadVersionInto])

  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listPrompts()
      .then(async (prompts) => {
        if (cancelled) return
        const first = prompts[0] ?? null
        setPrompt(first)
        if (first) await refresh(first.id)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load prompts.")
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin, refresh])

  if (!isSuperAdmin) {
    return <p className="text-sm text-muted-foreground">This page is only available to platform operators.</p>
  }

  async function onSaveDraft(e: FormEvent) {
    e.preventDefault()
    if (!prompt || !composite) return
    setBusy("save")
    setError(null)
    try {
      await createPromptDraft(prompt.id, { ...composite, prompt: text })
      await refresh(prompt.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the draft.")
    } finally {
      setBusy(null)
    }
  }

  async function onPublish(versionId: string) {
    if (!prompt) return
    setBusy("publish")
    setError(null)
    try {
      await publishPromptVersion(prompt.id, versionId)
      await refresh(prompt.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not publish.")
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Agent Prompt</h1>
        <p className="text-sm text-muted-foreground">
          Edit the agent prompt, save it as a draft, and publish a version.
        </p>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {loading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading…
        </p>
      ) : !prompt ? (
        <p className="text-sm text-muted-foreground">No prompts found.</p>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Bot className="size-4 text-muted-foreground" />
                {prompt.name}
              </CardTitle>
              <CardDescription>{prompt.insurance_type}</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={onSaveDraft} className="space-y-3">
                <div className="space-y-1.5">
                  <Label htmlFor="prompt-text">Prompt</Label>
                  <Textarea
                    id="prompt-text"
                    className="min-h-80 font-mono text-xs"
                    value={text}
                    onChange={(ev) => setText(ev.target.value)}
                  />
                </div>
                <Button type="submit" disabled={busy !== null}>
                  {busy === "save" ? <Loader2 className="animate-spin" /> : null} Save as draft
                </Button>
              </form>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Versions</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {versions.map((v) => (
                <div key={v.id} className="flex items-center justify-between gap-4 rounded-md border p-3">
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-medium">v{v.version}</span>
                    <Badge variant={v.status === "published" ? "default" : "secondary"}>{v.status}</Badge>
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => loadVersionInto(prompt.id, v.id)}>
                      View
                    </Button>
                    {v.status !== "published" && (
                      <Button size="sm" onClick={() => onPublish(v.id)} disabled={busy !== null}>
                        {busy === "publish" ? <Loader2 className="animate-spin" /> : null} Publish
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Swap the route**

In `App.tsx`: add `import { AgentPrompt } from "@/pages/AgentPrompt"` and change the `agent-prompt` route element from `<Placeholder title="Agent Prompt" />` to `<AgentPrompt />`.

- [ ] **Step 3: Typecheck + lint + build**

Run: `cd vera-frontend && npx tsc -b && npx eslint src/pages/AgentPrompt.tsx src/lib/api/prompts.ts src/App.tsx && npx vite build`
Expected: all clean.

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/pages/AgentPrompt.tsx vera-frontend/src/App.tsx
git commit -m "feat(prompts): agent prompt editor page with version history"
```

---

### Task 6: Finalize — simplify, full checks, push

- [ ] **Step 1: Run the mandatory simplifier**

Say **"simplify code"** (code-simplifier plugin) on the change; apply any refinements.

- [ ] **Step 2: Backend gate**

Run: `cd vera-backend && uv run --active ruff check . && uv run --active pytest tests/integration/control_plane/test_prompts.py -q`
Expected: clean / PASS. (Full `just check` if DB + env available.)

- [ ] **Step 3: Frontend gate**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npx vitest run && npx vite build`
Expected: all clean / PASS.

- [ ] **Step 4: Manual smoke (optional but recommended)**

As the seeded super admin at `/platform/login` → open `/agent-prompt` → edit the IBV prompt → Save as draft → Publish → confirm history shows the new published version and the prior one demoted.

- [ ] **Step 5: Push**

```bash
git push origin feat/frontend-superadmin-access
```

---

## Self-Review

- **Spec coverage:** list prompts (Task 1), version history + view (Task 1), create draft w/ schema binding + 409 (Task 2), publish + demote + one-published (Task 3), frontend editor + history + save/publish + route swap (Tasks 4–5), platform gate + tenant-forbidden (Task 1 test), tests both ends (Tasks 1–5), simplify + checks (Task 6). All spec sections covered.
- **Placeholders:** none — every step has concrete code/commands.
- **Type consistency:** `composite_json`/`CompositeJson`, `PromptVersionDetail`, `published_version`, `createPromptDraft(promptId, compositeJson)`, `publishPromptVersion(promptId, versionId)` are consistent across backend DTOs and frontend types/functions. `_READ`/`platform_require("platform:elevations:read")` used uniformly.
