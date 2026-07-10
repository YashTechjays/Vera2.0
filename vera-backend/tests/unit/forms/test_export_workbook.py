from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from openpyxl import load_workbook

from vera_core.forms.export import build_workbook
from vera_core.services.call_provenance import CallAttempt, FieldProvenance, JudgeInfo

V2 = {
    "dsl_version": "2.1",
    "name": "Test",
    "insurance_type": "infertility_treatment",
    "sections": {
        "cov": {
            "title": "Coverage",
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
    "tasks": [{"task_key": "t1", "title": "Task 1", "sections": ["cov"]}],
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


def test_workbook_has_form_and_provenance_sheets() -> None:
    path = "sections.cov.network_status"
    data = build_workbook(
        V2,
        values={path: "in-network"},
        sources={path: "ai_call"},
        provenance={path: FieldProvenance(attempt=1, mode="full", judge=JudgeInfo(90, True, "e"))},
        attempts=[_attempt(1, "full")],
    )
    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == ["Form", "Provenance"]
    form_cells = [tuple(r) for r in wb["Form"].iter_rows(values_only=True)]
    assert ("Coverage", None) in form_cells or ("Coverage",) in form_cells
    assert ("Network status", "in-network") in form_cells
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
