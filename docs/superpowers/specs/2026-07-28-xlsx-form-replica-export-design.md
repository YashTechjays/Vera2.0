# XLSX form-replica export — design

**Date:** 2026-07-28
**Status:** approved (brainstorm with Yash)
**Scope:** backend only — `vera_core/forms/export_form_sheet.py` (new),
`vera_core/forms/export.py` (compose), tests. Endpoint, permissions, audit,
filename, and the Provenance tab are unchanged.

## Problem

The exported XLSX (tab "Form") is a flat label/value list. The requirement is a
**replica of the UI data-entry form**: when a VA opens the downloaded file it
should look like the form modal in Data Management — same section placement,
same section styling, same CPT grids — so the deliverable matches what the
reviewer signed off on screen. (An older client sheet exists but is explicitly
NOT the target; the UI layout is.)

## Decision (from brainstorm)

Approach A — a backend layout engine that mirrors the UI's schema-derived
rules. The UI computes its layout from the schema plus a small rule set
(`vera-frontend/src/components/ibv/SchemaForm.tsx`); the export reimplements
those rules in Python over the same `FormSchemaDoc`. Rejected: FE-side
generation (moves a PHI disclosure off the audited server path) and
hand-maintained styled templates (drifts from the UI, per-schema upkeep).

Choices confirmed by Yash: side-by-side placement like the UI (not stacked);
full UI styling (green context headers, borders, bold labels, required
markers); Provenance sheet stays as tab 2 unchanged.

## Design

### 1. Module shape

- New pure module `packages/vera_core/src/vera_core/forms/export_form_sheet.py`:
  `render_form_sheet(ws, doc, values, shared_conditions)` — writes the replica
  into an openpyxl worksheet. DB-free, PHI-carrying (same contract as
  export.py's header comment).
- `export.py::build_workbook` keeps its exact signature and callers; tab 1 is
  produced by the new module (v2 schemas), tab 2 (Provenance) and the v1/legacy
  flat path are unchanged.

### 2. Placement geometry (mirrors SchemaForm.tsx)

Column bands: left block = columns A–B (label/value), spacer C, right block =
D–E, spacer F, reference rail = G–H.

- Top band: LEFT_TOP = [patient_information, insurance_information] stacked in
  A–B; RIGHT_TOP = [appointment_information, verification_information,
  benefit_coverage] stacked in D–E; RAIL = [hospital_information,
  provider_reference_information, insurance_reference_information] stacked in
  G–H.
- Below the top band (starting after the tallest of the three stacks + one
  blank row): remaining sections in schema document order — `ui.layout ==
  "table"` sections span full width from column A; consecutive non-table
  sections render two-up (A–B, then D–E, alternating), per the FE `chunkRest`
  rule.
- The three placement lists are module constants with a cross-reference
  comment to SchemaForm.tsx; a unit test asserts they match the sections
  present in the shipped ibv_standard schema (drift guard). Sections named in
  the constants but absent from a given schema are skipped (same as FE).

### 3. Label/value section rendering

- Section title bar merged across the block's two columns; fill green for
  `role == "context"` sections (the UI's "known background" signal), neutral
  gray otherwise; bold text; thin border.
- One row per leaf, schema order: label cell (bold, `*` suffix when required,
  thin border) + value cell (thin border). Value coercion matches the UI:
  None/absent → empty; values rendered with `str()` (same `_str` helper).
- Gated-off (inapplicable) leaves are INCLUDED with a light-gray fill and
  empty value — matching the UI's grayed rows. (Today's flat export skips
  them; the replica must not.)
- Applicability uses the same `leaf_gates`/`is_applicable`/`shared_conditions`
  machinery export.py already uses.

### 4. Grid (`ui.layout == "table"`) sections

Reimplement the FE SectionMatrix model in Python:

- Each top-level subtree of the section is a **group** (e.g. "In Vitro
  Fertilization (IVF)"): its rows are the CPT-code entries; shared leaf keys
  across rows are the **columns** (Covered, Copay ($), Coinsurance (%), Prior
  Authorization Required, …); group-level leaves (cycle limit, additional
  notes) are per-group merged cells.
- Rendering: full-width section title bar; a styled header row (Service,
  ICD-10, CPT Code, then column titles, then group-leaf titles); per group, N
  rows with merged (rowspan) Service and ICD-10 cells and merged group-leaf
  cells; bordered value cells; inapplicable cells light-gray.
- The exact group/row/column derivation must match
  `vera-frontend/src/lib/ibv/schema.ts`'s table model (read it during
  implementation; encode the same rules, with a unit test over a two-group
  fixture asserting header order and merge ranges).

### 5. Styling map (single constants block)

- Context header fill: green (match the UI's green, e.g. C6EFCE-family);
  non-context header fill: light gray; grid column-header fill: light gray;
  inapplicable fill: lighter gray; thin borders throughout; bold labels and
  titles; red asterisk not required — a plain `*` suffix on the label is
  sufficient (Excel single-color text per cell keeps it simple).
- Column widths set so labels/values are readable (labels ≈ 30, values ≈ 22,
  grid columns ≈ 14); no frozen panes; sheet title stays "Form".

### 6. Unchanged / out of scope

Provenance tab content and order; export endpoint, RBAC, Completed-only
gating, `export_artifact` ledger + `FORM_EXPORTED` audit; `ibv-<id>.xlsx`
filename; PDF (future); low-confidence styling on tab 1 (export is gated on
dispute resolution; judge data lives on tab 2).

## Testing

Pure unit tests over the builder (no DB), using a compact fixture schema plus
the shipped ibv_standard doc:

1. Top-band geometry: section titles land in the expected anchor cells (A/D/G
   bands), rail present.
2. Two-up + full-width flow below the band follows document order and
   `ui.layout` (table section spans from column A).
3. Grid model: header order, rowspan merge ranges, group-leaf merges for a
   two-group fixture.
4. Context sections get the green fill; non-context gray.
5. Required marker and inapplicable-gray handling.
6. Values land adjacent to their labels (spot-check several paths).
7. Existing export/endpoint/provenance tests stay green.

Verification: `just check`; then a real export from the UI on test, opened and
eyeballed against the form modal.
