# Per-tenant LiveKit outbound trunk from the integrations table

**Date:** 2026-06-29
**Status:** Design — approved direction, pending spec review
**Repos touched:** `vera-backend`, `vera-frontend`

## Problem

The LiveKit Elastic SIP trunk id used to place outbound calls is read from a single
deployment-wide environment variable (`VERA_LIVEKIT_SIP_TRUNK_ID` →
`Settings.livekit_sip_trunk_id`). It is global, not per-tenant. We already have a
tenant-scoped, envelope-encrypted integrations system; the trunk id should come from
there instead, so each tenant supplies its own trunk via the Settings UI and the
control plane decrypts it at dial time.

## Key facts (verified against the code)

- The integrations system is three tables: a global `integration_type` catalog
  (`name` + `credentials_schema`) and a per-tenant, RLS-scoped, envelope-encrypted
  `integration` row (`credential_ct` / `dek_ct` / `secret_ref`). One type is seeded
  today: `twilio_sip` with schema `{"twilio_sip_trunk": "string"}`
  (`scripts/seed.py`).
- Decryption already exists:
  `vera_core.integrations.credentials.get_integration_credentials(session, kms, *,
  integration_type_name)` joins the tenant's `integration` row to the type by name,
  opens the envelope, and returns the plaintext dict (or `None` if unconfigured).
  Requires a tenant-scoped (RLS) session.
- **The agent worker has no DB access** and does not dial. Outbound SIP dialing lives
  entirely in the **control plane**: `LiveKitGateway.create_sip_participant()`, called
  today only by the Voice Lab endpoint (`api/v1/voice_lab.py`). The production `/calls`
  flow does not dial out yet. → The trunk lookup + decrypt must live in the control
  plane, which has the tenant-scoped session and `app.state.kms`.
- The trunk id reaches the gateway via
  `build_livekit_gateway(settings, secrets)` →
  `LiveKitGateway(sip_trunk_id=settings.livekit_sip_trunk_id)` → `self._sip_trunk_id`,
  consumed in `create_sip_participant`.
- The frontend hard-codes the slug + key:
  `IntegrationsSection.tsx` → `INTEGRATION_TYPE = "twilio_sip"`,
  `CREDENTIAL_KEY = "twilio_sip_trunk"`. `lib/integrations.ts` is generic (no slug).
- KMS DI seam: `control_plane.deps.get_kms(request)` returns `app.state.kms`.

## Decisions

- **Fail closed.** If a tenant has not configured the trunk credential, the outbound
  dial raises the existing `ConflictError("outbound SIP is not configured")`. No env
  fallback.
- **Delete the env var entirely** (`Settings.livekit_sip_trunk_id` + the
  `VERA_LIVEKIT_SIP_TRUNK_ID` block in `env.example`). The DB integration becomes the
  single source of truth.
- **Rename in place.** The seeded catalog type becomes `livekit_outbound_trunk_id` with
  schema `{"trunk_id": "string"}`. No Alembic migration: migration `0001` materializes
  table DDL from `Base.metadata`; nothing about the *schema* changes — only seed data
  and one runtime credential shape. Pre-launch, no production credentials to migrate.

## Component 1 — Domain-language rename (catalog + frontend)

### Backend
- `vera-backend/scripts/seed.py`:
  `INTEGRATION_TYPES = [{"name": "livekit_outbound_trunk_id",
  "credentials_schema": {"trunk_id": "string"}}]`.

### Frontend
- `vera-frontend/src/components/settings/IntegrationsSection.tsx`:
  - `INTEGRATION_TYPE = "livekit_outbound_trunk_id"`, `CREDENTIAL_KEY = "trunk_id"`.
  - Update the seed-referencing comment.
  - Render a humanized display label ("LiveKit outbound trunk") in the panel header /
    form labels instead of the raw slug; keep the slug as the API identifier and
    `CREDENTIAL_KEY` as the request body key. (Minimal copy change — the slug is no
    longer user-facing text.)
- `vera-frontend/src/lib/integrations.ts`: no change (generic wrapper).

