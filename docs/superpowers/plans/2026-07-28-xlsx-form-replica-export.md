# XLSX Form-Replica Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The export's "Form" tab becomes a styled replica of the UI data-entry form (same section placement, section styling, and CPT grids), instead of a flat label/value list.

**Architecture:** A new pure module `vera_core/forms/export_form_sheet.py` ports the frontend's schema-derived layout rules (placement constants + two-up flow from `SchemaForm.tsx`; the matrix model from `lib/ibv/schema.ts::getSectionTable`) to Python/openpyxl. `export.py::build_workbook` keeps its signature; only its v2 "Form" sheet body is replaced. Provenance tab, endpoint, audit, filename unchanged.

**Tech Stack:** Python 3.12, openpyxl, pytest (pure unit tests, no DB needed for the new module).

**Spec:** `docs/superpowers/specs/2026-07-28-xlsx-form-replica-export-design.md`

## Global Constraints

- PHI: the workbook carries values by design; the new module must contain NO logging at all.
- mypy --strict (annotate everything); ruff check + `ruff format --check` must pass.
- `just check` verbatim on the final tree; after implementation run the `/simplify` skill on the change, then re-run `just check` (repo rule).
- Branch: `feat/xlsx-form-replica-export` (created; spec committed). Working dir for commands: `vera-backend/`.
- Placement lists must carry a cross-reference comment to `vera-frontend/src/components/ibv/SchemaForm.tsx` (both places update together).
- The FE reference sources are authoritative for rules: `SchemaForm.tsx` (placement/flow), `lib/ibv/schema.ts::getSectionTable` (matrix model), `SectionMatrix.tsx` (grid header order/static columns). Read them before deviating from the code given here.

---

### Task 1: Matrix table model (Python port of `getSectionTable`)

**Files:**
- Create: `packages/vera_core/src/vera_core/forms/export_form_sheet.py` (model part)
- Test: `tests/unit/forms/test_export_form_sheet.py`

**Interfaces:**
- Consumes: `vera_core.forms.dsl` (`FormSchemaDoc`, `Section`, `Group`, `Leaf`).
- Produces: `section_table(section_key: str, section: Section) -> SectionTable | None` and the dataclasses `TableCell(path, leaf)`, `TableRow(path, label, cells)`, `TableGroup(path, label, icd10, rows, extras)`, `SectionTable(columns, extra_columns, has_icd, groups, leaves)`. Task 2's grid renderer consumes exactly these.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/forms/test_export_form_sheet.py`:

