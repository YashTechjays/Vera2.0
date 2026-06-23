# Persona Tweak — Runtime Knob Wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the inert `Tenant.persona_tweak` JSONB knob end-to-end so a tenant admin can set a persona overlay and the agent worker applies it per call.

**Architecture:** A shared `PersonaTweak` Pydantic model in `vera_core.schemas` is the single contract. The control plane exposes audited, RBAC-gated `GET`/`PUT` endpoints to read/write it, loads it at `start_call`, and ships it to the worker as opaque LiveKit dispatch metadata. The worker parses the metadata (fail-safe to empty) and builds the agent's instructions + greeting from it. The worker never touches Postgres.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, SQLAlchemy async, LiveKit agents/api, pytest / pytest-asyncio, ruff, mypy --strict.

## Global Constraints

- PEP 695 type params only (`class Foo[T]`, `def f[T]`) — ruff rejects `Generic[T]`/`TypeVar`.
- `asyncio` is the only async runtime — never `import anyio`; use `asyncio.TaskGroup`/`asyncio.timeout`.
- Control-plane endpoints: return `ResponseModel[T]` via `ok(...)`; raise `CustomAPIException`/subclasses, never `HTTPException`; declare `responses=CustomAPIResponse.custom(...)`.
- Never log/trace plaintext PHI. `persona_tweak` is admin-authored **non-PHI** config; safe in metadata, prompts, and audit field-name lists.
- Audit records carry field **names**, never values; timestamps come from the DB clock.
- Verification gate: `just check` (ruff lint + mypy --strict + pytest) must pass. After implementation run the `/simplify` skill, then re-run `just check`.
- Integration tests need `just up && just migrate` (live Postgres); they skip otherwise.

---

## File Structure

- Create `packages/vera_core/src/vera_core/schemas/persona.py` — `PersonaTweak` model (the shared contract).
- Modify `packages/vera_core/src/vera_core/schemas/__init__.py` — export `PersonaTweak`.
- Modify `packages/vera_core/src/vera_core/models/rbac_defaults.py` — add `tenant:config:manage` permission.
- Modify `packages/vera_core/src/vera_core/models/enums.py` — add `AuthEvent.PERSONA_TWEAK_UPDATED`.
- Modify `apps/agent_worker/src/agent_worker/prompt.py` — `build_instructions(tweak)`, `resolve_greeting(tweak)`, `parse_persona_tweak(metadata)`.
- Modify `apps/agent_worker/src/agent_worker/agent.py` — `VeraAgent` takes per-call `instructions`/`greeting`.
- Modify `apps/agent_worker/src/agent_worker/main.py` — parse `ctx.job.metadata`, build per-call persona.
- Modify `apps/control_plane/src/control_plane/livekit_gateway.py` — `create_call_room(room_name, metadata)`.
- Modify `apps/control_plane/src/control_plane/api/v1/calls.py` — load tenant, pass tweak as dispatch metadata.
- Create `apps/control_plane/src/control_plane/api/v1/tenant_config.py` — `GET`/`PUT` persona endpoints.
- Modify `apps/control_plane/src/control_plane/api/v1/__init__.py` — register the router.
- Tests: `packages/vera_core/tests/unit/test_persona_schema.py`, update `apps/agent_worker/tests/unit/test_prompt.py` + `test_agent.py`, `tests/integration/control_plane/test_tenant_config.py`.

---

## Task 1: `PersonaTweak` shared schema

**Files:**
- Create: `packages/vera_core/src/vera_core/schemas/persona.py`
- Modify: `packages/vera_core/src/vera_core/schemas/__init__.py`
- Test: `packages/vera_core/tests/unit/test_persona_schema.py`

**Interfaces:**
- Produces: `PersonaTweak(BaseModel)` with optional `extra_instructions: str | None` (max_length 4000) and `greeting: str | None` (max_length 500); `model_config = ConfigDict(extra="forbid")`. Importable as `from vera_core.schemas import PersonaTweak`.

- [ ] **Step 1: Write the failing test**

