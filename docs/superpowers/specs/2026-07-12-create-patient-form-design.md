# Create New Patient Form (In-App Intake) — Design

**Date:** 2026-07-12
**Status:** Approved

## Problem

New patient forms enter the system only through a Google Apps Script spreadsheet
that POSTs to `POST /api/v1/patient-forms` with an `intake:write` API key. The
sheet lives outside the system: it hardcodes a `schema_version_id`, drifts from
the schema source of truth (re-seeding breaks it), and is hard to maintain.

## Goal

Let a tenant user create a new patient form from the Data Management page:
click **Add patient form** → pick a form schema → fill the schema's latest
published version rendered by the existing dynamic form renderer → submit.
Everything stays schema-driven; a form cannot be submitted without its
`system_fields` filled.

## Decisions

- **AppScript intake stays running untouched.** The in-app form is an
  additional creation path; the sheet can be retired later with zero coupling.
- **Permission:** `forms:write` gates both the button and the create endpoint
  (its seeded description already covers creation). No new permission.
- **Post-create UX:** close the modal, show a success toast, refresh the
  worklist (new row appears with status `ready_for_processing`).
- **Backend shape:** new session-authed create endpoint + a shared creation
  helper extracted from the existing API-key endpoint (not dual-auth on the
  existing endpoint).
- **Version resolution:** the server resolves the schema family's latest
  published version; the client never supplies a version id. The DB invariant
  `uq_schema_version_published_per_schema` (at most one published version per
  family) makes this a single lookup.

## Backend

### Shared creation helper

Extract the body of the current `POST /patient-forms`
(`apps/control_plane/src/control_plane/api/v1/patient_forms.py:117-235`) into a
helper taking `(session, tenant_id, schema_version, intake_payload)` that,
exactly as today:

1. runs `missing_required` against the schema's `system_fields`
   (`vera_core/forms/intake.py`) → 422 listing missing paths;
2. runs `promote_columns` → 422 on invalid values;
3. inserts `PatientForm` with `status=ready_for_processing`;
4. inserts one `intake`-source `FieldAnswer` per leaf value.

The API-key endpoint is refactored to call it — behavior-preserving, same
responses, proven by its existing tests passing unchanged.

### `POST /patient-forms:create` (new, session-authed)

- Gated `require("forms:write")`; tenant from session context (same pattern as
  the worklist/detail endpoints). PHI-write audit consistent with
  `disputes:resolve`.
- Body: `{schema_id, intake_payload}` — no version id.
- Resolves the family's published version server-side (reuse/relocate the
  `_published_schema_version` helper currently in `prompts.py:119-127` into a
  shared location instead of importing across routers). No published version
  or unknown `schema_id` → 409/422 with a clear message.
- Calls the shared helper; returns 201 with the created form's id + promoted
  summary (enough for a toast).

### `GET /patient-forms/schemas` (new, session-authed)

- Gated `require("forms:read")`. Returns only families that have a published
  version: `[{schema_id, name, insurance_type, published_version_id,
  published_version}]`.
- Catalog data, non-PHI → no PHI audit (same stance as the existing
  `GET /schema-versions/{id}`).
- The platform catalog endpoint (`form_schemas.py`, super-admin) stays
  untouched.
- Route-order note: the path collides with `GET /patient-forms/{form_id}`;
  declare `/patient-forms/schemas` before the parameterized route (FastAPI
  matches in declaration order) and cover it with a test (`GET
  /patient-forms/schemas` must not 404/422 as a bad form id).

## Frontend

### Entry point

`DataManagement.tsx` gets an **Add patient form** button next to the existing
filters, rendered only when `usePermission("forms:write")`.

### `CreatePatientFormModal` (new, `components/ibv/`)

Mounted alongside `IbvFormModal` inside the globally-mounted `IbvProvider`
(`AppShell.tsx`). Two steps:

1. **Pick schema** — fetches `GET /patient-forms/schemas`; select shows
   `name (insurance type)`; empty state if no family has a published version.
2. **Fill form** — renders the existing `<SchemaForm />` unchanged; footer:
   Back / Cancel / Submit. Submit disabled while in flight (double-submit
   guard).

### `IbvProvider` create mode

New `openCreate(schema)`:

- Loads the published version via the existing `loadSchema` (session cache
  reused).
- Seeds `values = {}` plus any leaf `default`s (matches the backend rule that
  a `system_fields` target with a default counts as filled); empty `disputes`;
  `formId: null`; `mode: "create"`. `DisputeControls` already render nothing
  when a path has no dispute, so `SchemaForm`/`FieldRenderer` need no changes.
- **Create-mode validation** — a variant of `validateAll`
  (`lib/ibv/validation.ts`): the required set is the schema's `system_fields`
  target paths (defaults exempt); existing pattern/range/date-format checks
  still apply to any filled field; condition-inapplicable fields still
  skipped. Submit runs validation and blocks with the renderer's existing
  inline `invalid` treatment until required fields are filled.
- **Submit** — build the nested `intake_payload` from the flat `values` map
  (small unflatten-by-`field_path` util, the inverse of the intake flattener),
  call `createPatientForm(schema_id, intake_payload)`, then success toast →
  close → bump `savedTick` so the worklist refetches.

### API client

Two additions to `lib/patient-forms/api.ts`, riding the existing `apiRequest`:
`listIntakeSchemas()` and `createPatientForm(schemaId, intakePayload)`.

The existing open/edit/dispute-resolve flow is unchanged.

## Error handling

- **Stale published version** (demoted between step 1 and submit) or unknown
  `schema_id` → backend 409/422; frontend shows a modal-level error banner.
- **Backend re-validation is authoritative**: the endpoint re-runs
  `missing_required` + `promote_columns` regardless of client checks; 422
  responses carry missing paths / bad values, which the frontend maps onto
  field errors where possible, banner otherwise.
- **PHI discipline**: failure paths log exception type only (existing
  convention); no PHI in logs or error messages beyond field paths.

## Testing

- **Backend (pytest):**
  - Existing intake endpoint tests pass unchanged after the helper extraction
    (regression proof).
  - `POST /patient-forms:create`: happy path (form + field_answer rows +
    promoted columns), missing system_fields → 422, no published version →
    409/422, missing `forms:write` → 403, tenant scoping.
  - `GET /patient-forms/schemas`: published-only filtering, permission.
- **Frontend:** unit tests for create-mode validation (system_fields
  required, default-exempt) and the unflatten util; `tsc` + `eslint` + build.
- **End-to-end verification:** boot backend + frontend, create a form through
  the UI, confirm the row appears in the worklist and opens correctly in the
  existing detail modal.

## Out of scope

- Retiring or flagging off the AppScript intake path.
- Draft/partial-save of an in-progress create.
- A dedicated `forms:create` permission.
- Any change to the platform (super-admin) schema catalog endpoints or UI.