```python
"""Unit tests for the UI-replica export sheet — pure, no DB."""

from __future__ import annotations

from typing import Any

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.export_form_sheet import section_table

# A minimal v2 doc with one table section exercising BOTH group shapes:
#  - ivf: subgroup rows (cpt_1, cpt_2) + group-level extras (cycle_limit, notes)
#  - oi:  leaf-only group (its leaves split into row cells vs extras by key)
_TABLE_DOC: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "Replica Test Schema",
    "insurance_type": "infertility_treatment",
    "system_fields": {"in_network": "sections.info.in_network"},
    "rep_call_reference_number_field": "sections.info.in_network",
    "promoted_fields": {},  # filled in fixture below
    "sections": {
        "info": {
            "title": "Info",
            "role": "collect",
            "fields": {
                "in_network": {
                    "type": "text", "title": "In Network", "role": "ask",
                    "required": True, "prompt": {"ask": "In network?"},
                },
            },
        },
        "treatment": {
            "title": "Treatment",
            "role": "collect",
            "ui": {"layout": "table"},
            "fields": {
                "tx_covered": {
                    "type": "text", "title": "Treatment Covered", "role": "ask",
                    "prompt": {"ask": "Covered?"},
                },
                "ivf": {
                    "title": "In Vitro Fertilization (IVF)",
                    "codes": {"icd10": ["Z31.83"]},
                    "fields": {
                        "cycle_limit": {
                            "type": "text", "title": "Cycle Limit", "role": "ask",
                            "prompt": {"ask": "Cycle limit?"},
                        },
                        "notes": {
                            "type": "text", "title": "Additional Notes", "role": "ask",
                            "prompt": {"ask": "Notes?"},
                        },
                        "cpt_58970": {
                            "title": "CPT 58970",
                            "fields": {
                                "covered": {
                                    "type": "text", "title": "Covered", "role": "ask",
                                    "prompt": {"ask": "Covered?"},
                                },
                                "copay": {
                                    "type": "text", "title": "Copay ($)", "role": "ask",
                                    "prompt": {"ask": "Copay?"},
                                },
                            },
                        },
                        "cpt_89280": {
                            "title": "CPT 89280",
                            "fields": {
                                "covered": {
                                    "type": "text", "title": "Covered", "role": "ask",
                                    "prompt": {"ask": "Covered?"},
                                },
                                "copay": {
                                    "type": "text", "title": "Copay ($)", "role": "ask",
                                    "prompt": {"ask": "Copay?"},
                                },
                            },
                        },
                    },
                },
                "oi": {
                    "title": "Ovulation Induction",
                    "codes": {"icd10": ["N97.0"], "cpt": ["58323"]},
                    "fields": {
                        "covered": {
                            "type": "text", "title": "Covered", "role": "ask",
                            "prompt": {"ask": "Covered?"},
                        },
                        "copay": {
                            "type": "text", "title": "Copay ($)", "role": "ask",
                            "prompt": {"ask": "Copay?"},
                        },
                        "cycle_limit": {
                            "type": "text", "title": "Cycle Limit", "role": "ask",
                            "prompt": {"ask": "Cycle limit?"},
                        },
                    },
                },
            },
        },
    },
    "tasks": [{"task_key": "main", "title": "Main", "sections": ["info", "treatment"]}],
}


def _doc() -> FormSchemaDoc:
    from vera_core.forms.dsl import PromotedFields

    raw = dict(_TABLE_DOC)
    raw["promoted_fields"] = dict.fromkeys(
        PromotedFields.model_fields, "sections.info.in_network"
    )
    return FormSchemaDoc.model_validate(raw)


def test_section_table_none_for_non_table_sections() -> None:
    doc = _doc()
    assert section_table("info", doc.sections["info"]) is None


def test_section_table_model_matches_frontend_rules() -> None:
    doc = _doc()
    table = section_table("treatment", doc.sections["treatment"])
    assert table is not None

    # Section-level leaves render above the grid.
    assert [p for p, _ in table.leaves] == ["sections.treatment.tx_covered"]

    # Columns = shared leaf keys of subgroup rows, first-seen order.
    assert [key for key, _ in table.columns] == ["covered", "copay"]
    assert [t for _, t in table.columns] == ["Covered", "Copay ($)"]
    # Extras = leaf keys sitting beside subgroups inside any group.
    assert [key for key, _ in table.extra_columns] == ["cycle_limit", "notes"]
    assert table.has_icd is True

    ivf, oi = table.groups
    assert ivf.label == "In Vitro Fertilization (IVF)"
    assert ivf.icd10 == "Z31.83"
    assert [r.label for r in ivf.rows] == ["CPT 58970", "CPT 89280"]
    assert ivf.rows[0].cells["copay"].path == (
        "sections.treatment.ivf.cpt_58970.copay"
    )
    assert set(ivf.extras) == {"cycle_limit", "notes"}

    # Leaf-only group: the group itself is one row labelled by its CPT codes;
    # keys that exist as extras elsewhere split out of the row cells.
    assert oi.label == "Ovulation Induction"
    assert len(oi.rows) == 1
    assert oi.rows[0].label == "58323"
    assert set(oi.rows[0].cells) == {"covered", "copay"}
    assert set(oi.extras) == {"cycle_limit"}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd vera-backend && uv run pytest tests/unit/forms/test_export_form_sheet.py -v
```
Expected: FAIL — `export_form_sheet` module does not exist.

- [ ] **Step 3: Implement the model**

Create `packages/vera_core/src/vera_core/forms/export_form_sheet.py` (model half; the renderer is Task 2 — add it to this same file then):

