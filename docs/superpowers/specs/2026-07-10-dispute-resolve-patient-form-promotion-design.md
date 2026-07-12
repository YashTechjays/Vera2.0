# Dispute-resolve → patient_form column promotion — Design

**Date:** 2026-07-10
**Status:** Proposed

## 1. Context and problem

`patient_form` carries a set of typed, indexed columns "promoted" out of the dynamic
form answers for fast search/display: `patient_name`, `patient_dob`, `appointment_date`,
`chart_number`, `appointment_type`, `member_policy_id`, `insurance_provider`,
`insurance_provider_phone_number`. Promotion happens once, at intake
(`vera_core.forms.intake.promote_columns`, called from `upload_patient_form`,
`vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py:136-158`).

The dispute-resolve endpoint (`POST /patient-forms/{form_id}/disputes:resolve`,
`resolve_disputes` in the same file, lines 513-692) lets a human correct a field's
current answer. It writes a new `field_answer` row (`source="human"`) and a
`dispute_action` audit row, and recomputes `patient_form.completion_pct` — but it never
re-derives the promoted columns. So editing, say, the insurance-reference fields through
dispute resolution updates `field_answer` but leaves the stale value sitting in
`patient_form.insurance_provider`, which is what worklist search/display actually reads.

Two related bugs surfaced during investigation:

- `promote_columns`'s hardcoded field-key literals for the insurance-reference section
  (`"insurance"`, `"phone_number"`) don't match the current `ibv_standard` catalog's real
  leaf names (`insurance_provider_name`, `insurance_phone_number`). So
  `insurance_provider`/`insurance_provider_phone_number` are likely never populated
  correctly even at intake time for that schema.
- `PatientForm.member_id` is dead: every code path that touches it sets it to `None` (see
  `PromotedIdentifiers.member_id` docstring: "no schema source at intake — always None
  here"); the only live identifier column of that kind is `member_policy_id`, sourced from
  `insurance_information.policy_number`.

## 2. Goals

- Promoted columns stay in sync with the current answer for their mapped field, whether
  that answer is set at intake or changed later via dispute resolution.
- The field → column mapping is declared in the schema DSL itself (per schema version),
  not hand-maintained Python literals — so a new schema, or a renamed field, can't
  silently drift out of sync the way `promote_columns` did.
- A schema can't declare a promoted mapping to a field that isn't guaranteed present at
  intake.
- Drop the dead `member_id` column while touching this area, rather than leaving a
  second, confusing "identifier-shaped" column next to `member_policy_id`.

Non-goals: resolving `call.insurance_provider_id` (the separate, global IVR-routing
master-data FK — unrelated table, unrelated concern) retroactively when a dispute
changes `patient_form.insurance_provider`; that FK is resolved once at call dispatch
(`queue_dispatcher.py`) and re-resolving it after the fact is a call-routing behavior
change out of scope here.

## 3. Approach

Extend the schema DSL (`vera_core.forms.dsl.FormSchemaDoc`) with a new
`promoted_fields: dict[str, str] | None` block: key = the exact `patient_form` column
name, value = the leaf's root-anchored path (`sections.…`), same shape as the existing
`system_fields` block.

This was chosen over (a) keeping two independent hardcoded Python mappings (one for
intake, a new one for dispute-resolve) — rejected, that's the exact drift pattern that
caused the insurance-key bug — and (b) reusing `system_fields` directly as the
column-mapping source — rejected, `system_fields` handles don't correspond 1:1 to
`patient_form` column names (e.g. handle `insurance_provider_name` vs. column
`insurance_provider`; handles `member_id`/`policy_id` both alias `policy_number` for
different purposes), so overloading it would require guessing intent from handle
spelling instead of stating it explicitly.

`promoted_fields` is required to be a **subset of `system_fields`**: every promoted
column's path must already appear in that schema's `system_fields.values()`. Since
`system_fields` targets are exactly what `required_intake_fields` enforces as mandatory
at creation (`intake.py:51-77`, modulo a leaf `default`), this guarantees a promoted
column can never be legitimately empty — no separate "these N columns are always
required" rule is needed, and schemas that don't collect a given concept (e.g.
`disease_only` has no appointment/insurance sections) simply don't promote those
columns; the corresponding `patient_form` column stays `None`, as today.

## 4. DSL changes (`vera_core/forms/dsl.py`)

```python
PROMOTABLE_COLUMNS: frozenset[str] = frozenset({
    "patient_name", "patient_dob", "appointment_date", "chart_number",
    "appointment_type", "member_policy_id", "insurance_provider",
    "insurance_provider_phone_number",
})

class FormSchemaDoc(_Model):
    ...
    system_fields: dict[str, str] | None = None
    promoted_fields: dict[str, str] | None = None
    ...
```

Validator additions in `_validate_document`, alongside the existing `system_fields`
block (`dsl.py:487-490`):