### Dev-DB note (not code)
The seeder upserts by `name`, so an existing dev DB keeps the orphaned `twilio_sip`
row. Pre-launch with no real credentials, so reset + re-seed the dev DB, or manually
`DELETE FROM integration_type WHERE name = 'twilio_sip';` (its dependent `integration`
rows, if any, must go first — FK `ondelete=RESTRICT`). Document in the PR description;
no migration.

## Component 2 — Control-plane dial path reads from DB, env removed

### `apps/control_plane/src/control_plane/livekit_gateway.py`
- Remove the `sip_trunk_id` constructor parameter and the `self._sip_trunk_id` field.
- `create_sip_participant(self, room_name, phone_number, trunk_id: str)` — `trunk_id`
  becomes a required argument. Keep a defensive guard:
  `if not trunk_id: raise ValueError("outbound SIP trunk is not configured")`.
- `build_livekit_gateway`: delete the `sip_trunk_id=settings.livekit_sip_trunk_id` line
  (the gateway no longer carries trunk state).

### `apps/control_plane/src/control_plane/api/v1/voice_lab.py`
- Add a tenant-scoped session dep (`TenantSession` from `common`) and a KMS dep
  (new `Kms = Annotated[KeyManagementService, Depends(get_kms)]` alias in `common.py`,
  or `Depends(get_kms)` inline).
- Replace the `settings.livekit_sip_trunk_id is None` precondition. For `is_outbound`:
  ```python
  creds = await get_integration_credentials(
      session, kms, integration_type_name="livekit_outbound_trunk_id"
  )
  trunk_id = creds.get("trunk_id") if creds else None
  if not trunk_id:
      raise ConflictError(message="outbound SIP is not configured")
  ```
  (phone-number E.164 validation unchanged.)
- Pass `trunk_id` into `livekit.create_sip_participant(room_name, body.phone_number, trunk_id)`.
- `settings: AppSettings` dep may be dropped if no longer used after the change.

### `packages/vera_core/src/vera_core/config/settings.py`
- Delete the `livekit_sip_trunk_id` field (lines around 108–110).

### `vera-backend/env.example`
- Delete the `VERA_LIVEKIT_SIP_TRUNK_ID` block (the comment + commented example line).

## Component 3 — Tests

### `tests/integration/control_plane/test_voice_lab.py`
- Replace the `trunk_configured` fixture (which overrode `get_settings_state`) with a
  fixture that, for the test tenant, inserts the `livekit_outbound_trunk_id`
  `IntegrationType` and a sealed `Integration` row whose `trunk_id` is set via the
  test `LocalDevKMS` (`seal_credentials`). Use the same KMS the app under test uses
  (`create_app(kms=...)` injects `LocalDevKMS(master_key=b"a"*32)`).
- `test_outbound_without_trunk_configured_returns_409` stays valid as-is (no credential
  configured → fail closed).
- `test_outbound_with_trunk_and_valid_phone_places_sip_call`: keep; adjust the
  `FakeLiveKit.sip_calls` assertion if the fake records the new `trunk_id` argument.
- Check `conftest.FakeLiveKit.create_sip_participant` signature accepts the added
  `trunk_id` argument (update the fake to record/accept it).

### `tests/unit/integrations/test_credentials.py`
- Update `twilio_sip` / `twilio_sip_trunk` references to `livekit_outbound_trunk_id` /
  `trunk_id` for consistency (these are generic-helper tests; the names are incidental).

## Out of scope

- Wiring the production `/calls` flow to actually dial out (it creates the room but does
  not place a SIP call today). This change only moves the *source* of the trunk id for
  the existing dial path (Voice Lab); when `/calls` grows outbound dialing it reuses the
  same `get_integration_credentials(...)` lookup.
- Supporting multiple integration types or a catalog-listing API.

## Verification

- `just check` (ruff + mypy --strict + pytest) in `vera-backend`.
- Frontend typecheck/lint in `vera-frontend`.
- `/simplify` on the diff, then re-run `just check` (per `vera-backend/CLAUDE.md`).
- Manual: re-seed dev DB, configure a trunk via Settings → Integrations, start an
  outbound Voice Lab session, confirm the SIP call is placed with the configured trunk;
  confirm an unconfigured tenant gets a 409.