```python
"""UI-replica "Form" sheet for the XLSX export — a Python port of the
frontend's schema-derived layout rules.

The sheet carries PHI (field values): callers stream it inside an authed,
audited, no-store response. This module must never log.

Layout sources of truth (update together):
- placement / flow: vera-frontend/src/components/ibv/SchemaForm.tsx
- matrix model:     vera-frontend/src/lib/ibv/schema.ts (getSectionTable)
- grid headers:     vera-frontend/src/components/ibv/SectionMatrix.tsx
"""

from __future__ import annotations

from dataclasses import dataclass

from vera_core.forms.dsl import Group, Leaf, Section


@dataclass(frozen=True)
class TableCell:
    path: str
    leaf: Leaf


@dataclass(frozen=True)
class TableRow:
    path: str
    label: str
    cells: dict[str, TableCell]


@dataclass(frozen=True)
class TableGroup:
    path: str
    label: str
    icd10: str
    rows: list[TableRow]
    extras: dict[str, TableCell]


@dataclass(frozen=True)
class SectionTable:
    columns: list[tuple[str, str]]        # (leaf key, column title)
    extra_columns: list[tuple[str, str]]  # (leaf key, column title)
    has_icd: bool
    groups: list[TableGroup]
    leaves: list[tuple[str, Leaf]]        # section-level (path, leaf) field rows


def _leaf_entries(g: Group | Section) -> list[tuple[str, Leaf]]:
    return [(k, f) for k, f in g.fields.items() if isinstance(f, Leaf)]


def _group_entries(g: Group | Section) -> list[tuple[str, Group]]:
    return [(k, f) for k, f in g.fields.items() if isinstance(f, Group)]


def section_table(section_key: str, section: Section) -> SectionTable | None:
    """Matrix model for a ``ui.layout: "table"`` section — the layout hint alone
    decides. Top-level groups are bands; their subgroups are rows and their own
    leaves are per-group rowspan extras; a subgroup-less group is itself one row
    labelled by its CPT codes, its leaves split row-cells/extras by key."""
    if section.ui is None or section.ui.layout != "table":
        return None
    base = f"sections.{section_key}"
    top_leaves = [(f"{base}.{k}", f) for k, f in _leaf_entries(section)]
    top_groups = _group_entries(section)

    extra_titles: dict[str, str] = {}
    for _, g in top_groups:
        if not _group_entries(g):
            continue
        for k, f in _leaf_entries(g):
            extra_titles.setdefault(k, f.title)

    column_titles: dict[str, str] = {}
    groups: list[TableGroup] = []
    for gkey, g in top_groups:
        gpath = f"{base}.{gkey}"
        subgroups = _group_entries(g)
        extras: dict[str, TableCell] = {}
        rows: list[TableRow] = []
        if subgroups:
            for k, f in _leaf_entries(g):
                extras[k] = TableCell(f"{gpath}.{k}", f)
            for rkey, r in subgroups:
                rpath = f"{gpath}.{rkey}"
                entries = _leaf_entries(r)
                for k, f in entries:
                    column_titles.setdefault(k, f.title)
                rows.append(
                    TableRow(
                        rpath,
                        r.title,
                        {k: TableCell(f"{rpath}.{k}", f) for k, f in entries},
                    )
                )
        else:
            entries = _leaf_entries(g)
            row_entries = [(k, f) for k, f in entries if k not in extra_titles]
            for k, f in row_entries:
                column_titles.setdefault(k, f.title)
            for k, f in entries:
                if k in extra_titles:
                    extras[k] = TableCell(f"{gpath}.{k}", f)
            cpt = ", ".join(g.codes.cpt) if g.codes and g.codes.cpt else "—"
            rows.append(
                TableRow(
                    gpath,
                    cpt,
                    {k: TableCell(f"{gpath}.{k}", f) for k, f in row_entries},
                )
            )
        icd = ", ".join(g.codes.icd10) if g.codes and g.codes.icd10 else ""
        groups.append(TableGroup(gpath, g.title, icd, rows, extras))

    return SectionTable(
        columns=list(column_titles.items()),
        extra_columns=list(extra_titles.items()),
        has_icd=any(g.icd10 for g in groups),
        groups=groups,
        leaves=top_leaves,
    )
```

NOTE for the implementer: check `Codes`' actual field names in `dsl.py` (`cpt`, `icd10`) and `Group.title`'s existence before running; if `Group.title` is optional, default to the group key humanized. If the frozen dataclasses fight mypy over `dict`/`list` invariance, drop `frozen=True` rather than fighting it.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd vera-backend && uv run pytest tests/unit/forms/test_export_form_sheet.py -v
```
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/export_form_sheet.py tests/unit/forms/test_export_form_sheet.py
git commit -m "feat(export): matrix table model — Python port of the FE getSectionTable rules

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Sheet renderer — styles, blocks, grids, placement

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/export_form_sheet.py` (append renderer)
- Test: `tests/unit/forms/test_export_form_sheet.py` (append)