```python
# packages/vera_core/tests/unit/test_persona_schema.py
import pytest
from pydantic import ValidationError

from vera_core.schemas import PersonaTweak


def test_empty_tweak_is_all_none() -> None:
    t = PersonaTweak()
    assert t.extra_instructions is None
    assert t.greeting is None


def test_round_trip_excludes_none() -> None:
    t = PersonaTweak(extra_instructions="Always confirm the member ID twice.")
    assert t.model_dump(exclude_none=True) == {
        "extra_instructions": "Always confirm the member ID twice."
    }


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        PersonaTweak.model_validate({"tone": "formal"})


def test_length_caps_enforced() -> None:
    with pytest.raises(ValidationError):
        PersonaTweak(extra_instructions="x" * 4001)
    with pytest.raises(ValidationError):
        PersonaTweak(greeting="y" * 501)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_persona_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'PersonaTweak'`.

- [ ] **Step 3: Create the schema**

```python
# packages/vera_core/src/vera_core/schemas/persona.py
"""Tenant persona overlay — the `Tenant.persona_tweak` runtime knob contract.

Admin-authored, non-PHI configuration shared by the control plane (validate on
write, serialize into dispatch metadata) and the agent worker (parse at call
start). The empty model is the documented no-op default for the JSONB column.
"""

from pydantic import BaseModel, ConfigDict, Field


class PersonaTweak(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Appended to the base SYSTEM_PROMPT. Length-capped to bound prompt growth.
    extra_instructions: str | None = Field(default=None, max_length=4000)
    # Overrides the base outbound GREETING when set.
    greeting: str | None = Field(default=None, max_length=500)
```

- [ ] **Step 4: Export it**

```python
# packages/vera_core/src/vera_core/schemas/__init__.py
from .dto import CallSummary, JoinTokenResponse, StartCallRequest
from .form_template import FieldType, FormField, FormTemplate
from .persona import PersonaTweak

__all__ = [
    "CallSummary",
    "FieldType",
    "FormField",
    "FormTemplate",
    "JoinTokenResponse",
    "PersonaTweak",
    "StartCallRequest",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_persona_schema.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/schemas/persona.py packages/vera_core/src/vera_core/schemas/__init__.py packages/vera_core/tests/unit/test_persona_schema.py
git commit -m "feat(persona): add shared PersonaTweak schema"
```

---

## Task 2: Worker prompt overlay + metadata parse

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/prompt.py:88-93`
- Test: `apps/agent_worker/tests/unit/test_prompt.py`

**Interfaces:**
- Consumes: `vera_core.schemas.PersonaTweak` (Task 1).
- Produces:
  - `build_instructions(tweak: PersonaTweak | None = None) -> str` — base `SYSTEM_PROMPT` (+ `extra_instructions` if set) + `CARTESIA_MARKUP_GUIDE`.
  - `resolve_greeting(tweak: PersonaTweak | None = None) -> str` — `tweak.greeting` or base `GREETING`.
  - `parse_persona_tweak(metadata: str | None) -> PersonaTweak` — fail-safe: `None`/empty/invalid JSON/invalid shape → `PersonaTweak()`.

- [ ] **Step 1: Write the failing tests**

Replace the body of `apps/agent_worker/tests/unit/test_prompt.py` with:

```python
from agent_worker.prompt import (
    CARTESIA_MARKUP_GUIDE,
    GREETING,
    SYSTEM_PROMPT,
    build_instructions,
    parse_persona_tweak,
    resolve_greeting,
)
from vera_core.schemas import PersonaTweak


def test_prompt_is_chat_only_and_includes_cartesia_guide() -> None:
    assert "record_service_coverage" not in SYSTEM_PROMPT
    assert "end_call" not in SYSTEM_PROMPT
    assert "infertility" in SYSTEM_PROMPT.lower()
    assert "diagnostic testing" in SYSTEM_PROMPT.lower()
    assert GREETING.startswith("Hi, I'm calling on behalf of a patient")
    combined = build_instructions()
    assert combined.startswith(SYSTEM_PROMPT)
    assert CARTESIA_MARKUP_GUIDE in combined
    assert "<spell>" in CARTESIA_MARKUP_GUIDE


