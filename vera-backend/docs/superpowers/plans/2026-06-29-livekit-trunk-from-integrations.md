# Per-tenant LiveKit outbound trunk from the integrations table — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Source the LiveKit outbound SIP trunk id from each tenant's envelope-encrypted `integration` row (decrypted at dial time in the control plane) instead of a global environment variable.

**Architecture:** The control plane already owns the tenant-scoped RLS DB session and the KMS. The Voice Lab dial endpoint resolves the tenant's `livekit_outbound_trunk_id` credential via the existing `get_integration_credentials(...)` helper, decrypts it, and passes the trunk id into `LiveKitGateway.create_sip_participant(...)`, which becomes a per-call argument. The agent worker is untouched (it has no DB access and does not dial). The global env var is deleted entirely; an unconfigured tenant fails closed.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, AES-256-GCM envelope encryption (`vera_core.config.kms`), pytest/pytest-asyncio; React + Vite + TypeScript frontend.

## Global Constraints

- **Prerequisite:** PR #25 (renumber alembic `0015_api_key_unique_name` → `0018`) must merge to `main` first; then rebase this branch onto `main` so the new migration chains off `0018` as `0019`. Verify `uv run --package control_plane alembic heads` shows a single head before adding the new migration.
- **Fail closed:** an unconfigured tenant's outbound dial raises `ConflictError("outbound SIP is not configured")` — no env fallback.
- **Delete the env var entirely:** remove `Settings.livekit_sip_trunk_id` and the `VERA_LIVEKIT_SIP_TRUNK_ID` block in `env.example`.
- **Domain names (verbatim):** integration type name `livekit_outbound_trunk_id`; credential key `trunk_id`; credentials_schema `{"trunk_id": "string"}`.
- **Code style:** PEP 695 type params; asyncio only (never `import anyio`); ruff + `mypy --strict`. Run `just check` before claiming done; run `/simplify` on the diff, then re-run `just check`.
- **KMS:** never construct a KMS outside `build_kms`/test fixtures; tests inject `LocalDevKMS(master_key=b"a" * 32)` (matches the `authz_app` fixture, so seal/open round-trips).
- **Commits:** no `Co-Authored-By` trailer (user's global rule).

---

### Task 1: Rename the seeded catalog type + drop-row migration

**Files:**
- Modify: `vera-backend/scripts/seed.py:282-284`
- Create: `vera-backend/migrations/versions/0019_drop_twilio_sip_integration_type.py`

**Interfaces:**
- Produces: an `integration_type` row named `livekit_outbound_trunk_id` with `credentials_schema = {"trunk_id": "string"}`; removes any legacy `twilio_sip` row.

- [ ] **Step 1: Update the seed catalog**

In `vera-backend/scripts/seed.py`, replace the `INTEGRATION_TYPES` list (lines 282-284):

```python
INTEGRATION_TYPES: list[dict[str, Any]] = [
    {"name": "livekit_outbound_trunk_id", "credentials_schema": {"trunk_id": "string"}},
]
```

- [ ] **Step 2: Write the drop-row migration**

Create `vera-backend/migrations/versions/0019_drop_twilio_sip_integration_type.py`:

```python
"""Drop the legacy twilio_sip integration_type (renamed to livekit_outbound_trunk_id)

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-29

scripts/seed.py renamed the outbound-trunk catalog type from `twilio_sip` to
`livekit_outbound_trunk_id`. The seeder upserts by `name`, so the old row would
linger on any already-seeded DB. Pre-launch, no tenant has configured it; delete any
dependent `integration` rows first (FK is ondelete=RESTRICT) and then the type row.
Idempotent: a no-op when the row is already absent.

Irreversible for tenant data: downgrade re-creates only the empty catalog row, not any
deleted tenant credentials.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM integration
        WHERE integration_type_id IN (
            SELECT id FROM integration_type WHERE name = 'twilio_sip'
        )
        """
    )
    op.execute("DELETE FROM integration_type WHERE name = 'twilio_sip'")


def downgrade() -> None:
    # Best-effort structural restore of the catalog row only; created_at/updated_at fill
    # from their server_default, and the id is arbitrary (Python-side default in the model).
    op.execute(
        """
        INSERT INTO integration_type (id, name, credentials_schema)
        VALUES (gen_random_uuid(), 'twilio_sip', '{"twilio_sip_trunk": "string"}'::jsonb)
        ON CONFLICT (name) DO NOTHING
        """
    )
```

- [ ] **Step 3: Verify a single clean alembic head**

Run: `cd vera-backend && uv run --package control_plane alembic heads 2>&1 | grep -v 'Building\|Built\|Installed'`
Expected: `0019 (head)` and no "present more than once" warning.

- [ ] **Step 4: Apply the migration and re-seed the dev DB**

Run:
```bash
cd vera-backend
uv run --package control_plane alembic upgrade head
uv run python scripts/seed.py   # or: just seed
docker exec vera-backend-postgres-1 psql -U vera -d vera -c "SELECT name, credentials_schema FROM integration_type;"
```
Expected: exactly one row — `livekit_outbound_trunk_id | {"trunk_id": "string"}` — and no `twilio_sip` row.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/scripts/seed.py vera-backend/migrations/versions/0019_drop_twilio_sip_integration_type.py
git commit -m "feat(integrations): rename outbound-trunk catalog type to livekit_outbound_trunk_id"
```

---

### Task 2: Control-plane dial path reads the trunk from the DB; env removed

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/livekit_gateway.py:19-32,63-82,102-110`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/common.py:21-34,51-60`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/voice_lab.py:21,63-95`
- Modify: `vera-backend/packages/vera_core/src/vera_core/config/settings.py` (delete `livekit_sip_trunk_id`)
- Modify: `vera-backend/env.example:50-52`
- Modify (test infra): `vera-backend/tests/integration/control_plane/conftest.py:44,54-55`
- Test: `vera-backend/tests/integration/control_plane/test_voice_lab.py:25-33,88-106`

**Interfaces:**
- Consumes: `get_integration_credentials(session, kms, *, integration_type_name="livekit_outbound_trunk_id")` from `vera_core.integrations.credentials` (returns `dict | None`; the dict carries `trunk_id`). `tenant_scoped_session` (RLS session) and `get_kms` from `control_plane.deps`.
- Produces: `LiveKitGateway.create_sip_participant(room_name: str, phone_number: str, trunk_id: str)` — `trunk_id` is now a required argument; the gateway no longer stores a trunk id. `Kms = Annotated[KeyManagementService, Depends(get_kms)]` alias added to `common.py`.

- [ ] **Step 1: Update the integration test to the new behavior (write the failing test)**

In `vera-backend/tests/integration/control_plane/test_voice_lab.py`, replace the imports block and the `trunk_configured` fixture (lines 8-33) with:

```python
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.control_plane.conftest import FakeLiveKit, RBACWorld
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.integrations.credentials import seal_credentials
from vera_core.models import Integration, IntegrationType
from vera_core.observability.correlation import parse_room_name, room_name_for_call

_TRUNK_TYPE = "livekit_outbound_trunk_id"
_TRUNK_VALUE = "ST_test_trunk"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def trunk_configured(
    admin_sessionmaker: async_sessionmaker[AsyncSession], rbac_world: RBACWorld
) -> AsyncIterator[None]:
    """Seal a trunk credential for the test tenant so the outbound dial resolves it
    from the DB. Uses the same LocalDevKMS master key as the app under test, so the
    app's get_integration_credentials can open what we seal here."""
    kms = LocalDevKMS(master_key=b"a" * 32)
    async with admin_sessionmaker() as session, session.begin():
        itype = IntegrationType(
            name=_TRUNK_TYPE, credentials_schema={"trunk_id": "string"}
        )
        session.add(itype)
        await session.flush()
        integration = Integration(
            tenant_id=rbac_world.tenant_id,
            integration_type_id=itype.id,
            status="active",
        )
        await seal_credentials(kms, integration=integration, credentials={"trunk_id": _TRUNK_VALUE})
        session.add(integration)
    try:
        yield
    finally:
        async with admin_sessionmaker() as session, session.begin():
            await session.execute(
                delete(Integration).where(Integration.tenant_id == rbac_world.tenant_id)
            )
            await session.execute(delete(IntegrationType).where(IntegrationType.name == _TRUNK_TYPE))
```

Then update the success assertion in `test_outbound_with_trunk_and_valid_phone_places_sip_call` (the `sip_calls` tuple now carries the resolved trunk):

```python
    assert fake_livekit.sip_calls[before] == (body["room_name"], "+15551234567", _TRUNK_VALUE)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_voice_lab.py -q`
Expected: FAIL — `test_outbound_with_trunk_and_valid_phone_places_sip_call` errors (FakeLiveKit.create_sip_participant takes 2 args / `sip_calls` tuple has 2 elements), and `voice_lab` still reads the soon-to-be-removed setting.

- [ ] **Step 3: Make `create_sip_participant` take a per-call trunk id**

In `vera-backend/apps/control_plane/src/control_plane/livekit_gateway.py`:

Replace the constructor (lines 20-32) to drop the trunk field:

```python
    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
```

Replace `create_sip_participant` (lines 63-82):

```python
    async def create_sip_participant(
        self, room_name: str, phone_number: str, trunk_id: str
    ) -> None:
        """Dial an outbound phone number into the room via the tenant's SIP trunk.

        The callee's audio joins the room as the SIP-callee participant; the agent and
        any listening monitor hear them once they answer. `trunk_id` is resolved per
        tenant from the integrations table by the caller (fail-closed before this).
        """
        if not trunk_id:
            raise ValueError("outbound SIP trunk is not configured")
        async with self._client() as lk:
            await lk.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    room_name=room_name,
                    participant_identity=SIP_CALLEE_IDENTITY,
                    participant_name="Outbound callee",
                    wait_until_answered=False,
                )
            )
```

Replace `build_livekit_gateway` (lines 102-110) to stop passing the setting:

```python
def build_livekit_gateway(settings: Settings, secrets: SecretProvider) -> LiveKitGateway:
    if settings.livekit_url is None:
        raise ValueError("VERA_LIVEKIT_URL must be set to use the LiveKit gateway")
    return LiveKitGateway(
        url=settings.livekit_url,
        api_key=secrets.get("LIVEKIT_API_KEY"),
        api_secret=secrets.get("LIVEKIT_API_SECRET"),
    )
```

- [ ] **Step 4: Update FakeLiveKit to record the trunk id**

In `vera-backend/tests/integration/control_plane/conftest.py`, change the `sip_calls` type (line 44) and the fake method (lines 54-55):

```python
        self.sip_calls: list[tuple[str, str, str]] = []
```
```python
    async def create_sip_participant(
        self, room_name: str, phone_number: str, trunk_id: str
    ) -> None:
        self.sip_calls.append((room_name, phone_number, trunk_id))
```

- [ ] **Step 5: Add the `Kms` DI alias**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/common.py`, add `get_kms` to the `control_plane.deps` import (lines 21-30) and `KeyManagementService` import, then add the alias near the other aliases (after line 60):

```python
from vera_core.config.kms import KeyManagementService
```
```python
Kms = Annotated[KeyManagementService, Depends(get_kms)]
```
(Add `get_kms` to the existing `from control_plane.deps import (...)` block.)

- [ ] **Step 6: Resolve the trunk from the DB in the dial endpoint**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/voice_lab.py`:

Add imports (near line 21 / 44):
```python
from control_plane.api.v1.common import Kms, LiveKit, TenantId, TenantSession
from vera_core.integrations.credentials import get_integration_credentials
```
(Drop `AppSettings` from the `common` import — it is no longer used here.)

Change the `start_voice_session` signature (lines 63-69) to take a tenant session + KMS and drop `settings`:

```python
async def start_voice_session(
    body: StartVoiceSessionRequest,
    tenant_id: TenantId,
    livekit: LiveKit,
    session: TenantSession,
    kms: Kms,
    caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
) -> ResponseModel[VoiceSessionResponse]:
```

Replace the outbound precondition block (lines 77-95) so the trunk comes from the DB and the dial passes it through:

```python
    is_outbound = body.mode == "outbound"
    trunk_id: str | None = None
    if is_outbound:
        creds = await get_integration_credentials(
            session, kms, integration_type_name="livekit_outbound_trunk_id"
        )
        trunk_id = creds.get("trunk_id") if creds else None
        if not trunk_id:
            raise ConflictError(message="outbound SIP is not configured")
        if body.phone_number is None or not _E164.match(body.phone_number):
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="phone_number must be E.164 for an outbound call",
            )

    prefix = MONITOR_IDENTITY_PREFIX if is_outbound else CALLER_IDENTITY_PREFIX
    browser_identity = f"{prefix}{caller.user_id}"

    await livekit.create_call_room(
        room_name, metadata={"wait_for_speaker": True, "publish_transcript": True}
    )
    if is_outbound:
        assert body.phone_number is not None  # validated non-None above when is_outbound
        assert trunk_id is not None  # set above when is_outbound
        await livekit.create_sip_participant(room_name, body.phone_number, trunk_id)
```

- [ ] **Step 7: Delete the env setting**

In `vera-backend/packages/vera_core/src/vera_core/config/settings.py`, delete the `livekit_sip_trunk_id` field and its two-line comment (the block around lines 108-110).

In `vera-backend/env.example`, delete the `VERA_LIVEKIT_SIP_TRUNK_ID` block (lines 50-52: the two comment lines and the commented example).

- [ ] **Step 8: Run the Voice Lab tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_voice_lab.py -q`
Expected: PASS — including `test_outbound_without_trunk_configured_returns_409` (no credential seeded → fail closed) and `test_outbound_with_trunk_and_valid_phone_places_sip_call` (asserts the resolved `ST_test_trunk` flows into the SIP call).

- [ ] **Step 9: Typecheck + lint (no stale references to the deleted setting)**

Run: `cd vera-backend && uv run mypy --strict . && uv run ruff check .`
Expected: clean. (Catches any remaining `settings.livekit_sip_trunk_id` reference.)

- [ ] **Step 10: Commit**

```bash
git add vera-backend/apps/control_plane vera-backend/packages/vera_core/src/vera_core/config/settings.py vera-backend/env.example vera-backend/tests/integration/control_plane/conftest.py vera-backend/tests/integration/control_plane/test_voice_lab.py
git commit -m "feat(integrations): resolve outbound trunk from the tenant integration, drop env var"
```

---

### Task 3: Rename `twilio_sip` references in the credentials unit test

**Files:**
- Modify: `vera-backend/tests/unit/integrations/test_credentials.py`

**Interfaces:**
- Consumes: nothing new — `seal_credentials` / `open_credentials` are name-agnostic; this is a cosmetic rename of the sample credential dict for consistency with the new domain language.

- [ ] **Step 1: Update the sample credential names**

In `vera-backend/tests/unit/integrations/test_credentials.py`, replace the sample credential dicts that use `{"twilio_sip_trunk": ...}` with `{"trunk_id": ...}` (the helper under test does not care about the key; keep whatever values/asserts exist, only swap the key name). Search the file for `twilio_sip` and update each occurrence.

- [ ] **Step 2: Run the unit test**

Run: `cd vera-backend && uv run pytest tests/unit/integrations/test_credentials.py -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add vera-backend/tests/unit/integrations/test_credentials.py
git commit -m "test(integrations): align credential-helper test names with trunk_id"
```

---

### Task 4: Frontend domain-language rename

**Files:**
- Modify: `vera-frontend/src/components/settings/IntegrationsSection.tsx:10-15,77-78,86,106-107`

**Interfaces:**
- Consumes: the backend `PUT /integrations/{integration_type}` contract (unchanged) — only the slug + credential key strings the component sends change.

- [ ] **Step 1: Update the slug, key, and the seed-referencing comment**

In `vera-frontend/src/components/settings/IntegrationsSection.tsx`, replace lines 10-15:

```tsx
// The single integration this tenant configures. The integration-type catalog is
// not exposed over the API, so the frontend names the slug + its one credential
// key directly. Both are seeded server-side (scripts/seed.py:
// livekit_outbound_trunk_id → {trunk_id}).
const INTEGRATION_TYPE = "livekit_outbound_trunk_id"
const CREDENTIAL_KEY = "trunk_id"
const DISPLAY_NAME = "LiveKit outbound trunk"
```

- [ ] **Step 2: Show the human label instead of the raw slug**

In the same file, replace the raw slug render (line 86) with the friendly name:

```tsx
          <span className="text-sm font-medium">{DISPLAY_NAME}</span>
```

And make the form label read naturally (lines 106-107):

```tsx
            <label className="text-xs text-muted-foreground">
              {configured ? "Replace trunk id" : "Set trunk id"}
            </label>
```

- [ ] **Step 3: Typecheck + lint the frontend**

Run: `cd vera-frontend && npm run lint && npx tsc --noEmit`
Expected: clean (confirm the exact scripts in `vera-frontend/package.json`; use the project's typecheck/lint commands).

- [ ] **Step 4: Manual smoke (after the backend tasks + re-seed)**

Settings → Integrations shows "LiveKit outbound trunk" / "Not configured". Paste a trunk id, Save → "Configured" with a "Last updated" timestamp. Start an outbound Voice Lab session → the SIP call is placed with the configured trunk; an unconfigured tenant gets a 409.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/components/settings/IntegrationsSection.tsx
git commit -m "feat(integrations): rename settings panel to livekit_outbound_trunk_id / trunk_id"
```

---

## Final verification (whole feature)

- [ ] `cd vera-backend && just check` — ruff + mypy --strict + pytest all green.
- [ ] Run `/simplify` on the diff, then re-run `just check`.
- [ ] `grep -rn "livekit_sip_trunk_id\|twilio_sip\|VERA_LIVEKIT_SIP_TRUNK" vera-backend vera-frontend` returns nothing (the env var and old slug are fully gone).
- [ ] Manual end-to-end per Task 4 Step 4.

## Self-review notes (coverage map)

- Spec Component 1 (rename) → Task 1 (seed + migration) + Task 4 (frontend). The spec's "no migration" stance was overridden by the user: a drop-row migration (`0019`) now removes the legacy `twilio_sip` row.
- Spec Component 2 (control-plane dial + env removal) → Task 2.
- Spec Component 3 (tests) → Task 2 (test_voice_lab fixture/asserts, FakeLiveKit) + Task 3 (test_credentials).
- Prerequisite: the duplicate-`0015` graph fix is a separate PR (#25) — must merge before `0019` is added; this branch rebases onto it.