```python
system_field_paths = set((self.system_fields or {}).values())
for column, path in (self.promoted_fields or {}).items():
    if column not in PROMOTABLE_COLUMNS:
        errors.append(f"promoted_fields.{column}: not a promotable patient_form column")
    if path not in leaves:
        errors.append(f"promoted_fields.{column}: {path!r} does not resolve to a leaf")
    elif path not in system_field_paths:
        errors.append(
            f"promoted_fields.{column}: {path!r} is not a system_fields target "
            "(promoted fields must be guaranteed present at intake)"
        )
```

This is a structural, closed-vocabulary check exactly like the rest of `dsl.py`'s
document validator — errors surface at schema-authoring / `just compile-schemas` time,
not at runtime.

## 5. Mapping logic (`vera_core/forms/intake.py`)

`promote_columns` stops hand-walking `_PATIENT_INFO`/`_APPOINTMENT_INFO`/etc. section
literals and becomes schema-driven, taking a value-getter so the same function serves
both a nested intake payload and a flat `{field_path: value}` map:

```python
def promote_columns(
    get_value: Callable[[str], Any], doc: FormSchemaDoc
) -> PromotedIdentifiers:
    """Extract + normalize the columns `doc.promoted_fields` maps to. Raises
    InvalidIntakeValue on a bad date."""
    values: dict[str, Any] = {}
    for column, path in (doc.promoted_fields or {}).items():
        raw = get_value(path)
        if column in ("patient_dob", "appointment_date"):
            values[column] = _parse_date(raw, path)
        elif column == "patient_name":
            cleaned = _clean_str(raw)
            values[column] = cleaned.lower() if cleaned is not None else None
        elif column == "chart_number":
            cleaned = _clean_str(raw)
            values[column] = None if cleaned is not None and cleaned.upper() == "N/A" else cleaned
        else:
            values[column] = _clean_str(raw)
    return PromotedIdentifiers(**values)
```

`PromotedIdentifiers` drops the `member_id` field (§7) and otherwise keeps the same
shape, with every field defaulting to `None` (a schema that doesn't promote a given
column simply never populates it — `dataclass` fields need `= None` defaults now that
not every call sets every field).

Call sites:

- **Intake** (`upload_patient_form`, `patient_forms.py:136-158`):
  `promote_columns(lambda p: _resolve_path(body.intake_payload, p), doc)` — `_resolve_path`
  already exists (`intake.py:80-89`) and walks a root-anchored path against the nested
  payload.
- **Dispute-resolve** (`resolve_disputes`, `patient_forms.py:513-692`):
  `promote_columns(lambda p: current_values.get(p), doc)` — `current_values` is already
  built in that handler (post-write, for the `completion_pct` recompute), keyed by the
  same `field_path` namespace the schema paths live in (`field_answer.field_path` is
  byte-identical to the schema path, per `forms/CLAUDE.md`).

## 6. `resolve_disputes` wiring

After `resolve_disputes` computes `current_values` and before it recomputes
`completion_pct`, it also computes promoted values and assigns any that changed:

```python
promoted = promote_columns(lambda p: current_values.get(p), doc)
for column in (doc.promoted_fields or {}):
    new_value = getattr(promoted, column)
    if getattr(form, column) != new_value:
        setattr(form, column, new_value)
```

This runs for every call to the endpoint that changes a mapped field's current value —
not only fields that were under an open dispute — matching "any field-change request
promotes dynamically." It only writes columns the schema actually declares in
`promoted_fields`, and only when the derived value actually differs, so it doesn't add
spurious `updated_at` churn on every resolve call.

`doc` (the `FormSchemaDoc`) is parsed once from `version.schema_json`, reusing the same
parse the endpoint already needs for `completion_pct_v2`/`is_v2`.

## 7. Collapse `member_id`/`member_policy_id` into one column, named `member_id`

**Superseded from a plain drop, per a follow-up decision in conversation (2026-07-10):**
rather than only dropping the dead `member_id` and leaving the live column named
`member_policy_id`, rename the live column to `member_id` too — so the schema handle
(`system_fields["member_id"]`), the `promoted_fields` key, the DB column, and the API
field all say the same thing end to end. That mismatch (schema calls the concept
"member_id"; the promoted column was called "member_policy_id") was part of what made
this area confusing in the first place.

- One migration: drop the dead old `member_id` column, then rename `member_policy_id` to
  `member_id`. Dual-path-safe: guarded on `member_policy_id` still existing, so it's a
  no-op on a fresh DB (`0001`'s `create_all` off the already-renamed model creates the
  final `member_id` shape directly) and a drop-then-rename on an existing dev DB.
- `PatientForm` model: one `member_id` field (`vera_core/models/patient_form.py`),
  replacing both the old dead field and `member_policy_id`.
- `PromotedIdentifiers.member_id` replaces both the old always-`None` `member_id` field
  and `member_policy_id`.
- `PatientFormDetail` (the dispute-resolve/detail response): its `member_id` field is
  removed with **no replacement** — it only ever exposed the dead column, and the detail
  view doesn't otherwise surface this identifier (only the worklist summary does).
- `PatientFormSummary` (the worklist list response) and the frontend
  (`vera-frontend/src/lib/patient-forms/types.ts`,
  `vera-frontend/src/pages/DataManagement.tsx`): `member_policy_id` renamed to
  `member_id`.