def test_empty_tweak_is_no_op() -> None:
    assert build_instructions(PersonaTweak()) == build_instructions(None)
    assert resolve_greeting(PersonaTweak()) == GREETING


def test_extra_instructions_appended_before_cartesia_guide() -> None:
    out = build_instructions(PersonaTweak(extra_instructions="Confirm member ID twice."))
    assert out.startswith(SYSTEM_PROMPT)
    assert "Confirm member ID twice." in out
    assert out.index("Confirm member ID twice.") < out.index(CARTESIA_MARKUP_GUIDE)


def test_greeting_override() -> None:
    assert resolve_greeting(PersonaTweak(greeting="Hello there.")) == "Hello there."


def test_parse_persona_tweak_fail_safe() -> None:
    assert parse_persona_tweak(None) == PersonaTweak()
    assert parse_persona_tweak("") == PersonaTweak()
    assert parse_persona_tweak("not json") == PersonaTweak()
    assert parse_persona_tweak('{"tone": "formal"}') == PersonaTweak()  # unknown key
    assert parse_persona_tweak('{"greeting": "Hi"}') == PersonaTweak(greeting="Hi")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_prompt.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_persona_tweak'`.

- [ ] **Step 3: Implement the overlay functions**

Add `import json` and `from vera_core.schemas import PersonaTweak` to the top of `apps/agent_worker/src/agent_worker/prompt.py` (after the existing `from __future__ import annotations`). Leave the `_INSTRUCTIONS` constant (line 88) in place — `agent.py` still imports it until Task 3. Replace only `build_instructions` (lines 91-93) and add the two new functions:

```python
def build_instructions(tweak: PersonaTweak | None = None) -> str:
    """Chat-only instructions: base persona (+ optional tenant extra instructions)
    followed by the Cartesia readback guide (we use sonic-3.5)."""
    parts = [SYSTEM_PROMPT]
    if tweak is not None and tweak.extra_instructions:
        parts.append(tweak.extra_instructions)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)


def resolve_greeting(tweak: PersonaTweak | None = None) -> str:
    """The outbound opener: the tenant override when set, else the base greeting."""
    if tweak is not None and tweak.greeting:
        return tweak.greeting
    return GREETING


def parse_persona_tweak(metadata: str | None) -> PersonaTweak:
    """Parse LiveKit dispatch metadata into a PersonaTweak. Fail-safe: any missing,
    empty, or malformed metadata yields the no-op tweak so a bad config never kills
    a live call (mirrors the cascade's fail-safe posture, not the strict PHI seams)."""
    if not metadata:
        return PersonaTweak()
    try:
        return PersonaTweak.model_validate(json.loads(metadata))
    except (json.JSONDecodeError, ValueError):
        return PersonaTweak()
```

Note: `_INSTRUCTIONS` stays for now (still imported by `agent.py`); Task 3 removes it together with the import change so no commit leaves a broken importer.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_prompt.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/prompt.py apps/agent_worker/tests/unit/test_prompt.py
git commit -m "feat(persona): apply tweak in worker prompt builder + fail-safe parse"
```

---

## Task 3: VeraAgent per-call persona

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/agent.py:13,18-25`
- Modify: `apps/agent_worker/src/agent_worker/prompt.py` (delete unused `_INSTRUCTIONS`)
- Test: `apps/agent_worker/tests/unit/test_agent.py`

**Interfaces:**
- Consumes: `build_instructions`, `resolve_greeting` (Task 2).
- Produces: `VeraAgent(boundary, session_id, *, instructions: str | None = None, greeting: str | None = None)`. Defaults fall back to base persona so existing call sites keep working. `on_enter` says the resolved greeting.

- [ ] **Step 1: Write the failing test**

Replace `apps/agent_worker/tests/unit/test_agent.py` with:

