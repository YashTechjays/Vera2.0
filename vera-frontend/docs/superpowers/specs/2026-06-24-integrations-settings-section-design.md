# Integrations section in Settings — design

**Date:** 2026-06-24
**Status:** Approved (design)
**Scope:** Frontend only (`vera-frontend`). No backend or migration changes.

## Goal

Add an **Integrations** section to the Settings page, mirroring the existing
`ApiKeysSection`. A tenant admin configures the single per-tenant outbound
integration credential (Twilio SIP trunk). One integration, one secret value,
per tenant.

## Context — backend is already built

No backend work is required. The following already exist:

- **Model** (`vera_core.models.integrations`): `Integration` (tenant-scoped,
  envelope-encrypted credential, one row per `(tenant_id, integration_type_id)`)
  and `IntegrationType` (global catalog).
- **Seed** (`scripts/seed.py`): one integration type `twilio_sip` with
  `credentials_schema = {"twilio_sip_trunk": "string"}` — a single value.
- **API** (`control_plane/api/v1/integrations.py`), gated by `integrations:manage`,
  RLS-scoped to the caller's tenant:
  - `GET /integrations` → `list[IntegrationSummary]` where
    `IntegrationSummary = { integration_type, status, configured, rotated_at }`.
    Never returns the secret.
  - `PUT /integrations/{integration_type}` with body
    `{ credentials: { <schema-key>: <value> } }` → upserts and envelope-encrypts.
    Returns the same non-secret `IntegrationSummary`. No idempotency header
    required (the endpoint takes no idempotency dependency).

The secret is **write-once**: accepted on write, never returned. The UI can only
ever show whether it is configured, not the value.

## Frontend changes

### 1. `src/lib/integrations.ts` (new)

Typed wrappers over the contract above, snake_case to match the backend:

```ts
export type Integration = {
  integration_type: string
  status: string
  configured: boolean
  rotated_at: string | null
}

export function listIntegrations(): Promise<Integration[]>           // GET /integrations
export function configureIntegration(                                 // PUT /integrations/{type}
  integrationType: string,
  credentials: Record<string, string>,
): Promise<Integration>
```

`configureIntegration` sends `{ credentials }` as the body. The caller supplies
the credentials object so the component stays data-driven (no hard-coded key).

### 2. `src/components/settings/IntegrationsSection.tsx` (new)

Single-integration panel. Mirrors `ApiKeysSection` conventions (state hooks,
`ApiError` handling, status badge styling, success/error messaging).

- **Data-driven, raw slug** — render `integration_type` and the credential-schema
  key directly from the data; no friendly-label mapping in the frontend.
- Loads via `listIntegrations()`. There is a single integration; take the first
  row. If the list is empty, show an empty/unconfigured state for `twilio_sip`.
- **Status badge:** `configured` → "Configured" (emerald), else "Not configured"
  (muted). Show "Last updated" from `rotated_at` when present
  (via existing `formatDate`).
- **One secret input** (`type="password"`), always empty on load (the value is
  never returned). Label derives from configured state: "Set token" when
  unconfigured, "Replace token" when configured.
- **Save** button → `configureIntegration(integration_type, { [schemaKey]: value })`,
  then refresh the row and clear the input. Disabled while saving or when the
  field is empty. Surface 4xx errors inline like the API keys section.

Because the section is fully data-driven, the credential-schema key
(`twilio_sip_trunk`) is the one value the form binds to. With the current
single-key seed there is exactly one field.

### 3. `src/pages/Settings.tsx` (edit)

Mount behind a permission check, next to the API keys section:

```tsx
const canManageIntegrations = usePermission("integrations:manage")
...
{canManageIntegrations && <IntegrationsSection />}
```

## Out of scope (YAGNI)

- Multiple integration types or a type picker — only `twilio_sip` exists.
- Multi-field credential forms — the seeded schema has a single key.
- Revealing / editing the stored secret — it is write-once by design.
- Any backend, model, or migration change.

## Testing

Manual verification in the running app: with `integrations:manage`, the section
appears, shows "Not configured", accepts a token, saves, and re-renders as
"Configured" with a "Last updated" timestamp. Without the permission, the
section is absent. Follow existing frontend test conventions if component tests
are present for `ApiKeysSection`.
