# Handoff: update the IBV Google Sheet's Apps Script for the new infertility schema

## Context — what already happened (a prior session, now done)

Repo: Vera 2.0 (`vera-backend` + `vera-frontend` monorepo), a HIPAA-regulated voice AI
platform. Branch `fix/infertility-schema`, HEAD `c21dc28`, 14 commits ahead of
`origin/dev`, not yet pushed/merged/PR'd. **Read `vera-backend/CLAUDE.md` and
`vera-backend/apps/control_plane/src/control_plane/CLAUDE.md` before touching any
patient-data or API-key code — this is a HIPAA PHI boundary.**

A prior session updated the `infertility_treatment` form schema (the DSL source is
`vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`) and
published two new `SchemaVersion`s. Full detail is in two plan documents already
committed on this branch — read them for the complete story if you need it:
- `docs/superpowers/plans/2026-07-14-infertility-schema-v2-content-update.md`
- `docs/superpowers/plans/2026-07-15-remove-prerequisite-fields.md`

**Your job is NOT to touch the backend schema again.** It's done, reviewed, tested,
and published. Your job is the **separate, next part**: bring the Google Sheet's Apps
Script up to date so it can keep submitting patient intake data through Vera's real
intake API, given the schema shape changed.

## The one file you're here to edit

**`appscript.js`** at the repo root — this is the actual Google Apps Script source
bound to the IBV intake spreadsheet. **It is currently untracked in git** (`git status`
shows `?? appscript.js`) — decide with the user whether to `git add` it into a tracked
location as part of this work (repo convention would suggest something like
`vera-backend/scripts/appscript/` or similar, but confirm with the user — don't just
move it unilaterally).