```python
"""Tests for VeraAgent — chat-only persona agent with PHI-wall node overrides."""

from agent_worker.agent import VeraAgent
from agent_worker.prompt import build_instructions
from vera_core.phi import PassthroughPHIBoundary
from vera_core.schemas import PersonaTweak


def test_vera_agent_is_chat_only_with_persona() -> None:
    agent = VeraAgent(boundary=PassthroughPHIBoundary(), session_id="s1")
    assert list(agent.tools) == []
    assert "infertility" in agent.instructions.lower()


def test_vera_agent_accepts_overlaid_instructions() -> None:
    instructions = build_instructions(PersonaTweak(extra_instructions="Confirm member ID twice."))
    agent = VeraAgent(
        boundary=PassthroughPHIBoundary(),
        session_id="s1",
        instructions=instructions,
        greeting="Hello there.",
    )
    assert "Confirm member ID twice." in agent.instructions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_agent.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'instructions'`.

- [ ] **Step 3: Update VeraAgent**

Change the import line `apps/agent_worker/src/agent_worker/agent.py:13` from:

```python
from agent_worker.prompt import _INSTRUCTIONS, GREETING
```

to:

```python
from agent_worker.prompt import build_instructions, resolve_greeting
```

Then delete the now-unused `_INSTRUCTIONS` constant from `apps/agent_worker/src/agent_worker/prompt.py` (the `_INSTRUCTIONS = f"{SYSTEM_PROMPT}\n\n{CARTESIA_MARKUP_GUIDE}"` line). `build_instructions()` is now the only assembler. Grep to confirm no other importer: `grep -rn "_INSTRUCTIONS" apps/` should return nothing after this.

Replace the `__init__`/`on_enter` block (lines 18-25) with:

```python
class VeraAgent(Agent):
    def __init__(
        self,
        boundary: PHIBoundaryProtocol,
        session_id: str,
        *,
        instructions: str | None = None,
        greeting: str | None = None,
    ) -> None:
        self._boundary = boundary
        self._session_id = session_id
        self._greeting = greeting if greeting is not None else resolve_greeting()
        super().__init__(
            instructions=instructions if instructions is not None else build_instructions(),
            tools=[],
        )

    async def on_enter(self) -> None:
        self.session.say(self._greeting)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_agent.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/agent.py apps/agent_worker/src/agent_worker/prompt.py apps/agent_worker/tests/unit/test_agent.py
git commit -m "feat(persona): VeraAgent accepts per-call instructions and greeting"
```

---

## Task 4: Worker entrypoint reads dispatch metadata

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/main.py:53-76`

**Interfaces:**
- Consumes: `parse_persona_tweak`, `build_instructions`, `resolve_greeting` (Tasks 2-3); `ctx.job.metadata` (LiveKit `Job.metadata`, verified present in the installed SDK).
- Produces: no new public surface — wires the tweak into `VeraAgent` at session start.

- [ ] **Step 1: Add imports**

In `apps/agent_worker/src/agent_worker/main.py`, change the agent import (line 13) and add the prompt import:

```python
from agent_worker.agent import VeraAgent
from agent_worker.cascade import _build_vad, build_session
from agent_worker.prompt import build_instructions, parse_persona_tweak, resolve_greeting
```

- [ ] **Step 2: Build the per-call persona in `entrypoint`**

In `entrypoint`, after `boundary = build_phi_boundary(settings)` and before `session = build_session(...)`, add:

```python
    # Tenant persona overlay arrives as opaque dispatch metadata (set by the control
    # plane). Fail-safe: bad/missing metadata falls back to the base persona.
    tweak = parse_persona_tweak(ctx.job.metadata if ctx.job is not None else None)
    instructions = build_instructions(tweak)
    greeting = resolve_greeting(tweak)
```

Then change the final `session.start(...)` call (line 76) to:

```python
    await session.start(
        agent=VeraAgent(
            boundary=boundary,
            session_id=session_id,
            instructions=instructions,
            greeting=greeting,
        ),
        room=ctx.room,
    )
```

- [ ] **Step 3: Verify worker unit tests still pass + typecheck**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit -v && uv run mypy --strict apps/agent_worker/src/agent_worker/main.py`
Expected: PASS; no mypy errors. (The entrypoint itself is exercised end-to-end by the cascade; the parse/build helpers are unit-tested in Tasks 2-3.)