- Both catalogs' `promoted_fields` (§8) are written directly in this final shape — keyed
  `"member_id"`, not `"member_policy_id"` — so they don't need a second edit.

**Still do not touch `system_fields["member_id"]`** in either catalog — unchanged from
the original reasoning: it's the handle that hydrates the `{{member_id}}` placeholder
actually spoken during the call (`ibv_standard.py`'s introduction task prompt references
it twice, e.g. "provide the member ID {{member_id}}"). The rename above makes the DB
column finally *agree* with this handle's name instead of colliding with it.

`system_fields["policy_id"]` (both catalogs) — a pure duplicate of `system_fields["member_id"]`,
same target path, no distinct consumer (nothing renders `{{policy_id}}`) — is dropped for
the same coherence reason (§8).

## 8. Catalog updates

`ibv_standard.py` (`vera_core/forms/catalog/ibv_standard.py`) adds `promoted_fields`
covering all 8 columns, reusing the paths its `system_fields` block already has
correct (including the two insurance ones, fixing the intake-time bug in §1):

```python
promoted_fields={
    "patient_name": "sections.patient_information.patient_name",
    "patient_dob": "sections.patient_information.patient_dob",
    "chart_number": "sections.patient_information.chart_number",
    "appointment_date": "sections.appointment_information.appointment_date",
    "appointment_type": "sections.appointment_information.appointment_type",
    "member_id": "sections.insurance_information.policy_number",
    "insurance_provider": "sections.insurance_reference_information.insurance_provider_name",
    "insurance_provider_phone_number": "sections.insurance_reference_information.insurance_phone_number",
},
```

`disease_only.py` adds `promoted_fields` for what it has data for — it has a
`policy_details.policy_number` field (same shape as `ibv_standard`'s
`insurance_information.policy_number`) but no appointment/insurance-*reference*
sections:

```python
promoted_fields={
    "patient_name": "sections.patient_information.patient_name",
    "patient_dob": "sections.patient_information.patient_dob",
    "chart_number": "sections.patient_information.chart_number",
    "member_id": "sections.policy_details.policy_number",
},
```

Both are followed by `just compile-schemas` to regenerate the compiled JSON artifacts
(the freshness test fails CI on drift).

**Coherence cleanup, same section of both files:** both catalogs' `system_fields` also
declare a `"policy_id"` handle aliasing the exact same path as `"member_id"`
(`sections.insurance_information.policy_number` / `sections.policy_details.policy_number`)
— a pure duplicate with no distinct consumer (nothing renders `{{policy_id}}`; only
`{{member_id}}` is spoken, in `ibv_standard.py`'s introduction task prompt).
`required_intake_fields` dedupes by path, so removing `"policy_id"` doesn't change any
required-field behavior. Drop the `"policy_id"` handle from both catalogs, keeping
`"member_id"` as the one canonical handle for this path — resolves the confusing
same-path duplication without touching the still-needed `"member_id"` handle (§7).

## 9. Error handling

- A malformed `promoted_fields` entry (unknown column, dangling path, path not backed by
  `system_fields`) fails `FormSchemaDoc` validation — i.e. `just compile-schemas` /
  schema authoring time, the same place every other DSL mistake is caught. No new
  runtime error path.
- `promote_columns` keeps raising `InvalidIntakeValue` for an unparseable date, exactly
  as today; `resolve_disputes` doesn't currently validate date-shaped dispute values
  before this change and isn't expected to start now — a bad date reaching promotion
  from a dispute would raise and the request would fail with a 500 rather than silently
  writing garbage. Given dispute values already went through `field_answer` write with
  no format validation, tightening that is out of scope here (a pre-existing gap in the
  dispute-resolve input contract, not something this change introduces or worsens).

## 10. Testing

- `tests/unit/forms/test_schema_dsl.py` — reject: unknown `promoted_fields` column, path
  not resolving to a leaf, path not present in `system_fields`. Accept: a valid
  `promoted_fields` block.
- `tests/unit/forms/test_intake.py` — `promote_columns` against both a nested payload and
  a flat map, covering date parsing, `patient_name` lowercasing, chart `"N/A"` handling,
  and a schema whose `promoted_fields` omits some columns (values stay `None`).
- `tests/integration/control_plane/test_patient_forms_intake.py` /
  `test_patient_forms_disputes.py` (existing dispute-resolve integration coverage) —
  add a case: resolving a dispute on the insurance-reference fields updates
  `patient_form.insurance_provider`/`insurance_provider_phone_number`, and resolving a
  dispute on a non-promoted field leaves promoted columns untouched.

## 11. Rollout note (not part of this spec's code change)

Before adding the `member_id`-drop migration, the two current alembic heads
(`8115d1763daf`, `089b3e98f0b0`) need resolving. This branch's migrations aren't shared
yet, so per the user's direction the fix is to relink `8115d1763daf`'s `down_revision`
from `efa94eaaf3f9` to `089b3e98f0b0` (one linear chain, no merge-revision file) and wipe
+ recreate the local dev DB — not `alembic merge heads`. This is a prep step for the
implementation plan, not a design decision about the feature itself.
