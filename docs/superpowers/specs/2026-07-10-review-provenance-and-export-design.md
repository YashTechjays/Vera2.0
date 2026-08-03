# Review Provenance & Export — Design Spec (Phases 3 + 4)

**Date:** 2026-07-10
**Status:** Approved design, pre-implementation
**Depends on:** Phase 1 (filled form eval, `2026-07-09-filled-form-eval-design.md`) and
Phase 2 (retry call, `2026-07-09-retry-call-design.md`) — both on PR #75. This work
builds on the data those phases record (`field_evaluation`, `call_lineage`,
`call_form_snapshot`, `call.mode`, retry reasons).

---

## 1. Problem & goal

Phases 1–2 made the pipeline *record* everything about how a form got filled — which
call wrote each value, what the judge thought of it, how attempts chain through
retries, and why a form fell to `EXCEPTION_REVIEW` — but nothing *displays* it, and
there is no way to get a verified form out of the system.

**Phase 3 (Review Provenance):** surface that record to the human reviewer —
per-field provenance, a per-form call-attempt timeline, and a triage view for
`EXCEPTION_REVIEW` forms.

**Phase 4 (Export):** produce the deliverable — an XLSX export of a COMPLETED form
(values + provenance), gated by a new permission, with disclosure audit and an
artifact ledger.

Both phases ship as **one combined spec and one implementation effort** (user
decision), structured so the backend work lands before the frontend consumes it.

**Out of scope (deferred):** field-level re-ask from the review UI (selecting fields
to send back to IN_QUEUE as `retry_fields`); PDF renderer (the mapping layer is
format-agnostic, the renderer is a follow-up); GCS artifact storage + signed URLs
(the ledger schema leaves room); dedicated full-page review workspace (Approach B —
this spec's API work is its prerequisite either way); export history UI.

---

## 2. Phase 3 — backend

### 2.1 Per-field provenance on the form detail

`GET /patient-forms/{form_id}` — each `FieldView` gains:

```
provenance: {
  attempt: int,            # 1-based position of the writing call in the form's call order
  mode: "full" | "retry",  # call.mode of the writing call
  judge: {                 # latest field_evaluation for the current answer, or null
    confidence: int,
    supported: bool,
    evidence: str | null,
  } | null,
} | null
```

- Present only for `source == "ai_call"` fields (their `field_answer.call_id` is the
  writing call); `intake`/`human` fields get `provenance: null` — the existing
  `source` chip covers them.
- Attempt numbers come from ordering the form's calls by `created_at` (UUIDv7 ids
  tie-break identically). The latest-evaluation-per-answer join reuses the
  MAX(created_at) subquery pattern from `load_field_status`
  (`vera_core/services/field_status.py`).
- The judge's `evidence` is de-identified transcript text (it was tokenized before
  the LLM saw it) and is already stored on `field_evaluation`; it is disclosed only
  inside this authed, audited, no-store response — same boundary as the dispute
  `evidence` the endpoint already returns.

### 2.2 Call-attempt timeline endpoint

`GET /patient-forms/{form_id}/calls` (new) — the attempt history, oldest first:

```
[{
  id: UUID,
  attempt: int,               # 1-based, same ordering rule as §2.1
  mode: "full" | "retry",
  status: str,                # call.current_status
  created_at: datetime,
  retry_of: UUID | null,      # call_lineage.parent_call_id where this call is retry_call_id
  changed_paths: [str],       # field paths whose value differs between the call's
                              # call_form_snapshot before_state and after_state
}]
```

- **Minimum necessary:** `changed_paths` carries paths only, never values — the
  reviewer sees values in the form view itself. A missing/partial snapshot row
  yields `changed_paths: []`, never an error.
- Auth chain: identical to the form-detail endpoint (authenticate → RBAC (same
  permission as form detail) → tenant session → audit → no-store). The response
  carries paths, statuses and timings — no field values.
- A form with no calls returns `[]`.

### 2.3 `review_reason` column