- [ ] **Step 4: Commit**

```bash
git add apps/agent_worker/src/agent_worker/main.py
git commit -m "feat(persona): worker entrypoint applies dispatch-metadata tweak"
```

---

## Task 5: LiveKit gateway forwards dispatch metadata

**Files:**
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py:23-33`

**Interfaces:**
- Produces: `create_call_room(room_name: str, metadata: str = "") -> None` — passes `metadata` into `CreateAgentDispatchRequest(metadata=...)` (field verified present in the installed `livekit.api`).

- [ ] **Step 1: Update the method signature and dispatch call**

Replace `create_call_room` (lines 23-33) with:

```python
    async def create_call_room(self, room_name: str, metadata: str = "") -> None:
        # LiveKitAPI wraps an aiohttp ClientSession, which requires a running
        # event loop — construct it inside the coroutine, not in __init__.
        lk = api.LiveKitAPI(self.url, self._api_key, self._api_secret)
        try:
            await lk.room.create_room(api.CreateRoomRequest(name=room_name))
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME, room=room_name, metadata=metadata
                )
            )
        finally:
            await lk.aclose()  # type: ignore[no-untyped-call]  # livekit-api missing return annotation
```

- [ ] **Step 2: Typecheck**

Run: `cd vera-backend && uv run mypy --strict apps/control_plane/src/control_plane/livekit_gateway.py`
Expected: no errors. (Behavior is covered via Task 6's integration test, which asserts the metadata reaches dispatch through a fake gateway.)

- [ ] **Step 3: Commit**

```bash
git add apps/control_plane/src/control_plane/livekit_gateway.py
git commit -m "feat(persona): gateway forwards dispatch metadata"
```

---

## Task 6: `start_call` ships the tenant's persona_tweak

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py:9-22,57-85`
- Test: `tests/integration/control_plane/test_tenant_config.py` (the dispatch-metadata assertion; file created in Task 7)

**Interfaces:**
- Consumes: `PersonaTweak` (Task 1); `create_call_room(room_name, metadata)` (Task 5); `Tenant` model (`vera_core.models`).
- Produces: `start_call` serializes the tenant's `persona_tweak` into dispatch metadata.

- [ ] **Step 1: Add imports**

In `apps/control_plane/src/control_plane/api/v1/calls.py`, add to the model/schema imports:

```python
from vera_core.models import Call, CallEvent, PatientForm, Tenant
from vera_core.schemas import CallSummary, JoinTokenResponse, PersonaTweak, StartCallRequest
```

- [ ] **Step 2: Load the tenant and pass metadata in `start_call`**

In `start_call`, replace the single line `await livekit.create_call_room(room_name)` (line 75) with:

```python
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()  # RLS on `tenant` keys on id → only the caller's own row
    # persona_tweak is admin-authored, non-PHI config; safe to serialize into metadata.
    tweak = PersonaTweak.model_validate(tenant.persona_tweak) if tenant else PersonaTweak()
    metadata = tweak.model_dump_json(exclude_none=True)
    await livekit.create_call_room(room_name, metadata=metadata)
```

- [ ] **Step 3: Typecheck**

Run: `cd vera-backend && uv run mypy --strict apps/control_plane/src/control_plane/api/v1/calls.py`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/calls.py
git commit -m "feat(persona): start_call ships tenant persona_tweak as dispatch metadata"
```

---

## Task 7: Tenant-config endpoints (GET/PUT) + RBAC + audit

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/rbac_defaults.py:15-27`
- Modify: `packages/vera_core/src/vera_core/models/enums.py:154-155`
- Create: `apps/control_plane/src/control_plane/api/v1/tenant_config.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/__init__.py`
- Test: `tests/integration/control_plane/test_tenant_config.py`