**Interfaces:**
- Consumes: Task 1's `section_table` + dataclasses; `vera_core.forms.conditions` (`leaf_gates`, `is_applicable`, `is_required`); openpyxl `Worksheet`.
- Produces: `render_form_sheet(ws: Worksheet, doc: FormSchemaDoc, values: Mapping[str, Any]) -> None` — Task 3 calls exactly this.

- [ ] **Step 1: Write the failing tests (append to the test file)**

```python
from openpyxl import Workbook

from vera_core.forms.export_form_sheet import (
    LEFT_TOP,
    RAIL,
    RIGHT_TOP,
    render_form_sheet,
)

# Extends the table fixture with placed sections: two LEFT_TOP, one RIGHT_TOP
# (context role → green), one RAIL (context), plus a trailing two-up pair.
_PLACED_DOC: dict[str, Any] = {
    **_TABLE_DOC,
    "sections": {
        "patient_information": {
            "title": "Patient Information",
            "role": "collect",
            "fields": {
                "patient_name": {
                    "type": "text", "title": "Patient Name", "role": "ask",
                    "required": True, "prompt": {"ask": "Name?"},
                },
                "spouse_name": {
                    "type": "text", "title": "Spouse Name", "role": "ask",
                    "applicable_when": {
                        "path": "sections.patient_information.patient_name",
                        "op": "eq", "value": "married",
                    },
                    "prompt": {"ask": "Spouse?"},
                },
            },
        },
        "appointment_information": {
            "title": "Appointment Information",
            "role": "context",
            "fields": {
                "appointment_date": {
                    "type": "text", "title": "Appointment Date", "role": "context",
                },
            },
        },
        "hospital_information": {
            "title": "Hospital Information",
            "role": "context",
            "fields": {
                "hospital_name": {
                    "type": "text", "title": "Hospital Name", "role": "context",
                },
            },
        },
        "treatment": _TABLE_DOC["sections"]["treatment"],
        "alpha": {
            "title": "Alpha",
            "role": "collect",
            "fields": {
                "a1": {"type": "text", "title": "A One", "role": "ask",
                        "prompt": {"ask": "?"}},
            },
        },
        "beta": {
            "title": "Beta",
            "role": "collect",
            "fields": {
                "b1": {"type": "text", "title": "B One", "role": "ask",
                        "prompt": {"ask": "?"}},
            },
        },
    },
    "system_fields": {"in_network": "sections.patient_information.patient_name"},
    "rep_call_reference_number_field": "sections.patient_information.patient_name",
    "tasks": [{
        "task_key": "main", "title": "Main",
        "sections": [
            "patient_information", "appointment_information",
            "hospital_information", "treatment", "alpha", "beta",
        ],
    }],
}


def _placed_doc() -> FormSchemaDoc:
    from vera_core.forms.dsl import PromotedFields

    raw = dict(_PLACED_DOC)
    raw["promoted_fields"] = dict.fromkeys(
        PromotedFields.model_fields, "sections.patient_information.patient_name"
    )
    return FormSchemaDoc.model_validate(raw)


def _render(values: dict[str, Any]) -> Any:
    wb = Workbook()
    ws = wb.active
    render_form_sheet(ws, _placed_doc(), values)
    return ws


def test_top_band_geometry_and_values() -> None:
    ws = _render({"sections.patient_information.patient_name": "Jane"})
    # Left block anchored at A1; right at D1; rail at G1.
    assert ws.cell(row=1, column=1).value == "Patient Information"
    assert ws.cell(row=1, column=4).value == "Appointment Information"
    assert ws.cell(row=1, column=7).value == "Hospital Information"
    # Label + value adjacency, with the required marker.
    assert ws.cell(row=2, column=1).value == "Patient Name *"
    assert ws.cell(row=2, column=2).value == "Jane"


def test_context_sections_get_green_title_fill() -> None:
    ws = _render({})
    ctx = ws.cell(row=1, column=4).fill.start_color.rgb  # context section
    plain = ws.cell(row=1, column=1).fill.start_color.rgb  # collect section
    assert ctx != plain
    assert str(ctx).endswith("C6EFCE")


def test_inapplicable_leaf_grayed_with_empty_value() -> None:
    ws = _render({"sections.patient_information.spouse_name": "should-not-show"})
    # patient_name != "married" → spouse row (row 3, left block) is gated off.
    assert ws.cell(row=3, column=2).value in (None, "")
    assert str(ws.cell(row=3, column=2).fill.start_color.rgb).endswith("F5F5F5")


def test_grid_full_width_and_two_up_flow_below_band() -> None:
    ws = _render({"sections.treatment.ivf.cpt_58970.copay": "30"})
    # Find the grid title and header row.
    titles = {ws.cell(row=r, column=1).value: r for r in range(1, ws.max_row + 1)}
    grid_row = titles["Treatment"]
    header = grid_row + 2  # +1 section-leaf row (tx_covered) sits between
    assert ws.cell(row=header, column=1).value == "Service"
    assert ws.cell(row=header, column=2).value == "ICD-10"
    assert ws.cell(row=header, column=3).value == "CPT Code"
    assert ws.cell(row=header, column=4).value == "Covered"
    assert ws.cell(row=header, column=5).value == "Copay ($)"
    assert ws.cell(row=header, column=6).value == "Cycle Limit"
    assert ws.cell(row=header, column=7).value == "Additional Notes"
    # IVF band: Service cell merged across its two CPT rows; copay value lands.
    ivf_first = header + 1
    merged = {str(rng) for rng in ws.merged_cells.ranges}
    assert f"A{ivf_first}:A{ivf_first + 1}" in merged
    assert ws.cell(row=ivf_first, column=1).value == "In Vitro Fertilization (IVF)"
    assert ws.cell(row=ivf_first, column=3).value == "CPT 58970"
    assert ws.cell(row=ivf_first, column=5).value == "30"
    # Two-up run: alpha (left band) and beta (right band) share a row.
    alpha_row = next(
        r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=1).value == "Alpha"
    )
    assert ws.cell(row=alpha_row, column=4).value == "Beta"


def test_placement_constants_reference_fe() -> None:
    # Guard: the constants stay aligned with SchemaForm.tsx's lists.
    assert LEFT_TOP == ["patient_information", "insurance_information"]
    assert RIGHT_TOP == [
        "appointment_information", "verification_information", "benefit_coverage",
    ]
    assert RAIL == [
        "hospital_information", "provider_reference_information",
        "insurance_reference_information",
    ]
```

