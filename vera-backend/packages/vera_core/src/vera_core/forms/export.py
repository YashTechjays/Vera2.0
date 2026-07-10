"""Pure XLSX mapping layer for the form export — DB-free, format-agnostic inputs.

The workbook IS PHI (it carries field values): callers stream it inside an
authed, audited, no-store response and never log its contents. A future PDF
renderer consumes the same arguments.
"""

from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any, cast

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet

from vera_core.forms.conditions import is_applicable, is_v2, leaf_gates
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.services.call_provenance import CallAttempt, FieldProvenance

_BOLD = Font(bold=True)


def _str(v: Any) -> str:
    """Coerce a field value to a spreadsheet-safe string; None → empty string."""
    return "" if v is None else str(v)


def _bold_header(ws: Worksheet, title: str) -> None:
    """Append a blank separator row then a bold-title row to *ws*."""
    ws.append([])
    ws.append([title])
    ws.cell(row=ws.max_row, column=1).font = _BOLD


def build_workbook(
    schema_json: Mapping[str, Any],
    values: Mapping[str, Any],
    sources: Mapping[str, str],
    provenance: Mapping[str, FieldProvenance],
    attempts: Sequence[CallAttempt],
) -> bytes:
    wb = Workbook()
    form_ws = cast(Worksheet, wb.active)
    form_ws.title = "Form"

    sorted_paths = sorted(values)

    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        shared = doc.shared_conditions or {}
        # Build label map from the already-parsed doc — avoids a second model_validate
        # inside field_labels (review.py re-parses if passed raw schema_json).
        titles = {path: leaf.title for path, leaf, _ in leaf_gates(doc)}
        current_section: str | None = None
        for path, leaf, gates in leaf_gates(doc):
            if not is_applicable(gates, values, shared):
                continue
            # v2 paths are root-anchored: sections.<key>.<...>
            section_key = path.split(".")[1]
            if section_key != current_section:
                current_section = section_key
                _bold_header(form_ws, doc.sections[section_key].title)
            value = values.get(path)
            if value is None and leaf.default is not None:
                value = leaf.default  # DSL §4.4: defaults count as filled on export
            form_ws.append([leaf.title, _str(value)])
    else:
        titles = {p: p for p in sorted_paths}
        for path in sorted_paths:
            form_ws.append([path, _str(values[path])])

    prov_ws = cast(Worksheet, wb.create_sheet("Provenance"))
    prov_ws.append(
        ["Field path", "Label", "Source", "Attempt", "Mode", "Judge confidence", "Supported"]
    )
    prov_ws.cell(row=1, column=1).font = _BOLD
    for path in sorted_paths:
        p = provenance.get(path)
        prov_ws.append(
            [
                path,
                titles.get(path, path),
                sources.get(path, ""),
                p.attempt if p else None,
                p.mode if p else None,
                p.judge.confidence if p and p.judge else None,
                p.judge.supported if p and p.judge else None,
            ]
        )

    attempt_by_id = {a.id: a.attempt for a in attempts}
    _bold_header(prov_ws, "Call history")
    prov_ws.append(["Attempt", "Mode", "Status", "Created at", "Retry of attempt"])
    for a in attempts:
        prov_ws.append(
            [
                a.attempt,
                a.mode,
                a.status,
                a.created_at.isoformat(),
                attempt_by_id.get(a.retry_of) if a.retry_of else None,
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