**Interfaces:**
- Consumes: `PersonaTweak` (Task 1); `TenantId`, `TenantSession`, `AuthAudit`, `emit_auth_event` (`api/v1/common.py`); `require` (`auth.rbac`); `Tenant` model; `AuthEvent.PERSONA_TWEAK_UPDATED`.
- Produces: `GET /api/v1/tenant/config/persona` and `PUT /api/v1/tenant/config/persona`, gated by `tenant:config:manage`.

- [ ] **Step 1: Add the permission**

In `packages/vera_core/src/vera_core/models/rbac_defaults.py`, add to `DEFAULT_PERMISSIONS` (after the `tenant:auth:configure` line):

```python
    "tenant:config:manage": "View and edit tenant runtime config (persona, knobs)",
```

(`TENANT_ADMIN` holds all of `DEFAULT_PERMISSIONS`, so this is granted automatically.)

- [ ] **Step 2: Add the audit event**

In `packages/vera_core/src/vera_core/models/enums.py`, add to `AuthEvent` after `PROVIDER_DISABLED` (line 155):

```python
    PERSONA_TWEAK_UPDATED = "persona_tweak_updated"
```

- [ ] **Step 3: Write the failing integration test**

```python
# tests/integration/control_plane/test_tenant_config.py
"""Integration tests for the tenant runtime-config surface (persona_tweak) over a
live RLS-enforcing connection. The `admin` persona holds TENANT_ADMIN (which
includes `tenant:config:manage`); `norole` holds nothing."""

import pytest

pytestmark = pytest.mark.integration

PERSONA_PATH = "/api/v1/tenant/config/persona"


async def test_get_persona_defaults_to_empty(client, admin_headers) -> None:
    resp = await client.get(PERSONA_PATH, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == {"extra_instructions": None, "greeting": None}


async def test_put_then_get_round_trip(client, admin_headers) -> None:
    body = {"extra_instructions": "Confirm member ID twice.", "greeting": "Hello there."}
    put = await client.put(PERSONA_PATH, json=body, headers=admin_headers)
    assert put.status_code == 200
    assert put.json()["data"] == body
    got = await client.get(PERSONA_PATH, headers=admin_headers)
    assert got.json()["data"] == body


async def test_put_rejects_unknown_key(client, admin_headers) -> None:
    resp = await client.put(PERSONA_PATH, json={"tone": "formal"}, headers=admin_headers)
    assert resp.status_code == 422


async def test_requires_permission(client, norole_headers) -> None:
    assert (await client.get(PERSONA_PATH, headers=norole_headers)).status_code == 403
    assert (
        await client.put(PERSONA_PATH, json={}, headers=norole_headers)
    ).status_code == 403
```

Note: reuse the `client`, `admin_headers`, `norole_headers` fixtures from `tests/integration/control_plane/conftest.py` (same ones `test_admin.py` uses). Inspect `test_admin.py` for the exact fixture names and adapt if they differ.

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd vera-backend && just up && just migrate && uv run pytest tests/integration/control_plane/test_tenant_config.py -v`
Expected: FAIL — 404 (route not registered yet).

- [ ] **Step 5: Create the router**

```python
# apps/control_plane/src/control_plane/api/v1/tenant_config.py
"""Tenant runtime-config surface (spec Fig 7 knobs). A TENANT_ADMIN reads/edits the
tenant's persona overlay. Gated by `tenant:config:manage` and audited. persona_tweak
is admin-authored, non-PHI config — no `phi:read` gate, no PHI-access audit — but the
mutation is recorded in the auth audit log (field names only)."""

from fastapi import APIRouter, Request
from sqlalchemy import select

from control_plane.api.v1.common import AuthAudit, TenantId, TenantSession, emit_auth_event
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import client_ip
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode, NotFoundError
from control_plane.responses import ResponseModel, ok
from vera_core.models import Tenant
from vera_core.models.enums import AuthEvent
from vera_core.schemas import PersonaTweak

router = APIRouter(tags=["tenant-config"])


async def _load_tenant(session: TenantSession, tenant_id: TenantId) -> Tenant:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()  # RLS on `tenant` keys on id → only the caller's own row
    if tenant is None:  # pragma: no cover — an authenticated tenant always has its row
        raise NotFoundError(message="tenant not found")
    return tenant