NOTE for the implementer: verify the `applicable_when` JSON shape against
`dsl.py`'s `Comparison` model (and the catalog's `eq(...)` helper output) before
running — adapt the fixture's condition syntax if the field names differ; the
test's INTENT (spouse row gated on patient_name == "married") must not change.

- [ ] **Step 2: Run to verify they fail**

```bash
cd vera-backend && uv run pytest tests/unit/forms/test_export_form_sheet.py -v
```
Expected: FAIL — `render_form_sheet` / constants don't exist yet.

- [ ] **Step 3: Implement the renderer (append to export_form_sheet.py)**

```python
from collections.abc import Mapping
from typing import Any

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

from vera_core.forms.conditions import is_applicable, is_required, leaf_gates
from vera_core.forms.dsl import FormSchemaDoc

# Placement mirrors vera-frontend/src/components/ibv/SchemaForm.tsx
# (LEFT_TOP / RIGHT_TOP / RAIL) — update both together.
LEFT_TOP = ["patient_information", "insurance_information"]
RIGHT_TOP = ["appointment_information", "verification_information", "benefit_coverage"]
RAIL = [
    "hospital_information",
    "provider_reference_information",
    "insurance_reference_information",
]

# Column anchors (1-based): left block A–B, right D–E, rail G–H; spacers C, F.
_LEFT_COL, _RIGHT_COL, _RAIL_COL = 1, 4, 7

_THIN = Side(style="thin", color="B0B0B0")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_BOLD = Font(bold=True)
_CENTER = Alignment(horizontal="center", vertical="center")
_TOP_LEFT = Alignment(vertical="top", wrap_text=True)
_FILL_CONTEXT = PatternFill("solid", start_color="C6EFCE")  # UI's green header
_FILL_PLAIN = PatternFill("solid", start_color="E7E6E6")
_FILL_GRID_HEADER = PatternFill("solid", start_color="F2F2F2")
_FILL_INAPPLICABLE = PatternFill("solid", start_color="F5F5F5")


def _fit(v: Any) -> str:
    return "" if v is None else str(v)


class _Ctx:
    """Render context: values + applicability machinery, computed once."""

    def __init__(self, doc: FormSchemaDoc, values: Mapping[str, Any]) -> None:
        self.values = values
        self.shared = doc.shared_conditions or {}
        self.gates = {path: gates for path, _leaf, gates in leaf_gates(doc)}

    def applicable(self, path: str) -> bool:
        return is_applicable(self.gates.get(path, ()), self.values, self.shared)


def _style(ws: Worksheet, row: int, col: int, *, fill: PatternFill | None = None,
           bold: bool = False, center: bool = False) -> None:
    c = ws.cell(row=row, column=col)
    c.border = _BORDER
    if fill is not None:
        c.fill = fill
    if bold:
        c.font = _BOLD
    c.alignment = _CENTER if center else _TOP_LEFT


def _title_bar(ws: Worksheet, row: int, col: int, span: int, text: str,
               *, context: bool) -> None:
    if span > 1:
        ws.merge_cells(start_row=row, start_column=col,
                       end_row=row, end_column=col + span - 1)
    ws.cell(row=row, column=col, value=text)
    fill = _FILL_CONTEXT if context else _FILL_PLAIN
    for i in range(span):
        _style(ws, row, col + i, fill=fill, bold=True, center=True)


def _leaf_row(ws: Worksheet, row: int, col: int, path: str, leaf: Leaf,
              ctx: _Ctx) -> None:
    applicable = ctx.applicable(path)
    star = " *" if applicable and is_required(leaf, ctx.values, ctx.shared) else ""
    ws.cell(row=row, column=col, value=f"{leaf.title}{star}")
    ws.cell(row=row, column=col + 1,
            value=_fit(ctx.values.get(path)) if applicable else "")
    fill = None if applicable else _FILL_INAPPLICABLE
    _style(ws, row, col, fill=fill, bold=True)
    _style(ws, row, col + 1, fill=fill)


def _field_block(ws: Worksheet, row: int, col: int, section_key: str,
                 section: Section, ctx: _Ctx) -> int:
    """Label/value section anchored at (row, col); returns rows consumed.
    Mirrors the FE flattenSection order: groups emit a sub-header then their
    children, depth-first."""
    _title_bar(ws, row, col, 2, section.title, context=section.role == "context")
    r = row + 1

    def emit(prefix: str, fields: Mapping[str, Any]) -> None:
        nonlocal r
        for key, f in fields.items():
            path = f"{prefix}.{key}"
            if isinstance(f, Leaf):
                _leaf_row(ws, r, col, path, f, ctx)
                r += 1
            else:  # nested group: sub-header row, then children
                ws.cell(row=r, column=col, value=f.title)
                ws.merge_cells(start_row=r, start_column=col,
                               end_row=r, end_column=col + 1)
                _style(ws, r, col, bold=True)
                _style(ws, r, col + 1)
                r += 1
                emit(path, f.fields)

    emit(f"sections.{section_key}", section.fields)
    return r - row


def _grid_block(ws: Worksheet, row: int, section_key: str, section: Section,
                ctx: _Ctx) -> int:
    """Full-width matrix section; returns rows consumed. Header order mirrors
    SectionMatrix.tsx: Service, [ICD-10], CPT Code, columns…, extras…"""
    table = section_table(section_key, section)
    assert table is not None  # caller checked ui.layout
    static = ["Service"] + (["ICD-10"] if table.has_icd else []) + ["CPT Code"]
    width = len(static) + len(table.columns) + len(table.extra_columns)

    _title_bar(ws, row, 1, width, section.title,
               context=section.role == "context")
    r = row + 1
    for path, leaf in table.leaves:  # section-level field rows above the grid
        _leaf_row(ws, r, 1, path, leaf, ctx)
        r += 1

    headers = static + [t for _, t in table.columns] + [t for _, t in table.extra_columns]
    for i, h in enumerate(headers):
        ws.cell(row=r, column=1 + i, value=h)
        _style(ws, r, 1 + i, fill=_FILL_GRID_HEADER, bold=True, center=True)
    r += 1

    icd_col = 2 if table.has_icd else None
    cpt_col = 3 if table.has_icd else 2
    first_val_col = cpt_col + 1
    extra_col0 = first_val_col + len(table.columns)

    for group in table.groups:
        n = len(group.rows)
        top = r
        ws.cell(row=top, column=1, value=group.label)
        if n > 1:
            ws.merge_cells(start_row=top, start_column=1, end_row=top + n - 1,
                           end_column=1)
        if icd_col is not None:
            ws.cell(row=top, column=icd_col, value=group.icd10)
            if n > 1:
                ws.merge_cells(start_row=top, start_column=icd_col,
                               end_row=top + n - 1, end_column=icd_col)
        for j, (key, _title) in enumerate(table.extra_columns):
            cell = group.extras.get(key)
            col = extra_col0 + j
            if cell is not None and ctx.applicable(cell.path):
                ws.cell(row=top, column=col, value=_fit(ctx.values.get(cell.path)))
            if n > 1:
                ws.merge_cells(start_row=top, start_column=col,
                               end_row=top + n - 1, end_column=col)
        for row_model in group.rows:
            ws.cell(row=r, column=cpt_col, value=row_model.label)
            for j, (key, _title) in enumerate(table.columns):
                cell = row_model.cells.get(key)
                col = first_val_col + j
                applicable = cell is not None and ctx.applicable(cell.path)
                if cell is not None and applicable:
                    ws.cell(row=r, column=col, value=_fit(ctx.values.get(cell.path)))
                _style(ws, r, col,
                       fill=None if applicable else _FILL_INAPPLICABLE)
            r += 1
        # Border/style pass over the band's static + extra cells.
        for rr in range(top, top + n):
            for col in (1, *((icd_col,) if icd_col else ()), cpt_col,
                        *range(extra_col0, extra_col0 + len(table.extra_columns))):
                _style(ws, rr, col, bold=col == 1)
    return r - row


def render_form_sheet(ws: Worksheet, doc: FormSchemaDoc,
                      values: Mapping[str, Any]) -> None:
    """Write the UI-replica form into *ws* (title, widths, sections)."""
    ws.title = "Form"
    ctx = _Ctx(doc, values)
    sections = doc.sections

    def stack(keys: list[str], col: int) -> int:
        r = 1
        for key in keys:
            if key not in sections:
                continue
            r += _field_block(ws, r, col, key, sections[key], ctx) + 1
        return r

    next_row = max(stack([k for k in LEFT_TOP], _LEFT_COL),
                   stack([k for k in RIGHT_TOP], _RIGHT_COL),
                   stack([k for k in RAIL], _RAIL_COL))

    placed = set(LEFT_TOP) | set(RIGHT_TOP) | set(RAIL)
    rest = [k for k in sections if k not in placed]
    r = next_row + 1
    run: list[str] = []

    def flush_run() -> None:
        nonlocal r, run
        for i in range(0, len(run), 2):
            pair = run[i : i + 2]
            heights = []
            for j, key in enumerate(pair):
                col = _LEFT_COL if j == 0 else _RIGHT_COL
                heights.append(_field_block(ws, r, col, key, sections[key], ctx))
            r += max(heights) + 1
        run = []

    for key in rest:
        sec = sections[key]
        if sec.ui is not None and sec.ui.layout == "table":
            flush_run()
            r += _grid_block(ws, r, key, sec, ctx) + 1
        else:
            run.append(key)
    flush_run()

    widths = {1: 30, 2: 22, 3: 14, 4: 30, 5: 22, 6: 3, 7: 30, 8: 22}
    for idx, w in widths.items():
        ws.column_dimensions[chr(ord("A") + idx - 1)].width = w
```