New nullable `patient_form.review_reason` (text), idempotent migration
(`ADD COLUMN IF NOT EXISTS`, per the repo's fresh-DB-CI rules).

- `evaluate_call` already computes a reason for its audit record; it now also
  stamps the column when transitioning to `EXCEPTION_REVIEW`. Values:
  `token_value`, `retries_exhausted`, `llm_error`, `no_transcript`.
- Cleared (`NULL`) on any transition **out of** `EXCEPTION_REVIEW` (manual
  re-queue or completion) — single write point in the status-change endpoint +
  `evaluate_call`'s `_finish`, both of which already own transitions.
- Exposed on `PatientFormSummary` (the list endpoint) so the Needs Review tab can
  show *why* without per-row audit queries. The manual `PUT .../status` path to
  EXCEPTION_REVIEW (if any) leaves it `NULL` — reason is a pipeline artifact, and
  the UI renders a `—` for it.
- Deliberately **not** adding `dispute_count` to the list response — it was removed
  from `PatientFormSummary` once before (see git history); reason + completion %
  carry the triage signal.

---

## 3. Phase 3 — frontend

All inside the existing surfaces; no new routes.

### 3.1 Provenance popover (`FieldRow`)

AI-sourced fields get a small info affordance next to the existing confidence chip
(shadcn Popover, same pattern as `DisputeControls` tooltips):

> Attempt 2 of 3 (retry) · judge 85, supported
> “…the plan covers infertility treatment at eighty percent…”

Fields with no evaluation show source/attempt only. Intake/human fields are
unchanged.

### 3.2 Call history tab (`IbvFormModal`)

The modal body gains a two-tab bar: **Form** (today's `SchemaForm`, default) and
**Call history**. The new tab fetches `GET /patient-forms/{id}/calls` lazily on
first open and renders one card per attempt:

- date · mode badge (FULL/RETRY) · call status
- "retry of attempt N" when `retry_of` resolves to an earlier attempt
- "n fields updated", expandable to the changed **field labels** (paths resolved to
  titles via the already-cached schema document)

### 3.3 Needs Review tab (`DataManagement`)

A third worklist tab beside **All Data** / **Completed**: presets
`status=exception_review` and adds two columns — **Reason** (badge from
`review_reason`, `—` when null) and **Age** (relative time since `updated_at`).
Reuses the existing tab/filter/pagination plumbing; no new page.

---

## 4. Phase 4 — export

### 4.1 Endpoint

`POST /patient-forms/{form_id}/export` →

- **Gates:** `require("forms:export")` (new permission, §4.4) **and** form status
  `COMPLETED`, else the standard validation-error envelope. Non-existent form → 404.
- **Response:** streams the XLSX bytes.
  `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
  `Content-Disposition: attachment; filename="ibv-{form_id}.xlsx"` (opaque form id —
  **never** the patient name — in the filename), `Cache-Control: no-store`.
- **Side effects (same transaction):** insert one `export_artifact` row (§4.3) and
  emit a disclosure `AuditRecord` — new `AuditEvent.FORM_EXPORTED`, detail carries
  field *names/counts* and the artifact id, never values.
- Generation is in-process and synchronous (a form is a few hundred cells; no job
  queue needed). Repeat exports are legal and each one is a distinct disclosure —
  no idempotency key.

### 4.2 Template-mapping layer (pure, DB-free)

`vera_core/forms/export.py`:

```
build_workbook(
    schema_doc: FormSchemaDoc,
    values: Mapping[str, Any],          # from load_current_values
    provenance: Mapping[str, Provenance],  # §2.1 shape, keyed by path
    call_history: Sequence[CallAttempt],   # §2.2 shape
) -> bytes
```

- **Sheet 1 “Form”:** sections as header rows, then label / value pairs in schema
  order (v2 document key order **is** display order — walk `leaf_gates(doc)`, skip
  inapplicable leaves, render declared defaults for unfilled leaves per DSL §4.4).
  v1 documents: flat path/value listing (legacy fallback).
- **Sheet 2 “Provenance”:** one row per field — path, label, source, attempt, mode,
  judge confidence, supported; below it a call-history block (attempt, mode,
  status, date, retry-of).
- The input shape is format-agnostic: a future PDF renderer consumes the same
  arguments; the endpoint only switches on the (future) `format` parameter —
  Phase 4 hardcodes XLSX.
- New dependency: `openpyxl` (workspace `vera_core` package). Pure function → unit
  tested by opening the produced bytes with openpyxl and asserting cells.

### 4.3 `export_artifact` ledger

New table (idempotent migration; RLS like other tenant tables):

```
export_artifact(
  id uuid pk,                -- uuid7
  tenant_id uuid fk,         -- RLS key
  form_id uuid fk,
  format text,               -- 'xlsx' (CHECK-constrained; widen later)
  sha256 text,               -- hash of the streamed bytes
  gcs_uri text null,         -- reserved for the future GCS variant; always NULL now
  exported_by uuid fk,       -- app_user
  created_at timestamptz,    -- DB clock
)
```

No file is stored in Phase 4 — the row + hash + audit record prove what left the
perimeter, when, and who took it. (ADR `vera2-database-design.md` §4.5.4 adopted
this table; the direct-stream variant makes `gcs_uri` nullable-for-later.)

### 4.4 `forms:export` permission

- Seeded into the permission catalog; granted to the **TENANT_ADMIN** and
  **SUPERVISOR** system roles by default (user decision), via the same
  seed + backfill-migration pattern as `voice_lab:sandbox`
  (`feat(rbac): backfill voice_lab:sandbox onto existing calls:read roles`).
- Export is a PHI **disclosure**, so it gets its own permission rather than piggy-
  backing on form read access (per the control-plane CLAUDE.md masking/minimum-
  necessary rules). Tenant admins can grant it to other roles through the existing
  roles UI — no new UI needed.

### 4.5 Frontend

**Export XLSX** button in the modal header bar (next to the status controls):
visible only when the form is `COMPLETED` **and** the session holds `forms:export`
(existing permission-context pattern). Click → `POST`, download the streamed blob
via an object URL, surface failures through the existing error/toast pattern.
The worklist gets no per-row export action in this phase — the modal is the single
export point.

---

## 5. Error handling & edge cases

| Case | Behavior |
| --- | --- |
| Export of non-COMPLETED form | validation-error envelope (no artifact row, no audit) |
| Export permission missing | standard 403 (RBAC deny is itself audited, as today) |
| Form with zero calls | timeline `[]`; export sheet 2 has an empty history block |
| Field answer with no evaluation | `judge: null`; popover shows source/attempt only |
| Missing/partial `call_form_snapshot` | `changed_paths: []`; never a 500 |
| `review_reason` unset (manual EXCEPTION_REVIEW) | column NULL, UI renders `—` |
| v1 schema document | provenance/popover work unchanged (path-keyed); export falls back to flat path/value sheet |
| openpyxl generation failure | 500 envelope; **no** artifact row / audit (transaction rolls back with the response error) |

## 6. Global constraints (inherited)

PHI never in logs/traces/URLs/filenames (opaque form-id filenames; `no-store` on
every PHI response; the XLSX itself is PHI and exists only in the response stream);
values enter only authed + audited surfaces; audit detail carries names/counts
only; timestamps from the DB clock; all DB work inside tenant-scoped sessions
(RLS); idempotent migrations (`ADD COLUMN IF NOT EXISTS`, guarded constraints,
random-hex revision ids); PEP 695 type params; asyncio only; response envelope via
`ok()` / `CustomAPIException`; new backend tests under `tests/unit/<area>/` and
`tests/integration/` (CI `testpaths`). No new long-lived loops → no boot-
verification requirement.

## 7. Testing

**Backend unit (pure):** snapshot-diff helper (before/after → changed paths, incl.
missing/partial states); attempt-numbering helper; `build_workbook` (open bytes
with openpyxl, assert section headers, values in schema order, provenance rows,
history block; v1 fallback).

**Backend integration (docker Postgres, fakes):** form-detail response carries
`provenance` (ai_call field with eval; human field null); `/calls` timeline shape
+ lineage + changed paths on a seeded 2-attempt form; `review_reason` stamped by
`evaluate_call` on each EXCEPTION_REVIEW reason and cleared on re-queue;
export endpoint — 403 without permission, validation error on non-COMPLETED,
success streams parseable XLSX + writes `export_artifact` + emits FORM_EXPORTED
audit with names-only detail.

**Frontend:** `tsc` + eslint + existing test suite + build; new components follow
the existing IbvProvider/shadcn patterns (popover render, tab switch fetch-once,
Needs Review tab filter, export button gating on status + permission).

## 8. Sequencing

Single branch (`feat/review-and-export`), backend-first:

1. Phase 3 backend (§2) — migrations, provenance serialization, `/calls`, reason.
2. Phase 4 backend (§4.1–4.4) — permission, ledger, mapping layer, endpoint.
3. Phase 3 frontend (§3).
4. Phase 4 frontend (§4.5).

Each step lands green (`just check` / frontend gates) before the next; the plan
doc will break these into TDD tasks.