@router.get(
    "/tenant/config/persona",
    response_model=ResponseModel[PersonaTweak],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_persona(
    tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("tenant:config:manage"),
) -> ResponseModel[PersonaTweak]:
    tenant = await _load_tenant(session, tenant_id)
    return ok(PersonaTweak.model_validate(tenant.persona_tweak))


@router.put(
    "/tenant/config/persona",
    response_model=ResponseModel[PersonaTweak],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def put_persona(
    body: PersonaTweak,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    caller: VerifiedIdentity = require("tenant:config:manage"),
) -> ResponseModel[PersonaTweak]:
    tenant = await _load_tenant(session, tenant_id)
    stored = body.model_dump(exclude_none=True)
    tenant.persona_tweak = stored
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.PERSONA_TWEAK_UPDATED,
        ip=client_ip(request),
        user_id=caller.user_id,
        meta={"fields": sorted(stored.keys())},  # field names only, never values
    )
    return ok(body)
```

- [ ] **Step 6: Register the router**

In `apps/control_plane/src/control_plane/api/v1/__init__.py`, add the import and include:

```python
from control_plane.api.v1.tenant_config import router as tenant_config_router
```

```python
router.include_router(tenant_config_router)
```

- [ ] **Step 7: Run the integration test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_tenant_config.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/models/rbac_defaults.py packages/vera_core/src/vera_core/models/enums.py apps/control_plane/src/control_plane/api/v1/tenant_config.py apps/control_plane/src/control_plane/api/v1/__init__.py tests/integration/control_plane/test_tenant_config.py
git commit -m "feat(persona): tenant-config persona GET/PUT endpoints with RBAC + audit"
```

---

## Task 8: Full verification + simplify pass

**Files:** none (verification only).

- [ ] **Step 1: Run the full CI gate**

Run: `cd vera-backend && just check`
Expected: ruff lint clean, `mypy --strict` clean, all pytest passing.

- [ ] **Step 2: Run the simplify skill**

Invoke the `/simplify` skill over the changed files (reuse / altitude / efficiency cleanup — quality only). Apply any cleanups.

- [ ] **Step 3: Re-run the gate**

Run: `cd vera-backend && just check`
Expected: still green.

- [ ] **Step 4: Commit any simplify changes**

```bash
git add -A
git commit -m "refactor(persona): simplify pass"
```

---

## Self-Review

**Spec coverage:**
- Shared `PersonaTweak` schema → Task 1. ✓
- `extra="forbid"`, length caps, empty=no-op → Task 1 (tests). ✓
- Worker prompt overlay + greeting override → Task 2. ✓
- Fail-safe metadata parse → Task 2 (`parse_persona_tweak`). ✓
- Per-call agent persona → Tasks 3-4. ✓
- Dispatch-metadata transport → Tasks 5-6. ✓
- GET/PUT endpoints, RLS, audit, `Cache-Control: no-store` → Task 7 (`ok()` sets no-store via the responses layer; verify in `responses.py` during impl). ✓
- New `tenant:config:manage` permission, granted to TENANT_ADMIN → Task 7. ✓
- Unit + integration tests → Tasks 1-3, 7. ✓
- `max_agents_per_va` / `retry_fill_threshold` → out of scope (spec). ✓

**Open verification points for the implementer:**
- Confirm `ok(...)` / the response layer sets `Cache-Control: no-store` (control-plane CLAUDE.md requires it); if not, set it on the two routes.
- Confirm the integration fixture names (`client`, `admin_headers`, `norole_headers`) against `tests/integration/control_plane/conftest.py` / `test_admin.py` and adapt.

**Placeholder scan:** none — every code step contains complete code.

**Type consistency:** `PersonaTweak`, `build_instructions(tweak)`, `resolve_greeting(tweak)`, `parse_persona_tweak(metadata)`, `create_call_room(room_name, metadata)`, `AuthEvent.PERSONA_TWEAK_UPDATED`, `tenant:config:manage` used consistently across tasks.