NOTE for the implementer: the grid header static labels ("Service", "ICD-10", "CPT Code") must be confirmed against `SectionMatrix.tsx`'s `<th>` JSX before finishing — adjust the strings (not the structure) if the UI differs. Column-width note: grids reuse columns A–H plus beyond; that keeps top-band widths dominant, acceptable per spec.

- [ ] **Step 4: Run the module tests**

```bash
cd vera-backend && uv run pytest tests/unit/forms/test_export_form_sheet.py -v
```
Expected: ALL PASS (7 tests). Iterate on geometry math (row offsets in the grid test are the likely first failure — fix the code, not the test, unless the test's arithmetic contradicts the implemented-and-correct layout).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/export_form_sheet.py tests/unit/forms/test_export_form_sheet.py
git commit -m "feat(export): UI-replica sheet renderer — placement, styling, CPT grids

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Compose into build_workbook + drift guard

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/export.py`
- Test: `tests/unit/forms/test_export.py` (existing — adapt), `tests/unit/forms/test_export_form_sheet.py` (append drift test)

**Interfaces:**
- Consumes: `render_form_sheet` (Task 2); existing Provenance-sheet code in export.py.
- Produces: unchanged `build_workbook(schema_json, values, sources, provenance, attempts) -> bytes` — endpoint untouched.

- [ ] **Step 1: Read the current export.py end-to-end** (123 lines) and the existing `tests/unit/forms/test_export.py` to see exactly what the flat v2 branch does and which assertions exist. The Provenance sheet builder and the non-v2 (legacy) flat path must be preserved verbatim.

- [ ] **Step 2: Write/adapt failing tests**

In `tests/unit/forms/test_export.py`: the assertions that walk the old flat "Form" sheet (bold section headers + label/value pairs appended vertically) must be replaced with replica-anchored ones. Keep every Provenance-sheet assertion untouched. Replacement shape (adapt names to the file's existing fixtures):

```python
def test_v2_form_sheet_is_ui_replica(...) -> None:
    wb = load_workbook(BytesIO(build_workbook(schema_json, values, sources, prov, attempts)))
    ws = wb["Form"]
    # replica anchors instead of the flat walk:
    assert ws.cell(row=1, column=1).value  # left-band section title present
    # a known label/value adjacency from the fixture:
    ...
    assert "Provenance" in wb.sheetnames  # tab 2 untouched
```

Append the drift/smoke test to `tests/unit/forms/test_export_form_sheet.py`:

```python
def test_ibv_standard_renders_and_placement_lists_exist() -> None:
    from vera_core.forms.catalog import ibv_standard  # adapt to the real accessor

    doc = FormSchemaDoc.model_validate(ibv_standard.schema_json())  # adapt accessor
    for key in (*LEFT_TOP, *RIGHT_TOP, *RAIL):
        assert key in doc.sections, f"placement list references missing section {key}"
    wb = Workbook()
    render_form_sheet(wb.active, doc, {})  # must not raise on the real schema
```

(Adapt the ibv_standard accessor to whatever the catalog module exposes — grep `def ` in `vera_core/forms/catalog/ibv_standard.py`; the seed script builds the schema JSON from it.)

- [ ] **Step 3: Run to verify the new/adapted tests fail**

```bash
cd vera-backend && uv run pytest tests/unit/forms/test_export.py tests/unit/forms/test_export_form_sheet.py -v
```

- [ ] **Step 4: Implement composition**

In `export.py::build_workbook`, replace only the v2 "Form"-sheet body:

```python
    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        render_form_sheet(form_ws, doc, values)
        titles = {path: leaf.title for path, leaf, _gates in leaf_gates(doc)}
    else:
        ... # existing flat path, unchanged
```

`titles` must keep feeding the Provenance sheet exactly as before (today it is built inside the same leaf_gates walk — preserve that data flow; only the Form-sheet writing moves to the new module). Add the import: `from vera_core.forms.export_form_sheet import render_form_sheet`.

- [ ] **Step 5: Run both test files, then the whole unit suite**

```bash
cd vera-backend && uv run pytest tests/unit/forms/ -v
```
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/export.py tests/unit/forms/
git commit -m "feat(export): Form tab becomes the UI-replica sheet; Provenance tab unchanged

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Full gate + simplify + PR

**Files:** none new — whole-tree verification.

- [ ] **Step 1: Full CI gate verbatim**

```bash
cd vera-backend && just check
```
Expected: green (if the 5 admin invitation tests fail locally with UniqueViolation on `%@test.example` users, that's the known stale-DB pollution — confirm they're the ONLY failures and note it; CI runs on a fresh DB).

- [ ] **Step 2: Run the /simplify skill on the branch diff** (repo rule), apply behavior-preserving fixes only, re-run `just check`.

- [ ] **Step 3: Push and open PR to dev**

```bash
git push -u origin feat/xlsx-form-replica-export
```
PR title: `feat(export): XLSX Form tab replicates the UI data-entry form`. Body links the spec and notes: Provenance tab unchanged; endpoint/audit unchanged; manual verification step = export a Completed form on test after deploy and eyeball against the form modal.