Companion doc: `vera-backend/docs/ibv-sheet-upload-setup.md` — the local-dev runbook
for testing this Apps Script against a local backend (via a `pinggy` tunnel, since Apps
Script runs in Google's cloud and needs a public URL to reach `localhost`). Read it before
testing anything.

There is **no other** Sheets/Apps Script integration anywhere else in the codebase —
this is the one and only external-submission surface. A design doc
(`vera-backend/docs/superpowers/specs/2026-07-02-form-schema-dsl-v2-design.md:595`)
explicitly documents this coupling: *"Renamed paths mean new intake payload keys —
coordinate the Apps Script."* That's exactly the situation now.

## The real intake API contract (verified against the actual code, not assumed)

```
POST /api/v1/patient-forms
Authorization: Bearer vk_<tenant_id>.<key_id>.<secret>   (an intake:write-scoped API key)
Content-Type: application/json

{
  "form_type_id": "<form_schema.id UUID>",
  "schema_version_id": "<schema_version.id UUID>",
  "intake_payload": {
    "<section_key>": { "<field_key>": <value>, ... },
    ...
  }
}
```

Key facts (file:line references, verified this session):
- Endpoint: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py:121` (`upload_patient_form`).
- Request model: `PatientFormUploadRequest` (same file, line 84) — `intake_payload: dict[str, Any]` is **nested by bare section key** (`"insurance_information": {"plan_type": "..."}`), **not** dotted root-anchored paths like `sections.insurance_information.plan_type`. Sending a literal `"sections"` wrapper key yourself is a tested rejection case — don't do it, the endpoint adds that wrapper internally for validation only.
- Auth: `require_scope("intake:write")` (line 134) — a custom `vk_…` bearer-token API key, minted once via a separate, human-session-gated `POST /api-keys` endpoint (`api_keys.py:99`, requires `TENANT_ADMIN`+`Idempotency-Key`). The plaintext token is shown exactly once at creation; only a salted hash is stored after that. Never log/commit/echo a real `vk_…` token.
- **The endpoint does NOT resolve "the currently published version" for you.** You must already know the exact `form_type_id` (=`form_schema.id`) and `schema_version_id` (=`schema_version.id`) UUIDs. There is no lookup-by-`insurance_type` endpoint today. Per the setup doc, these are currently obtained by a manual SQL query:
  ```sql
  SELECT fs.id AS form_type_id, sv.id AS schema_version_id
  FROM form_schema fs
  JOIN schema_version sv ON sv.schema_id = fs.id
  WHERE fs.insurance_type = 'infertility_treatment' AND sv.status = 'published';
  ```
  and pasted into Apps Script **Script Properties** (`FORM_TYPE_ID_<ENV>`, `SCHEMA_VERSION_ID_<ENV>`) per environment (`DEV`/`STAGING`/`LOCAL`, selected by cell `BB6`). **These IDs go stale every time the schema is re-seeded** (a re-publish demotes the old version and mints a new `schema_version_id`) — this already bit the team once (see the setup doc's "Gotchas"). Flag to the user whether a small `GET /form-schemas?insurance_type=...&status=published` lookup endpoint is worth adding so the Sheet doesn't need manual re-pasting after every republish — that endpoint does not exist yet.
- 422 on unknown field paths (a payload key that doesn't resolve to any current schema leaf) — implemented via `unknown_payload_paths` in `vera_core/forms/intake.py`.
- 422 on missing required fields — implemented via `missing_required` in the same file, listing the offending root-anchored paths.
- Dates: send ISO `YYYY-MM-DD` strings. The Apps Script already does this correctly (`Utilities.formatDate(value, timeZone, "yyyy-MM-dd")`).
- **No idempotency protection** on this endpoint (unlike `POST /api-keys`) — a retried submission (e.g. after a network hiccup) can create a duplicate `PatientForm`. Not something you need to fix, but worth mentioning to the user as a known gap.
- `vera-backend/scripts/seed_patient_data.py` is a **dev/demo-only** direct-DB seeder — it does NOT go through this HTTP API and is not a model for what the Apps Script should do. (A synthetic patient form was already seeded this way against the latest schema — `patient_form_id 019f6585-ad8f-7910-a5bb-9ff68e926517`, `schema_version=2`, tenant `vera-health-example` — useful for visually comparing against in the UI, not relevant to the Apps Script's own submission path.)

## Exactly what changed in the schema, and what that means for `DATA_MAPPING`

`appscript.js`'s `DATA_MAPPING` object (lines 1–340) mirrors the schema's nesting
exactly — it's what becomes `intake_payload`. Two field **moves** are real, load-bearing
changes (submitting the old shape will now either lose data silently or trigger a 422
"missing required fields", since the field being asked for is `required=True` in the DSL
either way); everything else is a no-op for this file.

### 1. Diagnostic Testing — REQUIRES an edit (load-bearing)

The schema now nests all 8 diagnostic CPT codes under one new parent group,
`labs_xray_ultrasound` (this made the UI merge the Service/ICD-10 columns — see the
2026-07-14 plan for why). `DATA_MAPPING.diagnostic_testing` must gain that wrapper key:

```js
// BEFORE (current appscript.js, lines 79-129) — WRONG for the new schema:
diagnostic_testing: {
  diagnostic_testing_covered: ["TODO_CELL", false],
  cpt_58340: { covered: ["M33", false], copay: ["P33", false], coinsurance: ["S33", false], prior_auth: ["V33", false] },
  cpt_82670: { covered: ["M34", false], copay: ["P34", false], coinsurance: ["S34", false], prior_auth: ["V34", false] },
  cpt_83001: { covered: ["M35", false], copay: ["P35", false], coinsurance: ["S35", false], prior_auth: ["V35", false] },
  cpt_83002: { covered: ["M36", false], copay: ["P36", false], coinsurance: ["S36", false], prior_auth: ["V36", false] },
  cpt_84146: { covered: ["M37", false], copay: ["P37", false], coinsurance: ["S37", false], prior_auth: ["V37", false] },
  cpt_84443: { covered: ["M38", false], copay: ["P38", false], coinsurance: ["S38", false], prior_auth: ["V38", false] },
  cpt_84144: { covered: ["M39", false], copay: ["P39", false], coinsurance: ["S39", false], prior_auth: ["V39", false] },
  cpt_76830: { covered: ["M40", false], copay: ["P40", false], coinsurance: ["S40", false], prior_auth: ["V40", false] },
},

// AFTER — matches the current schema (cell refs unchanged, only nesting added):
diagnostic_testing: {
  diagnostic_testing_covered: ["TODO_CELL", false],
  labs_xray_ultrasound: {
    cpt_58340: { covered: ["M33", false], copay: ["P33", false], coinsurance: ["S33", false], prior_auth: ["V33", false] },
    cpt_82670: { covered: ["M34", false], copay: ["P34", false], coinsurance: ["S34", false], prior_auth: ["V34", false] },
    cpt_83001: { covered: ["M35", false], copay: ["P35", false], coinsurance: ["S35", false], prior_auth: ["V35", false] },
    cpt_83002: { covered: ["M36", false], copay: ["P36", false], coinsurance: ["S36", false], prior_auth: ["V36", false] },
    cpt_84146: { covered: ["M37", false], copay: ["P37", false], coinsurance: ["S37", false], prior_auth: ["V37", false] },
    cpt_84443: { covered: ["M38", false], copay: ["P38", false], coinsurance: ["S38", false], prior_auth: ["V38", false] },
    cpt_84144: { covered: ["M39", false], copay: ["P39", false], coinsurance: ["S39", false], prior_auth: ["V39", false] },
    cpt_76830: { covered: ["M40", false], copay: ["P40", false], coinsurance: ["S40", false], prior_auth: ["V40", false] },
  },
},
```

### 2. Center of Excellence Required — REQUIRES an edit (load-bearing)

Moved from `authorization_department` into `enrollment` (right after the provider
phone field), in the schema:

```js
// BEFORE (current appscript.js, lines 302-311):
enrollment: {
  enrollment_required: ["AD62", false],
  enrollment_provider_name: ["AD63", false],
  enrollment_provider_phone: ["AD64", false],
},
authorization_department: {
  auth_department_name: ["J70", false],
  auth_department_phone: ["J71", false],
  center_of_excellence_required: ["AD65", false],
},

// AFTER:
enrollment: {
  enrollment_required: ["AD62", false],
  enrollment_provider_name: ["AD63", false],
  enrollment_provider_phone: ["AD64", false],
  center_of_excellence_required: ["AD65", false],
},
authorization_department: {
  auth_department_name: ["J70", false],
  auth_department_phone: ["J71", false],
},
```

**Open question for the user, not yours to decide silently:** the cell reference
(`AD65`) is kept as-is above — but should the *physical* spreadsheet cell also move to
sit visually near the Enrollment block instead of Authorization Department? That's a
Sheet-layout decision (moving a real cell / redesigning the sheet), separate from this
JS mapping change. Ask before doing it.

### 3. Now-dead fields — should be REMOVED from `DATA_MAPPING` (cleanup, not urgent but correct)

`lifetime_maximum.lifetime_cycle_max` and `lifetime_maximum.cycles_used` no longer
exist in the schema at all (removed in the prior session). They're currently mapped to
placeholder `["TODO_CELL", false]` in `appscript.js` (lines 296-297) — i.e. never wired
to a real cell yet, so removing them costs nothing. Leaving them in risks a 422 "unknown
field path" the moment someone *does* wire them to real cells and fills them in, since
`unknown_payload_paths` validation now rejects any payload key that doesn't resolve to a
real schema leaf. Just delete these two lines from `DATA_MAPPING.lifetime_maximum`.

### 4. Everything else in this schema update needs NO `DATA_MAPPING` change

Confirmed by tracing exactly how `intake_payload` gets validated (`resolve_path` walks
the nested dict by key, matching the DSL's dotted path segment-by-segment) — none of
these affect the wire shape:
- **Plan Type → Health Plan Type**, **Policy Situs → Home Plan / Policy Situs** + hint:
  only the human-facing *title* changed. The DSL keys (`plan_type`, `policy_situs`) are
  unchanged — `DATA_MAPPING.insurance_information.plan_type`/`policy_situs` stay exactly
  as they are.
- **Infertility Treatment reordering** (Ovulation Induction now first, etc.) and the
  three retitled treatments: only the *voice-agent question order* and UI table row
  order changed (a Python dict's insertion order). The field keys
  (`ovulation_induction`, `intrauterine_insemination`, …) and their nested `cpt_*` keys
  are unchanged — none of `DATA_MAPPING.infertility_treatment`'s keys need to move.
- **Egg Cryopreservation Cancer losing its ICD-10 code**: that's schema *metadata*
  (`codes.icd10`, used for the voice prompt and UI table), never part of the submitted
  answer values. No `DATA_MAPPING` change.
- **`prerequisite_fields` removal** (backend DSL + frontend UI-color-only concept):
  purely cosmetic, never touched `intake_payload` shape at all. No `DATA_MAPPING` change.

## Known pre-existing gaps in `appscript.js` (not caused by the schema update — found while reading it, worth flagging to the user, your call whether to fix as part of this task)

- Several `DATA_MAPPING` entries are still literal placeholders, `["TODO_CELL", false]`,
  never wired to a real spreadsheet cell: `diagnostic_testing.diagnostic_testing_covered`,
  `male_partner_coverage.male_partner_covered`, `infertility_treatment.infertility_tx_covered`,
  `third_party_administrator.tpa_exists`, `pharmacy_benefit_manager.pbm_exists`,
  `infertility_specialty_pharmacy.isp_exists`. `resolveRefs()`'s cell-address regex
  (`/^[A-Z]+\d+$/`) doesn't match the literal string `"TODO_CELL"`, so today these fall
  through to a bug-looking fallback branch that returns the raw `["TODO_CELL", false]`
  array as the "value" rather than a real cell read. This predates the schema changes —
  it's a separate, pre-existing incompleteness in the Sheet integration, not something
  this handoff's two required edits caused.
- There's a large commented-out earlier version of `sendDataToExternalSystem` (lines
  379–458) still sitting in the file — dead code from an earlier iteration, safe to
  delete if you're doing a cleanup pass, but not required for this task.
- No idempotency protection on `POST /api/v1/patient-forms` (mentioned above).

## Suggested first steps for you (the new session)

1. Read `vera-backend/CLAUDE.md` and `control_plane/CLAUDE.md` (PHI/HIPAA guardrails —
   you'll be touching API-key auth and patient-data submission).
2. Confirm with the user: should `appscript.js` be moved into a tracked path and
   committed as part of this work (it's currently untracked)? Where does the *actual*
   Google Sheet + its bound Apps Script project live (a Sheet URL, a `clasp` project, or
   is this local file the only copy that then gets manually pasted into the Apps Script
   editor)? You likely need that pointer from the user to actually deploy/test any
   change — this repo file is the source of truth for the code, but Google Sheets Apps
   Script projects are usually edited/deployed through Google's own editor or `clasp`.
3. Given this touches an external system, real money-adjacent patient intake, and a
   currently-untracked file with real product impact, use the **brainstorming** skill
   first if the user's ask is at all open-ended (e.g. "should we also fix the TODO_CELL
   gaps") before jumping to **writing-plans** — the two required `DATA_MAPPING` edits
   above are small and mechanical enough to just do directly and verify against
   `vera-backend/docs/ibv-sheet-upload-setup.md`'s local-dev flow (pinggy tunnel, real
   `POST /api/v1/patient-forms` call, confirm a `patient_form` row lands with the
   `labs_xray_ultrasound`-nested diagnostic data and `enrollment.center_of_excellence_required`
   both populated correctly) — but scope-check with the user before doing more than that.
4. After editing, re-run the setup doc's step 6 SQL query to get **fresh**
   `form_type_id`/`schema_version_id` for whatever environment you're testing against —
   they will have changed since the last time anyone touched Script Properties, per the
   "Gotchas" section.
